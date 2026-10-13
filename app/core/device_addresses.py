"""
Which addresses each device answers to, held for the length of a cycle.

The rows exist so a trap can be matched to the device that sent it even when
it left by an interface other than the one the device is polled on - see
app/models/device.py DeviceAddress and docs/CONFIGURATION.md section 5.3.

Keeping them accurate means reconciling each device's rows against what its
last poll saw, and doing that one device at a time costs a SELECT per device
per cycle: measured at 500 devices, 395 ms of a 2.3 s inventory cycle spent
asking 500 times what one statement could have answered. So this follows the
same shape as RawLinkIndex (app/core/link_index.py): read the table once at
the start of the cycle, answer from memory, and write only where something
actually changed.

A fleet whose addressing is not changing therefore writes nothing here at
all.
"""
import logging
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import delete, insert, select

from app.models.device import DeviceAddress

logger = logging.getLogger(__name__)


class DeviceAddressIndex:
    """
    {device id: {addresses}}, read once per cycle.

    Read fresh each cycle rather than kept, for the same reason as the cable
    index: the API can delete a device, and a stale index would skip INSERTs
    for rows that are not there - silently, because that affects no rows and
    raises nothing.

    Addresses rather than row ids, so that the writes below are expressed in
    the same terms as the comparison. There is nothing a row id would buy:
    (device_id, address) is unique, and keeping ids would mean inventing one
    for a row this cycle just inserted.
    """

    __slots__ = ("_by_device",)

    def __init__(self) -> None:
        self._by_device: Dict[int, Set[str]] = {}

    async def load(self, db, shard=None) -> int:
        """Replace the index with the current table. Returns the row count."""
        statement = select(DeviceAddress.device_id, DeviceAddress.address)
        if shard is not None:
            statement = shard.scope(statement, DeviceAddress.device_id)

        by_device: Dict[int, Set[str]] = {}
        count = 0
        for row in await db.execute(statement):
            by_device.setdefault(row.device_id, set()).add(row.address)
            count += 1

        self._by_device = by_device
        return count

    def known(self, device_id: int) -> Set[str]:
        return self._by_device.get(device_id, set())

    def diff(
        self, device_id: int, addresses: Iterable[Optional[str]]
    ) -> Tuple[List[str], List[str]]:
        """
        (addresses to add, addresses to remove) for this device.

        Removals matter as much as additions: an address that moves to
        another switch has to stop matching this one's traps, or the new
        switch's link_down is raised against the old device.
        """
        existing = self.known(device_id)
        wanted = {address for address in addresses if address}
        return sorted(wanted - existing), sorted(existing - wanted)

    async def apply(
        self, db, device_id: int, addresses: Iterable[Optional[str]]
    ) -> Tuple[int, int]:
        """
        Make the table match what this poll saw. Returns (added, removed).

        Nothing is written when nothing changed, which is the normal case -
        so the whole feature costs one SELECT per cycle on a settled fleet.
        """
        added, stale = self.diff(device_id, addresses)
        if not added and not stale:
            return 0, 0

        if stale:
            await db.execute(
                delete(DeviceAddress)
                .where(DeviceAddress.device_id == device_id)
                .where(DeviceAddress.address.in_(stale))
            )
        if added:
            await db.execute(insert(DeviceAddress), [
                {"device_id": device_id, "address": address, "source": "interface"}
                for address in added
            ])

        # Keep the index matching what was just written, so a device polled
        # twice in one cycle is a no-op the second time rather than an insert
        # the unique constraint rejects.
        current = self._by_device.setdefault(device_id, set())
        current.difference_update(stale)
        current.update(added)

        return len(added), len(stale)


class AddressWalkSchedule:
    """
    When each device is next due for an address walk.

    The walk is the expensive half of this feature: the rows cost one SELECT
    per cycle (above), but asking the device costs 5.01 ms of CPU against a
    95.6 ms poll - 5.2%, or 2.5 s added to a 500-device inventory cycle
    (docs/PERFORMANCE.md section 38.2, which also has how the first attempt
    at this measurement reported 10% and why that was noise).

    Addressing, though, is a slow variable. An interface's IP changes when
    someone changes it; polling it every five minutes buys nothing over
    polling it every hour. So a device is asked at most once per interval and
    the other cycles skip the walk entirely, which is where the cost goes.

    Process memory, deliberately: it decides only how often to *ask*, so the
    worst a restart can do is ask everything once, and nothing that matters
    is lost. Also why the clock is monotonic - a device must not go an extra
    hour unwalked because the host's clock jumped forward.
    """

    __slots__ = ("interval", "_last")

    def __init__(self, interval_seconds: float) -> None:
        # Below zero is the same as zero: walk every cycle.
        self.interval = max(0.0, float(interval_seconds))
        self._last: Dict[int, float] = {}

    def due(self, device_id: int) -> bool:
        """
        Whether this device's addresses should be asked for on this poll.

        A device this process has not walked yet is always due, so the
        interval delays picking up a *change* and never the first answer -
        a device added to the fleet is matched against its own addresses on
        its first successful poll, not an hour later.
        """
        if self.interval <= 0:
            return True
        last = self._last.get(device_id)
        return last is None or (time.monotonic() - last) >= self.interval

    def record(self, device_id: int) -> None:
        """
        Note that this device's addresses were just read.

        Called only when the walk actually produced an answer. Marking a
        device that did not answer would leave it unwalked for an interval
        because it was unreachable, which is the opposite of what should
        happen: a device that just came back is exactly one whose addressing
        may have changed.
        """
        self._last[device_id] = time.monotonic()

    def prune(self, device_ids: Iterable[int]) -> None:
        """Forget devices that are no longer in the fleet."""
        keep = set(device_ids)
        for device_id in [d for d in self._last if d not in keep]:
            del self._last[device_id]

    def walked(self) -> int:
        """How many devices this process has read addresses from."""
        return len(self._last)
