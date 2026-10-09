"""
How N collector processes divide one fleet between them.

A collector cycle is CPU bound on SNMP decoding, not on waiting for switches
(docs/PERFORMANCE.md section 10), so one process on one core has a ceiling:
around 120 seconds of inventory interval at 500 devices, and a 60 second fast
loop at 55% of a core. Python's GIL means threads do not help, but nothing in
a cycle needs to be in the same process as anything else in it - each device
is polled independently and written independently. So the way past the
ceiling is more processes, each taking a slice of the fleet.

The slice is ``devices.id % shards == shard_id``. Three properties matter,
and this is the cheapest expression that has all three:

  * **Stable.** A device's shard depends only on its own id, so adding or
    deleting any other device never moves it. Slicing by position
    ("every Nth row of an ordered list") would reshuffle the whole fleet
    whenever one device is removed, and every reshuffled device would lose
    its offline backoff and its place in the cadence.
  * **Expressible in SQL.** The filter goes in the WHERE clause, so a shard
    reads its own devices and its own cables and nothing else - at 500
    devices and 10,000 cables that is a quarter of the rows transferred and
    held in memory per process at four shards, rather than all of them with
    three quarters thrown away. A Python-side hash of the hostname would be
    better balanced in theory, but it cannot be pushed into the query, and
    autoincrement ids are already evenly spread modulo anything.
  * **Complete and disjoint.** Every id belongs to exactly one shard. That is
    what makes the offline backoff and the per-device cadence correct without
    any coordination between processes: each device has exactly one owner.

What this does *not* shard is the work that is about the fleet rather than
about a device: rebuilding merged_links and running the alert cycle both read
everything and must happen once, not N times. That is what the lease in
app/core/lease.py is for.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ShardConfigError(ValueError):
    """The shard count and id cannot describe a slice of a fleet"""


class Shard:
    """
    This process's slice of the fleet.

    ``Shard(1, 0)`` is the unsharded case and is deliberately not a special
    code path: ``owns()`` is True for everything and ``criterion()`` returns
    None, so a single collector builds exactly the queries it built before
    sharding existed.
    """

    __slots__ = ("count", "index")

    def __init__(self, count: int = 1, index: int = 0):
        count = int(count)
        index = int(index)
        if count < 1:
            raise ShardConfigError(
                f"COLLECTOR_SHARDS={count} is not a number of processes"
            )
        if not 0 <= index < count:
            raise ShardConfigError(
                f"COLLECTOR_SHARD_ID={index} is not a shard of "
                f"COLLECTOR_SHARDS={count}: it has to be 0..{count - 1}"
            )
        self.count = count
        self.index = index

    @classmethod
    def from_settings(cls, settings) -> "Shard":
        """
        Read the shard identity from the environment.

        A misconfiguration here is not something to paper over: with
        ``COLLECTOR_SHARDS=4`` and three processes started, a quarter of the
        fleet is never polled, and the only symptom is that those devices
        gradually alert as offline. So a count or id that cannot be a slice
        raises rather than being clamped into something that looks fine.
        """
        return cls(
            count=getattr(settings, "collector_shards", 1),
            index=getattr(settings, "collector_shard_id", 0)
        )

    # -- membership -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the fleet is actually split across processes"""
        return self.count > 1

    def owns(self, device_id: Optional[int]) -> bool:
        """True when this process is responsible for that device"""
        if not self.enabled:
            return True
        if device_id is None:
            return False
        return device_id % self.count == self.index

    def criterion(self, column):
        """
        The WHERE clause for this shard, or None when there is nothing to filter.

        ``column`` is whichever column holds the *device* id in the table being
        queried - ``Device.id`` for devices, ``RawLink.local_device_id`` for
        cables. Both resolve to the same shard for the same device, which is
        what keeps a shard's cables and its devices together.
        """
        if not self.enabled:
            return None
        return column % self.count == self.index

    def scope(self, statement, column):
        """``statement`` with this shard's criterion applied, if any"""
        criterion = self.criterion(column)
        if criterion is None:
            return statement
        return statement.where(criterion)

    # -- identity ---------------------------------------------------------

    @property
    def name(self) -> str:
        """Short stable identifier, used as the lease owner and in logs"""
        return f"shard-{self.index}-of-{self.count}"

    def label(self) -> str:
        if not self.enabled:
            return "single process"
        return f"shard {self.index} of {self.count}"

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"Shard(count={self.count}, index={self.index})"

    def __eq__(self, other) -> bool:
        return (
            isinstance(other, Shard)
            and other.count == self.count
            and other.index == self.index
        )

    def __hash__(self) -> int:
        return hash((self.count, self.index))
