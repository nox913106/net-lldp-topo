"""
Per-cycle raw_links index and batched writers, for both of the collector's
loops.

The columns of a raw_links row have two owners. The inventory loop owns what
is cabled where and how fast the port is; the fast loop owns the counter
sample, the rate derived from it and the port's link state. They run at
different intervals and at the same time, so each writes its own columns and
leaves the other's alone - otherwise the inventory loop's five-minute-old
traffic numbers would periodically overwrite the fast loop's fresh ones. The
column groups below are where that split is defined.

A poll cycle touches every cable in the fleet exactly once, and it already
knows which ones it is going to touch. Doing that one row at a time costs two
statements per cable - a SELECT to find the row and an UPDATE to write it -
which at 500 devices and ~10,000 cables is ~20,000 round trips to the database
per cycle. Both halves are avoidable:

  * RawLinkIndex reads the whole lookup table in one statement at the start of
    the cycle and answers the "which row is this cable?" question from process
    memory.
  * LinkWriteBuffer collects a device's writes and sends them as two
    statements (one executemany UPDATE, one executemany INSERT) instead of two
    per cable.

See docs/PERFORMANCE.md sections 17 and 20.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy import insert, select, update

from app.models.link import RawLink

logger = logging.getLogger(__name__)

# (local_device_id, local_port, remote_hostname) - the three columns the
# collector has always identified a cable by, and the columns
# ix_raw_links_lookup covers. The strings are kept exactly as stored: the
# SELECT this replaces compared them with SQL `=`, which is case and
# whitespace sensitive, so normalising them here would silently merge rows
# that the database treats as different.
LinkKey = Tuple[int, Optional[str], Optional[str]]


class LinkSnapshot(NamedTuple):
    """What the previous cycle left on a raw_links row.

    Only the fields a new sample needs: the row id to write back to, and the
    previous counter sample, without which a rate cannot be derived.
    """
    id: int
    bandwidth_mbps: Optional[int]
    in_octets: Optional[int]
    out_octets: Optional[int]
    sampled_at: Optional[datetime]


class RawLinkIndex:
    """
    The raw_links lookup table, held in collector process memory.

    Loaded once per cycle rather than kept across cycles on purpose. Keeping it
    would save one query per cycle (~50 ms of a ~5 s database phase) and buy a
    whole class of bug in exchange: the API deletes raw links when a device is
    removed, and a stale index would then point UPDATEs at rows that no longer
    exist - which affects zero rows and raises nothing, so the cable would
    simply stop being recorded, silently, until the process restarted. Reading
    it fresh each cycle keeps the collector correct against anything else that
    writes the table, and the 10,000 individual SELECTs are gone either way.
    """

    def __init__(self) -> None:
        self._by_key: Dict[LinkKey, LinkSnapshot] = {}

    async def load(self, db, shard=None) -> int:
        """
        Replace the index with the current contents of raw_links.

        ``shard`` limits it to the cables of the devices this process polls
        (app/core/sharding.py). It is the devices that are sharded, so the
        filter is on the cable's local device - that way a shard's cables and
        its devices are always the same slice, and a cycle never holds the
        other shards' 10,000 rows in memory to throw them away.
        """
        statement = select(
            RawLink.id,
            RawLink.local_device_id,
            RawLink.local_port,
            RawLink.remote_hostname,
            RawLink.bandwidth_mbps,
            RawLink.in_octets,
            RawLink.out_octets,
            RawLink.sampled_at,
        )
        if shard is not None:
            statement = shard.scope(statement, RawLink.local_device_id)
        result = await db.execute(statement)
        by_key: Dict[LinkKey, LinkSnapshot] = {}
        for row in result:
            by_key[(row.local_device_id, row.local_port, row.remote_hostname)] = (
                LinkSnapshot(
                    id=row.id,
                    bandwidth_mbps=row.bandwidth_mbps,
                    in_octets=row.in_octets,
                    out_octets=row.out_octets,
                    sampled_at=row.sampled_at,
                )
            )
        self._by_key = by_key
        return len(by_key)

    def get(
        self,
        local_device_id: int,
        local_port: Optional[str],
        remote_hostname: Optional[str]
    ) -> Optional[LinkSnapshot]:
        return self._by_key.get((local_device_id, local_port, remote_hostname))

    def __len__(self) -> int:
        return len(self._by_key)


# Every row in one executemany must carry the same keys, so these are written
# on every update whether or not the new sample changed them.
#
# The columns are grouped by which loop owns them, because the two loops run
# on different intervals and must not undo each other's work:
#
#   * inventory  - what is cabled where, and how fast the port is. Changes
#                  when somebody moves a cable. Written by the inventory poll.
#   * traffic    - the counter sample and the rate derived from it. Written by
#                  whichever loop is sampling counters: the fast loop when it
#                  is running, the inventory poll when it is not.
#   * state      - ifOperStatus. Written by the fast loop and by the trap
#                  receiver, never by the inventory poll.
_INVENTORY_COLUMNS = (
    "local_port_index",
    "remote_port",
    "remote_chassis_id",
    "protocol",
    "last_seen",
    "bandwidth_mbps",
)

_TRAFFIC_COLUMNS = (
    "in_octets",
    "out_octets",
    "in_bps",
    "out_bps",
    "sampled_at",
)

_STATE_COLUMNS = (
    "oper_status",
    "oper_status_at",
)

_INSERT_COLUMNS = (
    "local_device_id",
    "local_port",
    "remote_hostname",
) + _INVENTORY_COLUMNS + _TRAFFIC_COLUMNS


class LinkWriteBuffer:
    """
    One device's raw_links writes from an inventory poll, sent as two
    statements.

    Keyed rather than appended: a device that advertises the same neighbour
    twice on the same port used to end up with one row (the second pass found
    the first write and overwrote it), and that stays true here.

    ``write_traffic`` is off while the fast loop is running. The counter
    sample and the rate on a row then belong to the fast loop, and an
    inventory poll that wrote its own - five minutes stale, and computed
    against whatever the fast loop had already stored - would overwrite a
    fresher measurement with a worse one, and clear in_bps/out_bps every time
    traffic collection is off on this half of the collector.
    """

    def __init__(self, write_traffic: bool = True) -> None:
        self._update_columns = _INVENTORY_COLUMNS + (
            _TRAFFIC_COLUMNS if write_traffic else ()
        )
        self._updates: Dict[int, Dict[str, Any]] = {}
        self._inserts: Dict[LinkKey, Dict[str, Any]] = {}

    def update(self, link_id: int, values: Dict[str, Any]) -> None:
        row = {column: values.get(column) for column in self._update_columns}
        row["id"] = link_id
        self._updates[link_id] = row

    def insert(self, key: LinkKey, values: Dict[str, Any]) -> None:
        # A brand new row carries the traffic columns either way: there is no
        # fresher value to protect, and leaving them out of the INSERT would
        # mean one row shape for the first cycle and another afterwards.
        row = {column: values.get(column) for column in _INSERT_COLUMNS}
        row["local_device_id"], row["local_port"], row["remote_hostname"] = key
        self._inserts[key] = row

    @property
    def pending(self) -> Tuple[int, int]:
        return len(self._updates), len(self._inserts)

    async def flush(self, db) -> Tuple[int, int]:
        """Write the buffer. Returns (updated, inserted)."""
        updated, inserted = self.pending
        if self._updates:
            await db.execute(
                update(RawLink),
                sorted(self._updates.values(), key=lambda row: row["id"])
            )
        if self._inserts:
            await db.execute(insert(RawLink), list(self._inserts.values()))
        self._updates.clear()
        self._inserts.clear()
        return updated, inserted


class CabledPort(NamedTuple):
    """
    A port the inventory poll has seen a neighbour on.

    This is the fast loop's work list. ``port_index`` is the ifIndex the
    inventory poll resolved for the cable, which is exactly what makes a fast
    cycle a handful of GETs instead of a walk of the interface tables.
    """
    link_id: int
    local_port: Optional[str]
    port_index: int
    remote_hostname: Optional[str]
    bandwidth_mbps: Optional[int]
    in_octets: Optional[int]
    out_octets: Optional[int]
    sampled_at: Optional[datetime]
    oper_status: Optional[int]


async def load_cabled_ports(db, shard=None) -> Dict[int, List[CabledPort]]:
    """
    Every cable with a resolved ifIndex, grouped by device.

    One statement for the whole fleet, or for this shard's slice of it when
    ``shard`` is given. Cables whose ifIndex could not be resolved are left
    out: there is nothing to GET for them, and the inventory poll keeps trying
    to resolve them every cycle.
    """
    statement = (
        select(
            RawLink.id,
            RawLink.local_device_id,
            RawLink.local_port,
            RawLink.local_port_index,
            RawLink.remote_hostname,
            RawLink.bandwidth_mbps,
            RawLink.in_octets,
            RawLink.out_octets,
            RawLink.sampled_at,
            RawLink.oper_status,
        ).where(RawLink.local_port_index.isnot(None))
    )
    if shard is not None:
        statement = shard.scope(statement, RawLink.local_device_id)
    result = await db.execute(statement)

    by_device: Dict[int, List[CabledPort]] = {}
    for row in result:
        by_device.setdefault(row.local_device_id, []).append(
            CabledPort(
                link_id=row.id,
                local_port=row.local_port,
                port_index=row.local_port_index,
                remote_hostname=row.remote_hostname,
                bandwidth_mbps=row.bandwidth_mbps,
                in_octets=row.in_octets,
                out_octets=row.out_octets,
                sampled_at=row.sampled_at,
                oper_status=row.oper_status,
            )
        )
    return by_device


class LinkSampleBuffer:
    """
    The fast loop's raw_links writes, as at most two executemany UPDATEs for
    the whole cycle.

    One buffer per cycle rather than per device: a fast cycle writes five
    numbers per cable and nothing else, so there is no reason to pay a round
    trip per device for it.

    Rows are split by whether the port's state changed, because every row in
    one executemany has to carry the same columns and oper_status is the one
    column here with an index on it. Writing it on all 10,000 cables every
    cycle would churn that index continuously to store the value it already
    had; a cable whose state did not change gets its traffic columns and
    nothing else. That also makes oper_status_at mean "since when", not "as
    of the last cycle".
    """

    _TRAFFIC_ONLY = _TRAFFIC_COLUMNS
    _WITH_STATE = _TRAFFIC_COLUMNS + _STATE_COLUMNS

    def __init__(self) -> None:
        self._traffic: Dict[int, Dict[str, Any]] = {}
        self._with_state: Dict[int, Dict[str, Any]] = {}

    def set(
        self, link_id: int, values: Dict[str, Any], state_changed: bool = False
    ) -> None:
        if state_changed:
            target, other, columns = self._with_state, self._traffic, self._WITH_STATE
        else:
            target, other, columns = self._traffic, self._with_state, self._TRAFFIC_ONLY
        row = {column: values.get(column) for column in columns}
        row["id"] = link_id
        target[link_id] = row
        other.pop(link_id, None)

    @property
    def pending(self) -> Tuple[int, int]:
        """(rows buffered, of which carry a state change)"""
        return len(self._traffic) + len(self._with_state), len(self._with_state)

    async def flush(self, db) -> Tuple[int, int]:
        """Write the buffer. Returns (rows written, of which state changes)."""
        written, changed = self.pending
        for rows in (self._traffic, self._with_state):
            if not rows:
                continue
            # Ascending primary key order: the inventory poll can be writing
            # its own columns on some of these rows at the same time, and two
            # transactions that take row locks in the same order cannot
            # deadlock against each other.
            await db.execute(
                update(RawLink),
                sorted(rows.values(), key=lambda row: row["id"])
            )
        self._traffic.clear()
        self._with_state.clear()
        return written, changed
