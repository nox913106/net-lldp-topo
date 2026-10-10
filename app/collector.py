"""
Background collector service
Runs periodic SNMP polling of all managed devices
Integrates with Alert Engine and Topology Engine
"""
import asyncio
import logging
import signal
import socket
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import get_settings, log_level
from app.db.database import async_session_maker, init_db
from app.core.snmp_collector import SNMPCollector, InterfaceStats
from app.core.snmp_pool import CollectorPool
from app.core.damping import FlapDamper, OfflineConfirm, Verdict
from app.core.poll_scheduler import OfflineBackoff
from app.core.fast_poll import FastPoller
from app.core.trap_receiver import TrapReceiver
from app.core.topology_engine import TopologyEngine
from app.core.alert_engine import AlertEngine
from app.core.discovery_cache import DiscoveryCache
from app.core.lease import (
    Coverage, FLEET_LEASE, Lease, coordination_ttl, member_lease
)
from app.core.link_index import LinkWriteBuffer, RawLinkIndex
from app.core.sharding import Shard
from app.core.traffic import sample_values
from app.core.log_exporter import get_log_exporter, LogLevel
from app.models.device import Device, DeviceStatus

settings = get_settings()

# This process's slice of the fleet. Raises on a shard id that is not a shard
# of the configured count, because the alternative - starting anyway with a
# clamped value - means some devices are polled twice and others not at all,
# and nothing in the logs says so (app/core/sharding.py).
SHARD = Shard.from_settings(settings)

