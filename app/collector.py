"""
Background collector service
Runs periodic SNMP polling of all managed devices
Integrates with Alert Engine and Topology Engine
"""
import asyncio
import logging
import socket
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.config import get_settings
from app.db.database import async_session_maker, init_db
from app.core.snmp_collector import SNMPCollector, InterfaceStats
from app.core.poll_scheduler import OfflineBackoff
from app.core.topology_engine import TopologyEngine
from app.core.alert_engine import AlertEngine
from app.core.discovery_cache import DiscoveryCache
from app.core.traffic import counter_rate
from app.core.log_exporter import get_log_exporter, LogLevel
from app.models.device import Device, DeviceStatus
from app.models.link import RawLink

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Devices that do not answer SNMP are probed less and less often instead of
# costing a full timeout on every OID of every cycle. The state lives in this
# process only - see app/core/poll_scheduler.py for why.
offline_backoff = OfflineBackoff(
    base_interval=settings.collector_interval,
    max_interval=settings.collector_offline_backoff_max,
    enabled=settings.collector_offline_backoff
)

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


def _apply_traffic(
    raw_link: RawLink,
    stat: Optional[InterfaceStats],
    sampled_at: datetime
) -> None:
    """
    Record the port's real capacity and traffic rate on the link row.

    bandwidth_mbps is ifHighSpeed as the device reports it, which replaces
    guessing the speed from the port name. in_bps/out_bps are derived from this
    sample and the one stored on the row by the previous cycle, so they stay
    None until a second sample exists.
    """
    if stat is None:
        return

    if stat.speed_mbps > 0:
        raw_link.bandwidth_mbps = stat.speed_mbps

    if stat.in_octets is None and stat.out_octets is None:
        # Traffic collection is off. Drop the rates rather than leave the last
        # ones frozen in place - a rate from an unknown time ago is not a
        # measurement. The counter samples are kept, so switching collection
        # back on resumes from them instead of inventing a rate across the gap
        # (counter_rate rejects a gap that long anyway).
        raw_link.in_bps = None
        raw_link.out_bps = None
        return

    raw_link.in_bps = counter_rate(
        raw_link.in_octets, raw_link.sampled_at, stat.in_octets, sampled_at
    )
    raw_link.out_bps = counter_rate(
        raw_link.out_octets, raw_link.sampled_at, stat.out_octets, sampled_at
    )
    raw_link.in_octets = stat.in_octets
    raw_link.out_octets = stat.out_octets
    raw_link.sampled_at = sampled_at


