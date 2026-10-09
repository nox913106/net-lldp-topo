"""
SNMP trap receiver: link state in milliseconds instead of minutes.

Polling can never beat its own interval. Even the fast loop
(app/core/fast_poll.py) only learns that a port went down when it next asks,
so the average delay on a link-down alert is half the fast interval plus the
exchange itself. A trap is the device telling us as it happens: a linkDown
notification arrives in milliseconds, and the alert is raised from the trap,
not from the next cycle.

What this is not: a replacement for polling. A trap is one unacknowledged UDP
datagram, so it can be lost, duplicated, delayed or forged. Everything here is
therefore arranged so that a trap can only ever be an *early* answer:

  * The state a trap writes is the same ``raw_links.oper_status`` column the
    fast loop writes, so the next fast cycle re-establishes the truth. A lost
    trap costs one fast interval of latency; a forged or stale one is undone
    within one fast interval.
  * Only traps whose source address is a managed device are acted on. Anything
    else is counted and dropped.
  * Only traps about a port with a cable recorded on it are acted on. An
    access port to a laptop going down is not this project's business.
  * v1 and v2c only. Their community is checked by pysnmp, which is worth
    about as much as a cleartext password in a UDP packet - hence the point
    above about a trap never being the only source of truth. SNMPv3
    notifications need per-device engine ids and USM users, which is a
    configuration surface this does not have.

Traffic has no equivalent: there is no "my counter just jumped" trap. Sub-
interval traffic alerting needs RMON alarms on the device, or sFlow, or
streaming telemetry.
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.alert_engine import AlertEngine
from app.core.damping import trap_requires_poll
from app.core.snmp_oids import (
    COLD_START_TRAP, IF_ADMIN_STATUS, IF_INDEX, IF_OPER_STATUS,
    LINK_DOWN_TRAP, LINK_UP_TRAP, SNMP_TRAP_OID, WARM_START_TRAP
)
from app.db.database import async_session_maker
from app.models.alert import AlertProfile
from app.models.device import Device
from app.models.link import RawLink

logger = logging.getLogger(__name__)

# Traps are dropped rather than queued once this many are waiting. A chassis
# that reboots sends one per port, and a flapping fibre can send thousands:
# the queue must not be allowed to grow until the collector is killed for
# using too much memory. What is lost is latency, not correctness - the fast
# loop still picks the state up.
MAX_QUEUE = 2000

# How many traps one database round trip handles. During a storm this is what
# turns 500 traps into a handful of transactions.
MAX_BATCH = 200

# How long the address -> device map is reused before it is read again
DEVICE_MAP_TTL_SECONDS = 60.0

# A trap from an unknown source should not be able to make the collector
# re-read the device table on every datagram
DEVICE_MAP_MISS_COOLDOWN_SECONDS = 5.0

# ifOperStatus to assume when the trap did not carry one
_STATUS_FROM_TRAP = {
    LINK_DOWN_TRAP: 2,  # down
    LINK_UP_TRAP: 1,    # up
}


class ManagedDevice(NamedTuple):
    """A device a trap can be about, as the source address map holds it"""

    id: int
    hostname: str
    # Which alert profile decides whether a trap about this device's cables
    # alerts immediately or waits for a poll to agree (app/core/damping.py)
    alert_profile_id: Optional[int]


class TrapEvent:
    """One parsed notification"""

    __slots__ = ("source_ip", "trap_oid", "if_index", "oper_status", "received_at")

    def __init__(
        self,
        source_ip: str,
        trap_oid: str,
        if_index: Optional[int],
        oper_status: Optional[int],
        received_at: float
    ):
        self.source_ip = source_ip
        self.trap_oid = trap_oid
        self.if_index = if_index
        self.oper_status = oper_status
        self.received_at = received_at


def parse_varbinds(var_binds) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """
    Pull (snmpTrapOID, ifIndex, ifOperStatus) out of a notification.

    IF-MIB defines linkDown and linkUp as carrying ifIndex, ifAdminStatus and
    ifOperStatus, but plenty of devices send only ifIndex, or add ifDescr, or
    send the index in the varbind's OID suffix and not as its value. All of
    those are read here; anything else in the notification is ignored.
    """
    trap_oid: Optional[str] = None
    if_index: Optional[int] = None
    oper_status: Optional[int] = None

    for var_bind in var_binds:
        oid = str(var_bind[0])
        value = var_bind[1]

        if oid == SNMP_TRAP_OID:
            trap_oid = _as_text(value)
            continue

        base, _, suffix = oid.rpartition(".")
        for name, base_oid in (
            ("index", IF_INDEX), ("oper", IF_OPER_STATUS), ("admin", IF_ADMIN_STATUS)
        ):
            if base != base_oid:
                continue
            number = _as_int(value)
            if name == "index":
                # The value is the ifIndex; the OID suffix is the same number
                # on a well behaved agent and the fallback on the rest.
                if_index = number if number is not None else _as_int(suffix)
            elif name == "oper":
                oper_status = number
                if if_index is None:
                    if_index = _as_int(suffix)
            elif if_index is None:
                if_index = _as_int(suffix)
            break

    return trap_oid, if_index, oper_status


def _as_int(value: Any) -> Optional[int]:
    """
    An int from whatever a device put in the varbind, or None.

    Every exception, not just the obvious two: this is parsing a datagram
    anybody can send, and pyasn1 raises its own error type for a value that
    carries no value at all (a bare Null, or a schema object).
    """
    try:
        return int(value)
    except Exception:
        return None


def _as_text(value: Any) -> Optional[str]:
    """Same, for the OID in snmpTrapOID.0"""
    try:
        return str(value)
    except Exception:
        return None


class TrapReceiver:
    """
    Listens for linkDown/linkUp notifications and turns them into link state.

    Started and stopped by the collector process. Receiving happens in a
    pysnmp callback, which is synchronous and must not block, so a trap is
    parsed there and everything that touches the database happens in an
    asyncio task draining a bounded queue.
    """

    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        port: int = 162,
        communities: Optional[List[str]] = None,
        damper=None,
        requires_poll: bool = False,
        poll_confirms_state: bool = True
    ):
        self.bind_host = bind_host
        self.port = port
        self.communities = [c for c in (communities or []) if c] or ["public"]
        # Shared with the fast loop so that a cable flapping across a trap and
        # a poll is one flapping cable (app/core/damping.py)
        self.damper = damper
        # ALERT_TRAP_REQUIRES_POLL: whether a trap's alert waits for the next
        # fast cycle to agree with it. Per device the alert profile overrides
        # it. The state write never waits - only the alert does.
        self.requires_poll = bool(requires_poll)
        # Whether anything ever polls per-cable state. With
        # COLLECTOR_FAST_INTERVAL=0 nothing does, so there is nothing for a
        # trap to be confirmed by and "wait for a poll" would mean "never
        # alert". Then every trap alerts immediately, and says so once.
        self.poll_confirms_state = bool(poll_confirms_state)
        self._warned_no_poll = False

        self._engine = None
        self._receiver = None
        self._queue: "asyncio.Queue[TrapEvent]" = asyncio.Queue(maxsize=MAX_QUEUE)
        self._worker: Optional[asyncio.Task] = None

        self._devices: Dict[str, ManagedDevice] = {}
        self._profiles: Dict[int, bool] = {}
        self._devices_loaded = 0.0
        self._unknown_sources: Set[str] = set()

        self.received = 0
        self.dropped = 0
        self.acted_on = 0
        self.ignored = 0
        self.deferred = 0

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> bool:
        """
        Bind the trap port and start handling notifications.

        Returns False, with a reason logged, when the port cannot be bound -
        udp/162 is privileged, so this is the normal outcome of running the
        collector as an unprivileged user without CAP_NET_BIND_SERVICE, and it
        must not stop the rest of the collector from working.
        """
        from pysnmp.carrier.asyncio.dgram import udp
        from pysnmp.entity import config as snmp_config, engine as snmp_engine
        from pysnmp.entity.rfc3413 import ntfrcv

        try:
            engine = snmp_engine.SnmpEngine()
            transport = udp.UdpTransport().openServerMode(
                (self.bind_host, self.port)
            )
            snmp_config.addTransport(engine, udp.domainName + (1,), transport)
            for index, community in enumerate(self.communities):
                snmp_config.addV1System(engine, f"trap-area-{index}", community)
            self._receiver = ntfrcv.NotificationReceiver(engine, self._on_trap)
            engine.transportDispatcher.jobStarted(1)
        except Exception as e:
            logger.error(
                f"Trap receiver could not start on {self.bind_host}:{self.port}: {e}"
            )
            return False

        self._engine = engine

        # openServerMode() creates the socket through a future rather than
        # binding inline, so "address already in use" and "permission denied"
        # surface here and nowhere else.
        if not await self._confirm_bound(transport):
            await self.stop()
            return False

        self._worker = asyncio.create_task(self._drain())
        logger.info(
            f"Trap receiver listening on udp/{self.port} "
            f"({len(self.communities)} community string(s), v1/v2c)"
        )
        return True

    async def _confirm_bound(self, transport) -> bool:
        """Give the socket a moment to fail, and report it if it did"""
        pending = getattr(transport, "_lport", None)
        if pending is None:
            return True
        for _ in range(20):
            if getattr(pending, "done", None) and pending.done():
                break
            await asyncio.sleep(0.05)
        try:
            if pending.done() and pending.exception() is not None:
                logger.error(
                    f"Trap receiver could not bind "
                    f"{self.bind_host}:{self.port}: {pending.exception()}"
                )
                return False
        except Exception:  # pragma: no cover - depends on pysnmp internals
            return True
        return True

    async def stop(self) -> None:
        """Stop handling notifications and release the port"""
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

        engine, self._engine = self._engine, None
        if engine is not None:
            try:
                if self._receiver is not None:
                    self._receiver.close(engine)
                engine.transportDispatcher.jobFinished(1)
                engine.closeDispatcher()
            except Exception as e:  # pragma: no cover - best effort cleanup
                logger.debug(f"Trap receiver shutdown: {e}")
        self._receiver = None

    # -- receiving --------------------------------------------------------

    def _on_trap(
        self, engine, state_reference, context_engine_id, context_name,
        var_binds, cb_ctx
    ) -> None:
        """
        pysnmp callback. Synchronous, on the event loop - so it parses and
        queues, and does nothing that can block or raise.
        """
        self.received += 1
        try:
            source_ip = self._source_address(engine)
            trap_oid, if_index, oper_status = parse_varbinds(var_binds)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Unparsable trap: {e}")
            return

        if source_ip is None or trap_oid is None:
            self.ignored += 1
            return

        if trap_oid in (COLD_START_TRAP, WARM_START_TRAP):
            # A reboot resets the interface counters. Nothing to do: a counter
            # that went backwards already yields no rate (app/core/traffic.py).
            logger.info(f"Device at {source_ip} reports a restart ({trap_oid})")
            self.ignored += 1
            return

        if trap_oid not in _STATUS_FROM_TRAP:
            self.ignored += 1
            logger.debug(f"Ignoring trap {trap_oid} from {source_ip}")
            return

        if if_index is None:
            self.ignored += 1
            logger.warning(
                f"Link trap {trap_oid} from {source_ip} carried no ifIndex"
            )
            return

        event = TrapEvent(
            source_ip=source_ip,
            trap_oid=trap_oid,
            if_index=if_index,
            oper_status=oper_status,
            received_at=time.monotonic()
        )
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 100 == 1:
                logger.warning(
                    f"Trap queue full, dropped {self.dropped} traps - the fast "
                    f"loop will still pick the state up"
                )

    @staticmethod
    def _source_address(engine) -> Optional[str]:
        """The address the notification came from, per pysnmp's observer API"""
        context = engine.observer.getExecutionContext(
            "rfc3412.receiveMessage:request"
        )
        address = context.get("transportAddress") if context else None
        if not address:
            return None
        return str(address[0])

    # -- handling ---------------------------------------------------------

    async def _drain(self) -> None:
        """Handle queued traps, in batches during a storm"""
        while True:
            batch = [await self._queue.get()]
            while len(batch) < MAX_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._handle(batch)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Handling {len(batch)} trap(s) failed: {e}")

    async def _handle(self, batch: List[TrapEvent]) -> None:
        """
        Apply a batch of link traps: write the state, then raise or clear the
        alerts for exactly the cables involved.
        """
        # Last trap for a port wins: during a flap the newest state is the one
        # worth storing, and the fast loop corrects it either way.
        latest: Dict[Tuple[str, int], TrapEvent] = {}
        for event in batch:
            latest[(event.source_ip, event.if_index)] = event

        async with async_session_maker() as db:
            devices = await self._device_map(db, {ip for ip, _ in latest})

            wanted: Dict[Tuple[int, int], TrapEvent] = {}
            by_device: Dict[int, ManagedDevice] = {}
            for (source_ip, if_index), event in latest.items():
                device = devices.get(source_ip)
                if device is None:
                    self.ignored += 1
                    if source_ip not in self._unknown_sources:
                        self._unknown_sources.add(source_ip)
                        logger.warning(
                            f"Ignoring traps from {source_ip}: not a managed "
                            f"device (it is matched on its management address)"
                        )
                    continue
                wanted[(device.id, if_index)] = event
                by_device[device.id] = device

            if not wanted:
                return

            rows = (await db.execute(
                select(RawLink.id, RawLink.local_device_id,
                       RawLink.local_port_index, RawLink.local_port)
                .where(RawLink.local_device_id.in_({d for d, _ in wanted}))
                .where(RawLink.local_port_index.in_({i for _, i in wanted}))
            )).all()

            now = datetime.utcnow()
            updates: List[Dict[str, Any]] = []
            touched: List[int] = []
            deferred: List[int] = []
            for row in rows:
                event = wanted.get((row.local_device_id, row.local_port_index))
                if event is None:
                    continue  # another device's port happens to share the index
                status = event.oper_status
                if status is None:
                    status = _STATUS_FROM_TRAP[event.trap_oid]
                updates.append({
                    "id": row.id,
                    "oper_status": status,
                    "oper_status_at": now,
                })
                # The state is written either way - the topology and the UI
                # are current within milliseconds whatever the policy is.
                # What the policy decides is whether the *alert* is raised
                # from the notification or from the next poll that agrees
                # with it (app/core/damping.py).
                if self._requires_poll(by_device.get(row.local_device_id)):
                    deferred.append(row.id)
                else:
                    touched.append(row.id)

            if not updates:
                self.ignored += len(wanted)
                logger.debug(
                    f"{len(wanted)} link trap(s) about ports with no cable "
                    f"recorded on them"
                )
                return

            try:
                # Ascending primary key order, like every other bulk write to
                # this table, so a trap storm cannot deadlock against a cycle
                updates.sort(key=lambda row: row["id"])
                await db.execute(update(RawLink), updates)
                events = []
                if touched:
                    engine = AlertEngine(db, damper=self.damper)
                    events = await engine.check_link_states(raw_link_ids=touched)
                    await engine.process_events(events)
                else:
                    # Nothing to alert on, but the state write still has to
                    # be committed - process_events is what commits in the
                    # branch above.
                    await db.commit()
            except SQLAlchemyError as e:
                await db.rollback()
                logger.error(f"Storing link traps failed: {e}")
                return

            self.acted_on += len(updates)
            self.deferred += len(deferred)
            latency = max(
                (time.monotonic() - event.received_at for event in batch),
                default=0.0
            )
            logger.info(
                f"Link traps: {len(updates)} cable(s) updated from "
                f"{len(batch)} trap(s), {len(events)} alert event(s)"
                + (f", {len(deferred)} waiting for a poll to confirm"
                   if deferred else "")
                + f", {latency * 1000:.0f} ms from receipt"
            )

    def _requires_poll(self, device: Optional[ManagedDevice]) -> bool:
        """
        Whether a trap about this device's cables waits for a poll to agree.

        The profile decides, falling back to ALERT_TRAP_REQUIRES_POLL. With
        the fast loop switched off nothing ever polls per-cable state, so
        waiting would mean never alerting: that configuration alerts
        immediately and says so once rather than silently swallowing traps.
        """
        if device is None:
            return False
        wanted = self.requires_poll
        if device.alert_profile_id is not None:
            wanted = self._profiles.get(device.alert_profile_id, wanted)
        if wanted and not self.poll_confirms_state:
            if not self._warned_no_poll:
                self._warned_no_poll = True
                logger.warning(
                    "A trap is set to wait for a poll to confirm it, but "
                    "COLLECTOR_FAST_INTERVAL=0 means per-cable link state is "
                    "never polled - there is nothing to confirm it with. "
                    "Traps will alert immediately."
                )
            return False
        return wanted

    async def _device_map(self, db, sources: Set[str]) -> Dict[str, ManagedDevice]:
        """
        {management address: ManagedDevice}, cached.

        Re-read when it is stale, and - no more often than the cooldown - when
        a trap arrives from an address that is not in it, so a device added a
        moment ago does not have to wait out the TTL.

        The alert profiles are read in the same pass: there are a handful of
        them, they decide one boolean per device, and reading them per trap
        would be a query per datagram during a storm.
        """
        now = time.monotonic()
        age = now - self._devices_loaded
        missing = sources - set(self._devices)

        if self._devices and age < DEVICE_MAP_TTL_SECONDS and (
            not missing or age < DEVICE_MAP_MISS_COOLDOWN_SECONDS
        ):
            return self._devices

        rows = (await db.execute(select(
            Device.id, Device.hostname, Device.ip_address, Device.alert_profile_id
        ))).all()
        self._devices = {
            ip_address: ManagedDevice(device_id, hostname, profile_id)
            for device_id, hostname, ip_address, profile_id in rows if ip_address
        }
        profiles = (await db.execute(
            select(AlertProfile.id, AlertProfile.thresholds)
        )).all()
        self._profiles = {
            profile_id: trap_requires_poll(thresholds, self.requires_poll)
            for profile_id, thresholds in profiles
        }
        self._devices_loaded = now
        # An address that has since been added as a device is no longer
        # unknown; the rest stay suppressed, because a device that is not
        # managed will keep sending traps and one warning is enough.
        self._unknown_sources -= set(self._devices)
        return self._devices

    def stats(self) -> str:
        return (
            f"{self.received} received, {self.acted_on} applied, "
            f"{self.ignored} ignored, {self.dropped} dropped"
            + (f", {self.deferred} left to a poll to alert on"
               if self.deferred else "")
        )