# Every line of every shard says which shard it came from. N collector
# containers write to the same place, and without this a log line about a
# device gives no way to tell which process was responsible for it.
#
# The level is APP_LOG_LEVEL (app/config.py), which until now was documented
# but ignored: DEBUG is where the "why was this neighbour not discovered"
# lines are, and both entry points hardcoded INFO.
logging.basicConfig(
    level=log_level(),
    format=(
        f"%(asctime)s - [{SHARD.label()}] %(name)s - %(levelname)s - %(message)s"
        if SHARD.enabled
        else "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
)
logger = logging.getLogger(__name__)

# Is the fast loop running? It decides which half of the collector owns the
# traffic columns of raw_links: whoever samples the counters writes them, and
# the other half must leave them alone or it would overwrite a fresh
# measurement with a stale one. See app/core/link_index.py.
FAST_LOOP_ENABLED = settings.collector_fast_interval > 0

# Counters are sampled by whichever loop is the fast one. With the fast loop
# running, the inventory poll also stops asking for the two octet tables,
# which is two of its seventeen table walks per device per cycle.
INVENTORY_TRAFFIC = settings.collector_traffic_stats and not FAST_LOOP_ENABLED

# Devices that do not answer SNMP are probed less and less often instead of
# costing a full timeout on every OID of every cycle. The state lives in this
# process only - see app/core/poll_scheduler.py for why.
#
# One per loop: a device that has been dark for an hour should not cost the
# inventory poll a full set of timeouts, but the fast loop's judgement about
# when to try again is on its own, much shorter, scale.
offline_backoff = OfflineBackoff(
    base_interval=settings.collector_interval,
    max_interval=settings.collector_offline_backoff_max,
    enabled=settings.collector_offline_backoff,
    confirm_after=settings.alert_offline_confirm_cycles
)

fast_backoff = OfflineBackoff(
    base_interval=max(1, settings.collector_fast_interval),
    max_interval=settings.collector_offline_backoff_max,
    enabled=settings.collector_offline_backoff,
    confirm_after=settings.alert_offline_confirm_cycles
)

# How many consecutive unanswered polls make a device offline. One instance
# for the whole process, not one per loop: the two loops ask the same question
# about the same device, so a failure seen by either one counts, and the fast
# loop is normally the one that confirms (app/core/damping.py).
offline_confirm = OfflineConfirm(cycles=settings.alert_offline_confirm_cycles)

# Flap damping for per-cable link_down alerts. Lives here rather than inside
# AlertEngine because an engine lives for one cycle: both the fast loop and
# the trap receiver hand this same instance to the engine they build, so a
# cable that flaps between a trap and a poll is still one cable flapping.
flap_damper = FlapDamper(
    transitions=settings.alert_link_flap_transitions,
    window_seconds=settings.alert_link_flap_window
)

# SNMP engines, reused across devices instead of built per device per cycle.
# Measured: a device costs 227.5 ms of CPU to poll with its own engine and
# 65.3 ms with a shared one, so at 500 devices the old per-device collector
# spent 81 s of CPU per cycle on engines - see app/core/snmp_pool.py.
#
# One pool per loop, because they ask for different things: the fast loop
# wants the octet counters, the inventory poll does not when the fast loop is
# running.
inventory_pool = CollectorPool(
    default_community=settings.snmp_default_community,
    timeout=settings.snmp_timeout,
    retries=settings.snmp_retries,
    collect_traffic=INVENTORY_TRAFFIC,
    label="inventory"
)

fast_pool = CollectorPool(
    default_community=settings.snmp_default_community,
    timeout=settings.snmp_timeout,
    retries=settings.snmp_retries,
    collect_traffic=settings.collector_traffic_stats,
    label="fast"
)

# How many seconds a lease outlives its last renewal, derived from the loop
# that renews it: the fast loop when it is running, the inventory loop
# otherwise. Both the fleet-wide lease and this shard's heartbeat use it.
LEASE_TTL = coordination_ttl(
    settings.collector_fast_interval if FAST_LOOP_ENABLED
    else settings.collector_interval
)

# Exactly one shard rebuilds merged_links and runs the alert checks per
# interval: both read the whole fleet, so doing them per shard would mean N
# processes racing to rewrite the same rows and to create the same alerts.
# None when there is a single collector - then there is nothing to coordinate
# with, and no reason to let a lease stand between the fleet and its alerts.
fleet_lease = Lease(FLEET_LEASE, SHARD.name, LEASE_TTL) if SHARD.enabled else None

# "This shard is running", so whoever holds the lease above can notice that
# one of the configured shards is not.
heartbeat = member_lease(SHARD, LEASE_TTL) if SHARD.enabled else None
coverage = Coverage(SHARD) if SHARD.enabled else None

# Which neighbours are already known, and which ones cannot be turned into a
# device. Re-seeded from the database at the start of every cycle, so the two
# "do I know this neighbour?" queries per neighbour are answered from memory.
discovery_cache = DiscoveryCache(
    negative_ttl=settings.collector_discovery_cache_ttl
)

# A discovery insert needs its own session, on top of the session the device
# being polled is holding. Without a bound on how many can be open at once,
# the first cycle of a large fleet - when every neighbour is new - would ask
# for twice COLLECTOR_CONCURRENT connections. Discovery is not latency
# critical, so it queues.
_discovery_writers = asyncio.Semaphore(4)


async def resolve_hostname(hostname: str) -> str:
    """Resolve a hostname to an IPv4 address without blocking the event loop"""
    if not hostname:
        return ""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
        return infos[0][4][0] if infos else ""
    except (socket.gaierror, UnicodeError, OSError, IndexError):
        return ""


async def auto_discover_neighbor(
    parent_device_id: int,
    parent_hostname: str,
    neighbor,
    collector: SNMPCollector,
    log_exporter
) -> None:
    """
    Add a newly seen LLDP/CDP neighbour to the database.

    The "do I already know this one?" question is answered from the process
    cache rather than with two queries per neighbour per cycle, and the insert
    runs in its own session: a neighbour that cannot be added (two devices
    seeing it at the same moment, a hostname that collides) must not cost the
    polled device its link data.
    """
    advertised = neighbor.remote_hostname
    if not advertised:
        return

    # claim() is also the lock that stops two devices seeing the same new
    # neighbour from both probing and both inserting it.
    if not discovery_cache.claim(advertised):
        return

    try:
        if discovery_cache.knows(hostname=advertised):
            return

        # Resolve the neighbour IP:
        # 1. the management address advertised through LLDP/CDP
        # 2. DNS lookup of the advertised system name
        neighbor_ip = neighbor.remote_mgmt_ip
        if not neighbor_ip:
            neighbor_ip = await resolve_hostname(advertised)

        if not neighbor_ip:
            logger.debug(f"Could not resolve IP for neighbor {advertised}")
            discovery_cache.mark_dead_end(advertised)
            return

        if discovery_cache.knows(ip=neighbor_ip):
            # Already managed under a different name - nothing to add, and no
            # point resolving it again next cycle.
            discovery_cache.mark_dead_end(advertised)
            return

        # Validate the device over SNMP before adding it
        device_info = await collector.get_device_info(neighbor_ip)
        if not device_info:
            logger.debug(f"SNMP probe failed for neighbor {neighbor_ip}")
            discovery_cache.mark_dead_end(advertised)
            return

        hostname = device_info.hostname or advertised
        if discovery_cache.knows(hostname=hostname):
            discovery_cache.mark_dead_end(advertised)
            return

        new_device = Device(
            hostname=hostname,
            ip_address=neighbor_ip,
            snmp_community=collector.community,
            device_type="access",
            vendor=device_info.vendor,
            status="managed",
            auto_discover=True,
            parent_device_id=parent_device_id,
            last_seen=datetime.utcnow()
        )

        async with _discovery_writers:
            async with async_session_maker() as db:
                db.add(new_device)
                try:
                    await db.commit()
                except IntegrityError:
                    # Another collector process got there first
                    await db.rollback()
                    discovery_cache.add(hostname, neighbor_ip)
                    return

        discovery_cache.add(hostname, neighbor_ip)
        logger.info(
            f"Auto-discovered neighbor: {hostname} ({neighbor_ip}) "
            f"via {parent_hostname}"
        )

        await log_exporter.log_discovery(
            event_type="new_device",
            device_hostname=hostname,
            device_ip=neighbor_ip,
            extra={"discovered_via": parent_hostname}
        )

    except Exception as e:
        logger.error(f"Error in auto_discover_neighbor: {e}")
    finally:
        discovery_cache.release(advertised)


def _index_interface_stats(
    stats: List[InterfaceStats]
) -> Tuple[Dict[int, InterfaceStats], Dict[str, InterfaceStats]]:
    """Index the interface table by ifIndex and by every name it answers to"""
    by_index: Dict[int, InterfaceStats] = {}
    by_name: Dict[str, InterfaceStats] = {}
    for stat in stats:
        by_index.setdefault(stat.port_index, stat)
        for name in (stat.port_name, stat.port_descr):
            if name:
                by_name.setdefault(name.strip().lower(), stat)
    return by_index, by_name


def _match_interface(
    neighbor,
    by_index: Dict[int, InterfaceStats],
    by_name: Dict[str, InterfaceStats]
) -> Optional[InterfaceStats]:
    """
    Find the interface a neighbour was seen on.

    ifIndex first: it is resolved during LLDP collection and is unambiguous.
    The name is the fallback for devices whose lldpLocPortTable cannot be
    mapped to an ifIndex.
    """
    stat = by_index.get(neighbor.local_port_index)
    if stat is not None:
        return stat
    return by_name.get((neighbor.local_port or "").strip().lower())


_was_holder = False


async def _own_fleet_phase() -> bool:
    """
    Whether this process runs the merge and the alert checks this cycle.

    True with a single collector: there is nobody else, so there is nothing to
    coordinate and no reason for a lease to be able to stand between the fleet
    and its alerts. With several shards it is whoever holds the lease, which
    is a lease rather than "shard 0 always does it" so that losing one process
    does not stop alerting for the whole fleet (app/core/lease.py).
    """
    global _was_holder

    if fleet_lease is None:
        return True

    held = await fleet_lease.acquire()
    if held != _was_holder:
        logger.info(
            "This shard now runs the fleet-wide phase"
            if held else
            "Another shard runs the fleet-wide phase; this one polls its "
            "own devices only"
        )
        _was_holder = held

    if held and coverage is not None:
        await coverage.check()
    return held


async def poll_all_devices():
    """Poll all managed devices"""
    cycle_started = time.monotonic()

    async with async_session_maker() as db:
        # Get the managed devices this process is responsible for. The filter
        # is in the WHERE clause rather than applied afterwards, so a shard
        # does not read the whole fleet to discard most of it.
        statement = SHARD.scope(
            select(Device).where(Device.status != DeviceStatus.EXCLUDED),
            Device.id
        )
        result = await db.execute(statement)
        devices = result.scalars().all()

        # Refresh what auto-discovery already knows. Every device is listed,
        # excluded ones included - otherwise an excluded device gets
        # rediscovered and re-added on the next cycle.
        #
        # Deliberately not sharded: this answers "does this neighbour already
        # exist?", and a neighbour that belongs to another shard exists just
        # as much. Sharding this query would have every shard re-adding the
        # other shards' devices under new ids every cycle.
        known = await db.execute(select(Device.hostname, Device.ip_address))
        discovery_cache.seed(known.all())

        # The cable lookup table, read in one statement and answered from
        # memory for the rest of the cycle. Without it every cable costs a
        # SELECT before its UPDATE - at 500 devices that was ~10,000 queries
        # per cycle on their own.
        link_index = RawLinkIndex()
        indexed_links = await link_index.load(db, shard=SHARD)

    # Forget devices that no longer exist so the backoff state cannot grow
    offline_backoff.prune(device.id for device in devices)
    offline_confirm.prune(device.id for device in devices)

    due = [d for d in devices if offline_backoff.should_poll(d.id)]
    due_ids = {d.id for d in due}
    skipped = len(devices) - len(due)

    if skipped:
        logger.info(
            f"Starting poll cycle for {len(due)} devices "
            f"({skipped} skipped, backing off after repeated failures)"
        )
        for device in devices:
            if device.id not in due_ids:
                logger.debug(
                    f"Skipping {device.hostname} ({device.ip_address}): "
                    f"{offline_backoff.failure_count(device.id)} consecutive "
                    f"failures, retry in "
                    f"{offline_backoff.seconds_until_retry(device.id):.0f}s"
                )
    else:
        logger.info(f"Starting poll cycle for {len(due)} devices")

    # Poll devices concurrently with limit
    semaphore = asyncio.Semaphore(settings.collector_concurrent)

    async def poll_with_semaphore(device: Device):
        async with semaphore:
            # One collector per credential, shared by every device that uses
            # it, rather than one per device: an SNMP engine is expensive to
            # build and nothing on it is per-device (app/core/snmp_pool.py).
            async with inventory_pool.collector_for(device) as collector:
                # Talk to the device first, with no database connection held.
                # An unreachable device costs several seconds of timeouts, and
                # holding a connection across that would pin
                # COLLECTOR_CONCURRENT connections for the whole SNMP phase.
                result = await fetch_device_state(collector, device)

                # Whether this device is *called* offline is a separate
                # question from whether it answered: with
                # ALERT_OFFLINE_CONFIRM_CYCLES above 1 it takes that many
                # unanswered polls in a row, so that one lost exchange cannot
                # raise a critical alert (app/core/damping.py). Decided here
                # rather than inside store_poll_result because only what SNMP
                # returned is evidence about the device - a database error
                # while storing a successful poll is not.
                if result and result.get("success"):
                    offline_confirm.record_success(device.id)
                    verdict = None
                else:
                    verdict = offline_confirm.record_failure(device.id)

                # One session per device, taken only for the write. An
                # AsyncSession serialises everything issued through it without
                # complaining, so passing one shared session to every
                # concurrent poll made COLLECTOR_CONCURRENT inert for the
                # database half of the cycle - docs/PERFORMANCE.md 8.1.
                async with async_session_maker() as db:
                    tracked = await db.get(Device, device.id)
                    if tracked is None:
                        return None  # removed between listing and polling

                    reachable = await store_poll_result(
                        collector, tracked, result, db, link_index, verdict=verdict
                    )
                    try:
                        await db.commit()
                    except SQLAlchemyError as e:
                        await db.rollback()
                        logger.error(f"Saving {device.hostname} failed: {e}")

            if reachable:
                offline_backoff.record_success(device.id)
            else:
                failures = offline_backoff.record_failure(device.id)
                logger.debug(
                    f"{device.hostname}: {failures} consecutive failures, "
                    f"next attempt in "
                    f"{offline_backoff.delay_for(failures):.0f}s"
                )
            return reachable

    devices_started = time.monotonic()
    tasks = [poll_with_semaphore(d) for d in due]
    # One device blowing up must not abandon the rest of the cycle
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    for device, outcome in zip(due, outcomes):
        if isinstance(outcome, BaseException):
            logger.error(f"Polling {device.hostname} raised: {outcome}")
    devices_seconds = time.monotonic() - devices_started

    merge_seconds = alert_seconds = 0.0
    merged_here = False

    # Merging and alerting belong to whichever loop runs most often: an alert
    # can only ever be as fresh as the loop that evaluates it, and utilisation
    # is read off merged_links. With the fast loop running they are its job,
    # and doing them here as well would only add a slower copy of the same
    # work - see docs/PERFORMANCE.md section 20. When this loop is the one
    # that does them, it is still only one shard's job, not every shard's.
    if not FAST_LOOP_ENABLED and await _own_fleet_phase():
        merged_here = True
        async with async_session_maker() as db:
            logger.info("Updating merged links...")
            merge_started = time.monotonic()
            topology_engine = TopologyEngine(db)
            await topology_engine.update_merged_links_in_db()
            merge_seconds = time.monotonic() - merge_started

            # Nothing in the cycle can raise an alert before this point, so
            # the cycle time is also the floor on how late an alert can be -
            # docs/PERFORMANCE.md section 18.
            logger.info("Running alert checks...")
            alert_started = time.monotonic()
            alert_engine = AlertEngine(db, damper=flap_damper)
            await alert_engine.run_check_cycle()
            alert_seconds = time.monotonic() - alert_started

    total = time.monotonic() - cycle_started
    # Only report the fleet-wide phase when this cycle actually ran it:
    # "merge 0.0s" on a cycle that never merged reads like the merge being
    # free rather than being somebody else's job.
    fleet = (
        f", merge {merge_seconds:.1f}s, alerts {alert_seconds:.1f}s"
        if merged_here else ""
    )
    logger.info(
        f"Inventory cycle completed in {total:.1f}s "
        f"(devices {devices_seconds:.1f}s{fleet}, "
        f"{indexed_links} links indexed), "
        f"{inventory_pool.stats()}, discovery cache: {discovery_cache.stats()}"
    )
    return total


async def fetch_device_state(
    collector: SNMPCollector, device: Device
) -> Optional[Dict]:
    """
    The SNMP half of a poll. Holds no database connection.

    Returns None when the exchange itself failed, which the caller stores the
    same way as a device that did not answer.
    """
    try:
        return await collector.poll_device(device.ip_address)
    except Exception as e:
        logger.error(f"Error polling {device.hostname}: {e}")
        return None


async def store_poll_result(
    collector: SNMPCollector,
    device: Device,
    result: Optional[Dict],
    db,
    link_index: RawLinkIndex,
    verdict: Optional[Verdict] = None
) -> bool:
    """
    The database half of a poll: write what SNMP returned.

    `device` must belong to `db`. `link_index` is the cycle's snapshot of
    raw_links, which is what makes this a batched write rather than two
    statements per cable. Returns True when the device answered SNMP, so the
    caller can keep the offline backoff up to date.

    `verdict` is the caller's OfflineConfirm decision about a device that did
    not answer. None means "call it offline", which is both the default and
    what ALERT_OFFLINE_CONFIRM_CYCLES=1 produces; a verdict that is not yet
    confirmed leaves the device's status alone, so a single lost exchange
    writes nothing and alerts nothing.
    """
    log_exporter = get_log_exporter()

    try:
        if result and result["success"]:
            # Update device info
            previous_status = device.status
            device.status = DeviceStatus.MANAGED
            device.last_seen = datetime.utcnow()

            if result["metrics"]:
                device.cpu_percent = result["metrics"].cpu_percent
                device.memory_percent = result["metrics"].memory_percent

            if result["device_info"]:
                device.vendor = result["device_info"].vendor
                device.uptime_seconds = result["device_info"].uptime_seconds

            # Log recovery if device was offline
            if previous_status == DeviceStatus.OFFLINE:
                await log_exporter.log(
                    level=LogLevel.INFO,
                    source="collector",
                    message=f"Device {device.hostname} is back online",
                    device_hostname=device.hostname,
                    device_ip=device.ip_address
                )

            # The interface table, ready to be matched against each neighbour's
            # local port, so every cable gets the port's real speed and rate
            by_index, by_name = _index_interface_stats(result["interface_stats"])
            sampled_at = datetime.utcnow()
            matched = 0

            # One buffer per device: the cables are written as two statements
            # at the end instead of a SELECT and an UPDATE each. The traffic
            # columns are only ours to write when no fast loop is running.
            writes = LinkWriteBuffer(write_traffic=INVENTORY_TRAFFIC)
            new_keys = set()

            for neighbor in result["lldp_neighbors"] + result["cdp_neighbors"]:
                # Which raw_links row is this cable? Answered from the index
                # the cycle loaded in one statement, not with a query per
                # cable - see app/core/link_index.py.
                key = (device.id, neighbor.local_port, neighbor.remote_hostname)
                previous = link_index.get(*key)

                stat = _match_interface(neighbor, by_index, by_name)
                if stat is not None:
                    matched += 1

                values = {
                    # These can change on an existing cable, and older versions
                    # of the collector stored unusable values in some of them
                    "local_port_index": neighbor.local_port_index,
                    "remote_port": neighbor.remote_port,
                    "remote_chassis_id": neighbor.remote_chassis_id,
                    "protocol": neighbor.protocol,
                    "last_seen": sampled_at,
                }
                values.update(sample_values(previous, stat, sampled_at))

                if previous is not None:
                    writes.update(previous.id, values)
                else:
                    first_time = key not in new_keys
                    new_keys.add(key)
                    writes.insert(key, values)

                    if first_time:
                        await log_exporter.log_discovery(
                            event_type="new_link",
                            device_hostname=device.hostname,
                            device_ip=device.ip_address,
                            extra={
                                "local_port": neighbor.local_port,
                                "remote_hostname": neighbor.remote_hostname,
                                "remote_port": neighbor.remote_port
                            }
                        )

                # Auto-discover neighbor devices if enabled
                if device.auto_discover:
                    await auto_discover_neighbor(
                        device.id, device.hostname, neighbor, collector, log_exporter
                    )

            updated, inserted = await writes.flush(db)

            neighbor_count = len(result["lldp_neighbors"]) + len(result["cdp_neighbors"])
            logger.info(
                f"Polled {device.hostname} ({device.ip_address}): "
                f"{len(result['lldp_neighbors'])} LLDP + "
                f"{len(result['cdp_neighbors'])} CDP neighbors, "
                f"{len(result['interface_stats'])} interfaces, "
                f"{matched}/{neighbor_count} links matched to an interface, "
                f"{updated} links updated, {inserted} added"
            )
            return True
        else:
            if verdict is not None and not verdict.confirmed:
                # Not enough failures in a row to call it offline yet. The
                # status, and therefore the alert, is left exactly as it was.
                logger.warning(
                    f"Polled {device.hostname}: FAILED "
                    f"({verdict.describe()}, not calling it offline yet)"
                )
                return False

            # Mark device as potentially offline
            if device.status != DeviceStatus.OFFLINE:
                await log_exporter.log(
                    level=LogLevel.WARNING,
                    source="collector",
                    message=f"Device {device.hostname} is not responding",
                    device_hostname=device.hostname,
                    device_ip=device.ip_address
                )
            device.status = DeviceStatus.OFFLINE
            logger.warning(f"Polled {device.hostname}: FAILED")
            return False

    except Exception as e:
        # A failure in here is a failure of *this code*, not evidence about
        # the device: the SNMP exchange has already happened by now. Writing
        # status=offline on a database error made the collector raise a
        # critical alert about a healthy device every time a write failed.
        logger.error(f"Error storing poll result for {device.hostname}: {e}")
        return False


async def _prepare_database() -> None:
    """
    Make sure the schema this collector needs is in place.

    The collector container can come up before the app container, and the
    indexes and traffic columns are what keep a cycle from degenerating into
    full table scans, so it does not wait for someone else to add them.
    """
    for attempt in range(3):
        try:
            await init_db()
            return
        except Exception as e:
            # Harmless when the app container is doing the same DDL right now
            logger.warning(f"Schema check failed ({e}), retrying")
            await asyncio.sleep(2 * (attempt + 1))
    logger.error("Could not verify the database schema, continuing anyway")


# Breathing room after a cycle that overran its interval completely. It stops
# a fleet the collector cannot keep up with from turning into a hot loop
# against both the database and every switch in it.
OVERRUN_GAP_SECONDS = 5.0


async def _fixed_cadence(name: str, interval: float, body, overrun_hint: str):
    """
    Run ``body`` every ``interval`` seconds, measured between starts.

    Sleeping the whole interval *after* the work made the real period
    "interval plus however long the work took" - about 350 s instead of 300 at
    500 devices - and that period is the denominator of every rate derived
    from a counter, as well as the floor on how late an alert can be. It also
    moved with the fleet's size and health, so the configured number told an
    operator very little. See docs/PERFORMANCE.md section 18.1.
    """
    while True:
        started = time.monotonic()
        try:
            await body()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"{name} cycle error: {e}")

        elapsed = time.monotonic() - started
        delay = interval - elapsed
        if delay <= 0:
            logger.warning(
                f"{name} cycle took {elapsed:.1f}s, longer than its "
                f"{interval:.0f}s interval. The real interval is the cycle "
                f"time, not the configured one - {overrun_hint}"
            )
            delay = OVERRUN_GAP_SECONDS
        await asyncio.sleep(delay)


