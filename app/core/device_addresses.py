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
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy import delete, insert, select

from app.models.device import DeviceAddress

logger = logging.getLogger(__name__)


class DeviceAddressIndex:
    """
    {device id: {address: row id}}, read once per cycle.

    Read fresh each cycle rather than kept, for the same reason as the cable
    index: the API can delete a device, and a stale index would aim DELETEs
    at rows that are already gone and skip INSERTs for rows that are not
    there - silently, because neither affects any rows.
    """

    __slots__ = ("_by_device",)

    def __init__(self) -> None:
        self._by_device: Dict[int, Dict[str, int]] = {}

    async def load(self, db, shard=None) -> int:
        """Replace the index with the current table. Returns the row count."""
        statement = select(
            DeviceAddress.id, DeviceAddress.device_id, DeviceAddress.address
        )
        if shard is not None:
            statement = shard.scope(statement, DeviceAddress.device_id)

        by_device: Dict[int, Dict[str, int]] = {}
        count = 0
        for row in await db.execute(statement):
            by_device.setdefault(row.device_id, {})[row.address] = row.id
            count += 1

        self._by_device = by_device
        return count

    def known(self, device_id: int) -> Dict[str, int]:
        return self._by_device.get(device_id, {})

    def diff(
        self, device_id: int, addresses: Iterable[Optional[str]]
    ) -> Tuple[List[str], List[int]]:
        """
        (addresses to add, row ids to delete) for this device.

        Deletions matter as much as insertions: an address that moves to
        another switch has to stop matching this one's traps, or the new
        switch's link_down is raised against the old device.
        """
        existing = self.known(device_id)
        wanted = {address for address in addresses if address}
        return (
            sorted(wanted - set(existing)),
            [row_id for address, row_id in existing.items() if address not in wanted],
        )

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
            await db.execute(delete(DeviceAddress).where(DeviceAddress.id.in_(stale)))
        if added:
            await db.execute(insert(DeviceAddress), [
                {"device_id": device_id, "address": address, "source": "interface"}
                for address in added
            ])

        # Keep the index consistent with what was just written, so a second
        # poll of the same device in the same cycle (a retry) is a no-op
        # rather than a duplicate insert the unique constraint would reject.
        current = self._by_device.setdefault(device_id, {})
        for address in added:
            current[address] = -1
        for address in [a for a, row_id in current.items() if row_id in stale]:
            current.pop(address, None)

        return len(added), len(stale)
