"""
device_addresses is reconciled against the poll, not appended to.

The reason is a correctness one rather than tidiness: if 10.1.1.1 is moved
from one switch to another and the old row survives, every trap the new
switch sends from that address is attributed to the old one - a link_down
raised against the wrong device, which is worse than no trap at all.
"""
import asyncio

from sqlalchemy import delete, select

from app.core.device_addresses import DeviceAddressIndex
from app.db.database import async_session_maker, init_db
from app.models.device import Device, DeviceAddress


def run(coro):
    return asyncio.run(coro)


async def _two_devices():
    await init_db()
    async with async_session_maker() as db:
        await db.execute(delete(DeviceAddress))
        await db.execute(delete(Device))
        one = Device(hostname="sw-one", ip_address="10.0.0.1", snmp_community="public")
        two = Device(hostname="sw-two", ip_address="10.0.0.2", snmp_community="public")
        db.add_all([one, two])
        await db.commit()
        await db.refresh(one)
        await db.refresh(two)
        return one.id, two.id


async def _addresses(device_id):
    async with async_session_maker() as db:
        return sorted(row.address for row in (await db.execute(
            select(DeviceAddress.address)
            .where(DeviceAddress.device_id == device_id)
        )).all())


async def _store(device_id, addresses):
    """
    One device's reconcile, through a freshly loaded index - which is what a
    cycle does: read the table once, then decide from memory.
    """
    async with async_session_maker() as db:
        index = DeviceAddressIndex()
        await index.load(db)
        result = await index.apply(db, device_id, addresses)
        await db.commit()
        return result


def test_the_first_poll_records_what_the_device_holds():
    async def check():
        one, _ = await _two_devices()
        added, removed = await _store(one, ["10.0.0.1", "192.168.99.1"])
        return added, removed, await _addresses(one)

    added, removed, addresses = run(check())
    assert (added, removed) == (2, 0)
    assert addresses == ["10.0.0.1", "192.168.99.1"]


def test_an_unchanged_fleet_writes_nothing():
    """Steady state is one SELECT and no writes at all."""
    async def check():
        one, _ = await _two_devices()
        await _store(one, ["10.0.0.1", "192.168.99.1"])
        return await _store(one, ["192.168.99.1", "10.0.0.1"])

    assert run(check()) == (0, 0)


def test_an_address_the_device_no_longer_holds_is_dropped():
    async def check():
        one, _ = await _two_devices()
        await _store(one, ["10.0.0.1", "192.168.99.1"])
        added, removed = await _store(one, ["10.0.0.1"])
        return added, removed, await _addresses(one)

    added, removed, addresses = run(check())
    assert (added, removed) == (0, 1)
    assert addresses == ["10.0.0.1"]


def test_an_address_that_moves_follows_the_device_that_has_it():
    """The whole reason this is a reconcile."""
    async def check():
        one, two = await _two_devices()
        await _store(one, ["10.0.0.1", "172.16.0.9"])
        await _store(one, ["10.0.0.1"])          # re-cabled, address gone
        await _store(two, ["10.0.0.2", "172.16.0.9"])
        return await _addresses(one), await _addresses(two)

    first, second = run(check())
    assert first == ["10.0.0.1"]
    assert second == ["10.0.0.2", "172.16.0.9"]


def test_a_device_that_reports_nothing_keeps_nothing():
    """
    An agent with neither IP address table answers with an empty list, and an
    empty list is evidence: there is nothing to match traps against. The
    caller is what decides whether the walk happened at all.
    """
    async def check():
        one, _ = await _two_devices()
        await _store(one, ["10.0.0.1"])
        added, removed = await _store(one, [])
        return added, removed, await _addresses(one)

    added, removed, addresses = run(check())
    assert (added, removed) == (0, 1)
    assert addresses == []


def test_one_device_is_reconciled_without_touching_another():
    async def check():
        one, two = await _two_devices()
        await _store(one, ["10.0.0.1", "192.168.99.1"])
        await _store(two, ["10.0.0.2"])
        await _store(one, [])
        return await _addresses(one), await _addresses(two)

    first, second = run(check())
    assert first == []
    assert second == ["10.0.0.2"]


def test_deleting_a_device_takes_its_addresses_with_it():
    async def check():
        one, _ = await _two_devices()
        await _store(one, ["10.0.0.1", "192.168.99.1"])
        async with async_session_maker() as db:
            device = await db.get(Device, one)
            await db.delete(device)
            await db.commit()
        return await _addresses(one)

    assert run(check()) == []


def test_an_empty_address_is_not_stored():
    """A row with no address would match nothing and read as a bug."""
    async def check():
        one, _ = await _two_devices()
        added, _ = await _store(one, ["10.0.0.1", "", None])
        return added, await _addresses(one)

    added, addresses = run(check())
    assert added == 1
    assert addresses == ["10.0.0.1"]
