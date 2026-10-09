"""
Per-cycle raw_links index and batched writer.

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

See docs/PERFORMANCE.md section 17.
"""
import logging
from datetime import datetime
from typing import Any, Dict, NamedTuple, Optional, Tuple

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

    async def load(self, db) -> int:
        """Replace the index with the current contents of raw_links"""
        result = await db.execute(
            select(
                RawLink.id,
                RawLink.local_device_id,
                RawLink.local_port,
                RawLink.remote_hostname,
                RawLink.bandwidth_mbps,
                RawLink.in_octets,
                RawLink.out_octets,
                RawLink.sampled_at,
            )
        )
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
_UPDATE_COLUMNS = (
    "local_port_index",
    "remote_port",
    "remote_chassis_id",
    "protocol",
    "last_seen",
    "bandwidth_mbps",
    "in_octets",
    "out_octets",
    "in_bps",
    "out_bps",
    "sampled_at",
)

_INSERT_COLUMNS = (
    "local_device_id",
    "local_port",
    "remote_hostname",
) + _UPDATE_COLUMNS


class LinkWriteBuffer:
    """
    One device's raw_links writes, sent as two statements.

    Keyed rather than appended: a device that advertises the same neighbour
    twice on the same port used to end up with one row (the second pass found
    the first write and overwrote it), and that stays true here.
    """

    def __init__(self) -> None:
        self._updates: Dict[int, Dict[str, Any]] = {}
        self._inserts: Dict[LinkKey, Dict[str, Any]] = {}

    def update(self, link_id: int, values: Dict[str, Any]) -> None:
        row = {column: values.get(column) for column in _UPDATE_COLUMNS}
        row["id"] = link_id
        self._updates[link_id] = row

    def insert(self, key: LinkKey, values: Dict[str, Any]) -> None:
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
            await db.execute(update(RawLink), list(self._updates.values()))
        if self._inserts:
            await db.execute(insert(RawLink), list(self._inserts.values()))
        self._updates.clear()
        self._inserts.clear()
        return updated, inserted
