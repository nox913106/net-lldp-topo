"""
Both loops, end to end: a real poll, a real database, real rows.

Everything else in this suite tests a piece - a walk, a damper, a schedule.
These run app.collector's own cycle functions, which is the only level at
which a whole class of failure is visible: the SNMP layer can be perfect and
the cycle still store nothing, because of a key read off the collector object
instead of off the result, an index that was never loaded, or a device that
looked offline for a reason that had nothing to do with the device. Each of
those has happened in this project, and none of them would have failed a unit
test.

These were scratchpad scripts until now, which meant the strongest evidence
in the project lived in a container that gets reclaimed - CI tested rather
less than I had. docs/PERFORMANCE.md section 39.
"""
import asyncio

from sqlalchemy import select

from app.models.alert import Alert
from app.models.device import Device, DeviceAddress, DeviceStatus
from app.models.link import MergedLink, RawLink
from tests.conftest import (
    CABLED, DOWN_PORT, OWN_ADDRESSES, PEER, PEER_COMMUNITY, TRAP_PEER_IP,
)


def run(coro):
    return asyncio.run(coro)


async def seed(hostname="sim-local", ip="127.0.0.1", community="public"):
    """
    One managed device, the way the API would add it.

    ``auto_discover=False`` deliberately: discovery would SNMP-probe the
    neighbour this agent advertises, which on the loopback is the agent
    itself. These tests are about the loops, not about discovery.
    """
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        device = Device(
            hostname=hostname, ip_address=ip, snmp_community=community,
            status=DeviceStatus.MANAGED, auto_discover=False
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)
        return device.id


async def all_of(model):
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        return list((await db.execute(select(model))).scalars().all())


async def cables(device_id):
    """{local port index: (remote hostname, oper_status, bandwidth)}"""
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        found = (await db.execute(
            select(RawLink).where(RawLink.local_device_id == device_id)
        )).scalars().all()
    return {
        row.local_port_index:
            (row.remote_hostname, row.oper_status, row.bandwidth_mbps)
        for row in found
    }


async def addresses_of(device_id):
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        return sorted((await db.execute(
            select(DeviceAddress.address)
            .where(DeviceAddress.device_id == device_id)
        )).scalars().all())


# -- the inventory loop ----------------------------------------------------


def test_an_inventory_cycle_stores_the_device_its_cables_and_its_addresses(
    collector_process
):
    async def check():
        from app.db.database import async_session_maker

        device_id = await seed()
        await collector_process.poll_all_devices()

        async with async_session_maker() as db:
            device = await db.get(Device, device_id)
            stored = (device.status, device.vendor, device.last_seen is not None)

        return stored, await cables(device_id), await addresses_of(device_id)

    (status, vendor, seen), cabling, addresses = run(check())

    assert status == DeviceStatus.MANAGED
    assert vendor == "cisco_ios"
    assert seen is True

    # One cable per LLDP neighbour, keyed by the port index the device gave -
    # which is what the fast loop samples and what a trap is matched against
    assert sorted(cabling) == sorted(CABLED)
    assert {entry[0] for entry in cabling.values()} == {PEER}
    assert {entry[2] for entry in cabling.values()} == {1000}

    # And the addresses it holds, which is what makes its traps recognisable
    assert addresses == sorted(OWN_ADDRESSES)


def test_a_second_cycle_changes_nothing_it_does_not_have_to(collector_process):
    """
    A settled fleet writes no new rows.

    This is the shape the cost measurements assume (docs/PERFORMANCE.md 37.2,
    38.3): the cable index and the address index exist so an unchanged device
    costs reads and no writes. A second cycle that duplicated cables or
    re-inserted addresses would make both the measurements and the tables
    wrong.
    """
    async def check():
        device_id = await seed()
        await collector_process.poll_all_devices()
        first = await cables(device_id), await all_of(DeviceAddress)
        await collector_process.poll_all_devices()
        second = await cables(device_id), await all_of(DeviceAddress)
        return first, second

    (cables_1, addresses_1), (cables_2, addresses_2) = run(check())
    assert cables_1 == cables_2
    assert len(addresses_1) == len(addresses_2) == len(OWN_ADDRESSES)
    # Same rows, not replacements: a reconcile that deleted and re-inserted
    # would keep the count and lose first_seen
    assert ({row.id for row in addresses_1}
            == {row.id for row in addresses_2})