async def _wait_for_signal() -> None:
    """
    Wait for SIGTERM or SIGINT, so that stopping the collector is orderly.

    Without a handler, SIGTERM - which is what `docker stop` and systemd send
    first - kills the interpreter where it stands: the trap port is not
    closed, the engine pools are not closed, and the leases this process holds
    are left to expire on their own. The fleet-wide phase would then be
    nobody's job for up to one TTL even though another shard was ready to take
    it over immediately.
    """
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    installed = []
    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            loop.add_signal_handler(number, stopping.set)
            installed.append(number)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass  # not a Unix event loop; the default handling applies
    try:
        await stopping.wait()
    finally:
        for number in installed:
            loop.remove_signal_handler(number)


async def main():
    """
    Main collector loop.

    Two loops, because the work has two natural frequencies:

      * the inventory loop (COLLECTOR_INTERVAL, 300 s) works out what is
        cabled to what, which changes when somebody moves a cable
      * the fast loop (COLLECTOR_FAST_INTERVAL, 60 s) works out whether those
        cables are up and how busy they are, and runs the alert checks

    and optionally a trap receiver, which hears about a port going down as it
    happens instead of at the next fast cycle.
    """
    interval = settings.collector_interval
    fast_interval = settings.collector_fast_interval

    logger.info(
        f"SNMP Collector starting (inventory interval: {interval}s, "
        + (f"fast interval: {fast_interval}s" if FAST_LOOP_ENABLED
           else "fast loop off")
        + (f", {SHARD.label()}, lease TTL {LEASE_TTL:.0f}s"
           if SHARD.enabled else "") + ")"
    )
    if SHARD.enabled:
        logger.info(
            f"Polling the devices whose id mod {SHARD.count} is "
            f"{SHARD.index}. Every shard from 0 to {SHARD.count - 1} has to "
            f"be running or those devices are polled by nobody."
        )
    # Alert damping, when any of it is on. An operator reading "device_offline
    # three minutes after the device died" has to be able to see from the log
    # that this is configured and not broken.
    damping = []
    if offline_confirm.enabled:
        damping.append(
            f"a device is called offline after {offline_confirm.cycles} "
            f"unanswered polls in a row (about "
            f"{offline_confirm.cycles * (fast_interval if FAST_LOOP_ENABLED else interval)}s)"
        )
    if flap_damper.enabled:
        damping.append(
            f"a cable that changes state {flap_damper.transitions} times in "
            f"{flap_damper.window:.0f}s keeps one alert instead of one per flap"
        )
    if settings.alert_trap_requires_poll:
        damping.append(
            "a trap writes the state immediately but leaves the alert to the "
            "next fast cycle (per device in the alert profile)"
        )
    for line in damping:
        logger.info(f"Alert damping: {line}")

    if FAST_LOOP_ENABLED and fast_interval >= interval:
        logger.warning(
            f"COLLECTOR_FAST_INTERVAL={fast_interval}s is not shorter than "
            f"COLLECTOR_INTERVAL={interval}s, so the fast loop buys no "
            f"freshness and only costs a second pass over the fleet"
        )

    # Initial wait for database to be ready
    await asyncio.sleep(5)
    await _prepare_database()

    trap_receiver = None
    if settings.collector_trap_enabled:
        trap_receiver = TrapReceiver(
            bind_host=settings.collector_trap_bind,
            port=settings.collector_trap_port,
            communities=settings.trap_community_list(),
            damper=flap_damper,
            requires_poll=settings.alert_trap_requires_poll,
            # Only the fast loop polls per-cable state, so it is also the
            # only thing that can confirm a trap (app/core/damping.py).
            poll_confirms_state=FAST_LOOP_ENABLED
        )
        if not await trap_receiver.start():
            logger.warning(
                "Continuing without trap reception: link state will be "
                "established by the fast loop only"
                + (". Only one process on a host can hold the trap port, so "
                   "with sharding this is expected on all but one of them - "
                   "the one that has it applies traps for the whole fleet, "
                   "not just for its own devices."
                   if SHARD.enabled else "")
            )
            trap_receiver = None

    async def inventory_cycle():
        if heartbeat is not None and not FAST_LOOP_ENABLED:
            # Whichever loop runs most often renews the heartbeat, because the
            # TTL is derived from that loop's interval
            await heartbeat.acquire()
        await poll_all_devices()
        # Engines whose credential nothing uses any more
        await inventory_pool.prune()
        await fast_pool.prune()
        if trap_receiver is not None:
            logger.info(f"Traps: {trap_receiver.stats()}")

    tasks = [asyncio.create_task(_fixed_cadence(
        "Inventory", interval, inventory_cycle,
        "either raise COLLECTOR_INTERVAL or run more shards "
        "(COLLECTOR_SHARDS, docs/PERFORMANCE.md section 24)."
    ), name="inventory")]

    if FAST_LOOP_ENABLED:
        poller = FastPoller(
            pool=fast_pool,
            concurrent=settings.collector_concurrent,
            backoff=fast_backoff,
            shard=SHARD,
            fleet_lease=fleet_lease,
            coverage=coverage,
            confirm=offline_confirm,
            damper=flap_damper
        )

        async def fast_cycle():
            if heartbeat is not None:
                await heartbeat.acquire()
            await poller.run_cycle()

        tasks.append(asyncio.create_task(_fixed_cadence(
            "Fast", fast_interval, fast_cycle,
            "either raise COLLECTOR_FAST_INTERVAL or run more shards "
            "(COLLECTOR_SHARDS, docs/PERFORMANCE.md section 24)."
        ), name="fast"))

    watched = set(tasks)
    try:
        # Neither loop ever returns, so this waits until one of them raises
        # something it could not handle, or the process is asked to stop.
        stopped = asyncio.create_task(_wait_for_signal(), name="shutdown")
        watched = set(tasks) | {stopped}
        done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            logger.info("Stopping on signal")
        else:
            for task in done:
                task.result()  # re-raise whatever took a loop down
    finally:
        for task in watched:
            task.cancel()
        # Retrieve the cancellations, or the event loop reports each of them
        # as an exception nobody looked at while the process is shutting down
        await asyncio.gather(*watched, return_exceptions=True)
        if trap_receiver is not None:
            await trap_receiver.stop()
        # Expire both leases on the way out so another shard picks the
        # fleet-wide phase up immediately instead of after a TTL, and so a
        # planned restart is not reported as a missing shard.
        if fleet_lease is not None:
            await fleet_lease.release()
        if heartbeat is not None:
            await heartbeat.release()
        await inventory_pool.aclose()
        await fast_pool.aclose()


if __name__ == "__main__":
    asyncio.run(main())
