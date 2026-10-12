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