def test_a_device_that_does_not_answer_is_called_offline(
    collector_process, monkeypatch
):
    """
    The other half of a poll: nothing on the far end.

    An address with nothing listening answers nothing, and with
    ALERT_OFFLINE_CONFIRM_CYCLES at its default of 1 one silent cycle is
    enough to say so.

    The SNMP timeout is shortened for this one test. At the real default
    (5 seconds, one retry, several GETs before the poll gives up) this takes
    45 seconds, which is a true statement about what an unreachable device
    costs a cycle - docs/PERFORMANCE.md says so and the backoff exists
    because of it - but it is not what is being tested here.
    """
    monkeypatch.setattr(collector_process.inventory_pool, "timeout", 1)
    monkeypatch.setattr(collector_process.inventory_pool, "retries", 0)

    async def check():
        from app.db.database import async_session_maker

        device_id = await seed(hostname="gone", ip="127.0.0.253")
        await collector_process.poll_all_devices()

        async with async_session_maker() as db:
            device = await db.get(Device, device_id)
            return device.status, len(await cables(device_id))

    status, cable_count = run(check())
    assert status == DeviceStatus.OFFLINE
    assert cable_count == 0


# -- the fast loop ---------------------------------------------------------


def test_a_fast_cycle_writes_per_cable_state_and_alerts_on_the_down_one(
    collector_process
):
    """
    The fast loop's whole job, in one cycle.

    The inventory cycle records which ports are cabled; the fast loop samples
    only those, writes each cable's up/down, refreshes the merged links and
    runs the alert checks. The simulated chassis has one cabled port down, so
    exactly one link_down alert must exist afterwards - and the cables that
    are up must not produce one.
    """
    from app.core.fast_poll import FastPoller

    async def check():
        device_id = await seed()
        await collector_process.poll_all_devices()

        poller = FastPoller(
            pool=collector_process.fast_pool, concurrent=4,
            backoff=collector_process.fast_backoff,
            damper=collector_process.flap_damper,
        )
        await poller.run_cycle()

        alerts = [
            (row.alert_type, row.is_active, row.device_id)
            for row in await all_of(Alert)
        ]
        return device_id, await cables(device_id), alerts

    device_id, cabling, alerts = run(check())

    assert cabling[DOWN_PORT][1] == 2            # the down cable
    assert all(cabling[port][1] == 1 for port in CABLED if port != DOWN_PORT)
    assert [a for a in alerts if a[0] == "link_down"] == [
        ("link_down", True, device_id)
    ]


def test_two_devices_that_name_each_other_merge_into_one_link_each(
    collector_process
):
    """
    The merge, which one simulated switch cannot exercise.

    A cable is reported twice - once by each end - and the topology engine
    pairs the two halves by sysName. With only one managed device there is
    nothing to pair with, and merged_links stays empty, which is correct and
    also means the merge is never exercised. So this seeds both ends: the
    default recording names sim-peer as its neighbour, and the peer recording
    names sim-local as its, on a second address of the same agent.
    """
    from app.core.fast_poll import FastPoller

    async def check():
        local = await seed()
        peer = await seed(hostname=PEER, ip=TRAP_PEER_IP,
                          community=PEER_COMMUNITY)
        await collector_process.poll_all_devices()

        poller = FastPoller(
            pool=collector_process.fast_pool, concurrent=4,
            backoff=collector_process.fast_backoff,
            damper=collector_process.flap_damper,
        )
        await poller.run_cycle()

        merged = [
            (row.device_a_id, row.device_b_id, row.port_pairs,
             row.total_bandwidth_mbps)
            for row in await all_of(MergedLink)
        ]
        return local, peer, len(await cables(local)), len(await cables(peer)), merged

    local, peer, local_cables, peer_cables, merged = run(check())

    # Each end reported all three cables
    assert local_cables == peer_cables == len(CABLED)

    # And they came out as *one* link between the two devices carrying three
    # port pairs - not two halves, and not three separate links
    assert len(merged) == 1
    device_a, device_b, pairs, bandwidth = merged[0]
    assert sorted((device_a, device_b)) == sorted((local, peer))
    assert len(pairs) == len(CABLED)
    assert bandwidth == 1000 * len(CABLED)


def test_traffic_is_a_rate_not_a_counter(collector_process):
    """
    Two samples far enough apart, through the loops rather than by hand.

    The counters the agent serves increase on their own, so a second fast
    cycle has something to subtract. A rate that comes back as the counter
    itself - the obvious failure - would be off by nine orders of magnitude,
    so this asserts a plausible band rather than a number.
    """
    from app.core.fast_poll import FastPoller

    async def check():
        device_id = await seed()
        await collector_process.poll_all_devices()
        poller = FastPoller(
            pool=collector_process.fast_pool, concurrent=4,
            backoff=collector_process.fast_backoff,
            damper=collector_process.flap_damper,
        )
        await poller.run_cycle()
        await asyncio.sleep(1.2)
        await poller.run_cycle()
        return await cables(device_id), [
            (row.local_port_index, row.in_bps, row.out_bps)
            for row in (await all_of(RawLink))
        ]

    _, rates = run(check())
    rates = {index: (in_bps, out_bps) for index, in_bps, out_bps in rates}
    for port in CABLED:
        in_bps, out_bps = rates[port]
        # 700 Mbps in, 10 Mbps out, from the rates the recording declares
        assert 500_000_000 < in_bps < 900_000_000, (port, in_bps)
        assert 1_000_000 < out_bps < 20_000_000, (port, out_bps)
