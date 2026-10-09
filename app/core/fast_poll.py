"""
The fast loop: liveness, per-cable link state, and traffic.

One poll cycle used to do two jobs with completely different natural
frequencies. Working out **what is cabled to what** means walking the LLDP and
CDP tables, the local port tables and the interface tables - around 17 table
walks and 500 varbinds per device - and the answer changes when somebody
moves a cable. Working out **whether those cables are up and how busy they
are** means reading three values per cable, and the answer changes by the
second.

Tying them to one interval buys the frequency of the fast job at the price of
the slow one, which is why a 300 second interval meant a 6.4 minute average
delay on a utilisation alert, and why the shortest burst that could ever cross
a 70% threshold was 210 seconds long (docs/PERFORMANCE.md section 18).

This loop does the fast half only, and it does not walk anything: the
inventory poll has already recorded the ifIndex of every cable in
``raw_links.local_port_index``, so this is GETs against exactly the ports that
have a cable in them - about 3 varbinds per cable, against the ~500 a full
inventory poll walks.

What it deliberately does not do:

  * CPU and memory. The vendor OIDs for those are themselves five minute
    averages (``cpuUsage5min`` and friends), so asking for them every minute
    adds load and no information. They stay on the inventory loop.
  * ifHighSpeed. A port's capacity is inventory: it changes when somebody
    reconfigures the port, not while traffic flows.
  * Anything that writes the inventory columns of raw_links. The two loops own
    disjoint column sets (app/core/link_index.py), which is what lets them run
    at the same time without undoing each other.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.core.alert_engine import AlertEngine
from app.core.link_index import CabledPort, LinkSampleBuffer, load_cabled_ports
from app.core.snmp_collector import InterfaceStats
from app.core.topology_engine import TopologyEngine
from app.core.traffic import sample_values
from app.db.database import async_session_maker
from app.models.device import Device, DeviceStatus

logger = logging.getLogger(__name__)


@dataclass
class _DeviceOutcome:
    """What one device's fast sample produced, before anything is written"""
    device_id: int
    reachable: bool
    sampled_at: datetime
    # (link_id, column values, whether oper_status changed)
    samples: List[Tuple[int, Dict[str, Any], bool]] = field(default_factory=list)
    transitions: List[str] = field(default_factory=list)


class BulkUpdate:
    """
    UPDATE ... WHERE id = :id for many rows, as one statement per column set.

    Rows are grouped by which columns they carry, because an executemany needs
    every parameter set to have the same keys. That is what lets a device that
    answered write its status *and* its last_seen while a device that did not
    answer writes only its status - rather than one uniform statement that
    would have to overwrite last_seen with NULL to keep the shape.
    """

    def __init__(self, model):
        self._model = model
        self._groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}

    def add(self, pk: int, values: Dict[str, Any]) -> None:
        key = tuple(sorted(values))
        row = dict(values)
        row["id"] = pk
        self._groups.setdefault(key, []).append(row)

    @property
    def pending(self) -> int:
        return sum(len(rows) for rows in self._groups.values())

    async def flush(self, db) -> int:
        written = self.pending
        for rows in self._groups.values():
            # Ascending primary key order, so two transactions that touch the
            # same rows take their row locks in the same order and cannot
            # deadlock against each other.
            rows.sort(key=lambda row: row["id"])
            await db.execute(update(self._model), rows)
        self._groups.clear()
        return written