async def poll_all_devices():
    """Poll all managed devices"""
    async with async_session_maker() as db:
        # Get all managed devices
        result = await db.execute(
            select(Device).where(Device.status != DeviceStatus.EXCLUDED)
        )
        devices = result.scalars().all()

        # Refresh what auto-discovery already knows. Every device is listed,
        # excluded ones included - otherwise an excluded device gets
        # rediscovered and re-added on the next cycle.
        known = await db.execute(select(Device.hostname, Device.ip_address))
        discovery_cache.seed(known.all())

    # Forget devices that no longer exist so the backoff state cannot grow
    offline_backoff.prune(device.id for device in devices)

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
            # Use device-specific community, fallback to default
            community = device.snmp_community or settings.snmp_default_community
            # One collector - and therefore one SNMP engine and one UDP
            # socket - per device, released as soon as the device is done
            async with SNMPCollector(
                community=community,
                timeout=settings.snmp_timeout,
                retries=settings.snmp_retries,
                snmp_version=device.snmp_version or "v2c",
                v3_username=device.snmpv3_username,
                v3_auth_protocol=device.snmpv3_auth_protocol,
                v3_auth_password=device.snmpv3_auth_password,
                v3_priv_protocol=device.snmpv3_priv_protocol,
                v3_priv_password=device.snmpv3_priv_password,
                collect_traffic=settings.collector_traffic_stats
            ) as collector:
                # Talk to the device first, with no database connection held.
                # An unreachable device costs several seconds of timeouts, and
                # holding a connection across that would pin
                # COLLECTOR_CONCURRENT connections for the whole SNMP phase.
                result = await fetch_device_state(collector, device)

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
                        collector, tracked, result, db
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

    tasks = [poll_with_semaphore(d) for d in due]
    # One device blowing up must not abandon the rest of the cycle
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    for device, outcome in zip(due, outcomes):
        if isinstance(outcome, BaseException):
            logger.error(f"Polling {device.hostname} raised: {outcome}")

    async with async_session_maker() as db:
        # Update merged links
        logger.info("Updating merged links...")
        topology_engine = TopologyEngine(db)
        await topology_engine.update_merged_links_in_db()

        # Run alert checks
        logger.info("Running alert checks...")
        alert_engine = AlertEngine(db)
        await alert_engine.run_check_cycle()

    logger.info(f"Poll cycle completed, discovery cache: {discovery_cache.stats()}")


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
    db
) -> bool:
    """
    The database half of a poll: write what SNMP returned.

    `device` must belong to `db`. Returns True when the device answered SNMP,
    so the caller can keep the offline backoff up to date.
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

            # Update raw links
            for neighbor in result["lldp_neighbors"] + result["cdp_neighbors"]:
                # Check if link already exists
                existing = await db.execute(
                    select(RawLink).where(
                        RawLink.local_device_id == device.id,
                        RawLink.local_port == neighbor.local_port,
                        RawLink.remote_hostname == neighbor.remote_hostname
                    )
                )
                raw_link = existing.scalar_one_or_none()

                if raw_link:
                    # Refresh the details, they can change (and older versions
                    # of the collector stored unusable values here)
                    raw_link.local_port_index = neighbor.local_port_index
                    raw_link.remote_port = neighbor.remote_port
                    raw_link.remote_chassis_id = neighbor.remote_chassis_id
                    raw_link.protocol = neighbor.protocol
                    raw_link.last_seen = datetime.utcnow()
                else:
                    raw_link = RawLink(
                        local_device_id=device.id,
                        local_port=neighbor.local_port,
                        local_port_index=neighbor.local_port_index,
                        remote_hostname=neighbor.remote_hostname,
                        remote_port=neighbor.remote_port,
                        remote_chassis_id=neighbor.remote_chassis_id,
                        protocol=neighbor.protocol
                    )
                    db.add(raw_link)

                    # Log new link discovery
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

                stat = _match_interface(neighbor, by_index, by_name)
                if stat is not None:
                    matched += 1
                _apply_traffic(raw_link, stat, sampled_at)

                # Auto-discover neighbor devices if enabled
                if device.auto_discover:
                    await auto_discover_neighbor(
                        device.id, device.hostname, neighbor, collector, log_exporter
                    )

            neighbor_count = len(result["lldp_neighbors"]) + len(result["cdp_neighbors"])
            logger.info(
                f"Polled {device.hostname} ({device.ip_address}): "
                f"{len(result['lldp_neighbors'])} LLDP + "
                f"{len(result['cdp_neighbors'])} CDP neighbors, "
                f"{len(result['interface_stats'])} interfaces, "
                f"{matched}/{neighbor_count} links matched to an interface"
            )
            return True
        else:
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
        logger.error(f"Error storing poll result for {device.hostname}: {e}")
        device.status = DeviceStatus.OFFLINE
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


async def main():
    """Main collector loop"""
    logger.info(f"SNMP Collector starting (interval: {settings.collector_interval}s)")

    # Initial wait for database to be ready
    await asyncio.sleep(5)
    await _prepare_database()

    while True:
        try:
            await poll_all_devices()
        except Exception as e:
            logger.error(f"Poll cycle error: {e}")

        await asyncio.sleep(settings.collector_interval)


if __name__ == "__main__":
    asyncio.run(main())