class FastPoller:
    """
    One fast cycle: sample every cabled port, write what changed, refresh the
    merged links and run the alert checks.

    The alert checks live here rather than on the inventory loop because an
    alert is only ever as fresh as the loop that evaluates it. Leaving
    ``run_check_cycle()`` on a 300 second loop would have made the whole
    exercise pointless.
    """

    def __init__(
        self,
        pool,
        concurrent: int,
        backoff,
        shard=None,
        fleet_lease=None,
        coverage=None
    ):
        self.pool = pool
        self.concurrent = max(1, int(concurrent))
        self.backoff = backoff
        # This process's slice of the fleet, or None for all of it
        # (app/core/sharding.py)
        self.shard = shard
        # Held by exactly one shard. The device sampling above needs no
        # coordination - a device has one owner - but the merge and the alert
        # cycle read the whole fleet and must happen once per interval rather
        # than once per shard (app/core/lease.py).
        self.fleet_lease = fleet_lease
        self.coverage = coverage
        self._was_holder = False

    # -- one device -------------------------------------------------------

    async def _sample_device(
        self, device: Device, ports: List[CabledPort]
    ) -> _DeviceOutcome:
        """SNMP half: no database connection is held while this runs"""
        try:
            async with self.pool.collector_for(device) as collector:
                samples = await collector.get_port_samples(
                    device.ip_address, [port.port_index for port in ports]
                )
        except Exception as e:
            logger.error(f"Fast sample of {device.hostname} raised: {e}")
            samples = None

        sampled_at = datetime.utcnow()
        outcome = _DeviceOutcome(
            device_id=device.id,
            reachable=samples is not None,
            sampled_at=sampled_at
        )

        for port in ports:
            sample = samples.get(port.port_index) if samples is not None else None

            # An unreachable device, a port the agent has no answer for and a
            # port that has been removed all mean the same thing here: the
            # state is not known. That is not "down" and not "up", and it is
            # stored as NULL so nothing downstream can mistake a reading from
            # an hour ago for the current state.
            status = sample.oper_status if sample is not None else None

            # sample_values() is the rate logic the inventory poll uses, with
            # its edge cases (first sample, counter reset, gap too long or too
            # short) already covered by tests. speed_mbps is 0 because
            # capacity is not this loop's business: it keeps whatever the
            # inventory poll stored.
            stat = None
            if sample is not None and (
                sample.in_octets is not None or sample.out_octets is not None
            ):
                stat = InterfaceStats(
                    port_name="",
                    port_index=port.port_index,
                    speed_mbps=0,
                    in_octets=sample.in_octets,
                    out_octets=sample.out_octets
                )

            values = sample_values(port, stat, sampled_at)
            values["oper_status"] = status
            values["oper_status_at"] = sampled_at

            changed = status != port.oper_status
            if changed and port.oper_status is not None:
                outcome.transitions.append(
                    f"{device.hostname} {port.local_port} "
                    f"{_status_name(port.oper_status)} -> {_status_name(status)}"
                )
            outcome.samples.append((port.link_id, values, changed))

        return outcome

    # -- the fleet-wide phase ---------------------------------------------

    async def _own_fleet_phase(self) -> bool:
        """
        Whether this process does the merge and the alert checks this cycle.

        Always true when there is no lease, which is the unsharded case: one
        process, nothing to coordinate with, and no new way for the alert
        checks to be skipped.
        """
        if self.fleet_lease is None:
            return True

        held = await self.fleet_lease.acquire()
        if held != self._was_holder:
            if held:
                logger.info(
                    "This shard now runs the fleet-wide phase "
                    "(merged links and alert checks)"
                )
            else:
                logger.info(
                    "Another shard is running the fleet-wide phase; this one "
                    "polls its own devices only"
                )
            self._was_holder = held

        if held and self.coverage is not None:
            await self.coverage.check()
        return held

    # -- one cycle --------------------------------------------------------

    async def run_cycle(self) -> float:
        """Run one fast cycle. Returns how long it took, in seconds."""
        started = time.monotonic()

        async with async_session_maker() as db:
            statement = select(Device).where(Device.status != DeviceStatus.EXCLUDED)
            if self.shard is not None:
                statement = self.shard.scope(statement, Device.id)
            devices = (await db.execute(statement)).scalars().all()
            # The work list: every cable whose ifIndex is known, in one query
            ports_by_device = await load_cabled_ports(db, shard=self.shard)

        self.backoff.prune(device.id for device in devices)
        due = [d for d in devices if self.backoff.should_poll(d.id)]
        cables = sum(len(ports_by_device.get(d.id, ())) for d in due)

        semaphore = asyncio.Semaphore(self.concurrent)

        async def with_semaphore(device: Device) -> _DeviceOutcome:
            async with semaphore:
                return await self._sample_device(
                    device, ports_by_device.get(device.id, [])
                )

        sample_started = time.monotonic()
        outcomes = await asyncio.gather(
            *[with_semaphore(device) for device in due], return_exceptions=True
        )
        snmp_seconds = time.monotonic() - sample_started

        links = LinkSampleBuffer()
        device_writes = BulkUpdate(Device)
        unreachable = 0
        transitions: List[str] = []

        for device, outcome in zip(due, outcomes):
            if isinstance(outcome, BaseException):
                # One device must never cost the rest of the cycle its data
                logger.error(f"Fast sample of {device.hostname} failed: {outcome}")
                continue

            if outcome.reachable:
                self.backoff.record_success(outcome.device_id)
                device_writes.add(outcome.device_id, {
                    # .value, not the enum member: this goes straight into a
                    # bound parameter rather than through the ORM's type
                    "status": DeviceStatus.MANAGED.value,
                    "last_seen": outcome.sampled_at,
                })
            else:
                unreachable += 1
                self.backoff.record_failure(outcome.device_id)
                device_writes.add(
                    outcome.device_id, {"status": DeviceStatus.OFFLINE.value}
                )

            for link_id, values, changed in outcome.samples:
                links.set(link_id, values, state_changed=changed)
            transitions.extend(outcome.transitions)

        for line in transitions:
            logger.info(f"Link state: {line}")

        written = changes = 0
        merge_seconds = alert_seconds = 0.0
        fleet_phase = True
        async with async_session_maker() as db:
            try:
                written, changes = await links.flush(db)
                await device_writes.flush(db)
                await db.commit()
            except SQLAlchemyError as e:
                await db.rollback()
                logger.error(f"Writing the fast cycle failed: {e}")

            # Utilisation lives on merged_links, and an alert can only be as
            # fresh as the number it reads, so the merge belongs to whichever
            # loop the alert checks run on.
            #
            # Both read the whole fleet, so in a sharded deployment they are
            # one shard's job, not every shard's. Which one is decided by a
            # lease rather than by configuration ("shard 0 does it") so that
            # losing that process does not stop alerting altogether.
            fleet_phase = await self._own_fleet_phase()
            if fleet_phase:
                merge_started = time.monotonic()
                try:
                    await TopologyEngine(db).update_merged_links_in_db()
                except SQLAlchemyError as e:
                    await db.rollback()
                    logger.error(f"Refreshing merged links failed: {e}")
                merge_seconds = time.monotonic() - merge_started

                alert_started = time.monotonic()
                try:
                    await AlertEngine(db).run_check_cycle()
                except Exception as e:
                    # run_check_cycle() has no internal guard, and a failure
                    # here used to take the whole cycle down (18.5 of
                    # docs/PERFORMANCE.md)
                    await db.rollback()
                    logger.error(f"Alert checks failed: {e}")
                alert_seconds = time.monotonic() - alert_started

        total = time.monotonic() - started
        tail = (
            f", merge {merge_seconds:.1f}s, alerts {alert_seconds:.1f}s"
            if fleet_phase else ", fleet-wide phase held by another shard"
        )
        logger.info(
            f"Fast cycle: {len(due)} devices, {cables} cables in "
            f"{total:.1f}s (snmp {snmp_seconds:.1f}s{tail}), "
            f"{written} cables written ({changes} state changes), "
            f"{unreachable} not answering, {self.pool.stats()}"
        )
        return total


_STATUS_NAMES = {
    1: "up", 2: "down", 3: "testing", 4: "unknown",
    5: "dormant", 6: "notPresent", 7: "lowerLayerDown",
}


def _status_name(status: Optional[int]) -> str:
    """ifOperStatus as a word, for log lines humans read"""
    if status is None:
        return "unknown"
    return _STATUS_NAMES.get(status, str(status))
