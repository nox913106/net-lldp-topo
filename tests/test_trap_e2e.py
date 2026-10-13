"""
Real traps: real datagrams, from real source addresses, to a real receiver.

The whole point of matching a device by every address it holds is that the
trap arrives from the interface the packet left by rather than from the
address the device is polled on - so a test that hands a parsed trap to a
function proves nothing about the thing that was broken. These bind a
TrapReceiver to a loopback port and send it notifications from addresses the
simulated switch reports as its own, which is possible because all of
127.0.0.0/8 is local.

What each case guards against is a different kind of wrong:

* a trap from another of the device's interfaces being dropped - the burden
  this replaced, one `snmp-server trap-source` line per device;
* a trap from an address nobody holds being applied to someone;
* a device behind NAT, whose v1 trap carries the address it thinks it has;
* a **forged** claim overriding the source address, which would let anyone
  who can send a datagram raise an alert against any device;
* and the staleness the walk schedule buys its CPU saving with
  (docs/PERFORMANCE.md 38.4).
"""
import asyncio
import socket

import pytest
from pysnmp.hlapi.asyncio import (
    CommunityData, ContextData, NotificationType, ObjectIdentity, SnmpEngine,
    UdpTransportTarget, sendNotification
)
from pysnmp.proto import rfc1902
from sqlalchemy import select

import app.core.trap_receiver as trap_module
from app.core.snmp_oids import (
    IF_INDEX, IF_OPER_STATUS, LINK_DOWN_TRAP, LINK_UP_TRAP, SNMP_TRAP_ADDRESS,
)
from app.core.trap_receiver import TrapReceiver
from app.models.alert import Alert
from app.models.device import Device, DeviceAddress, DeviceStatus
from app.models.link import RawLink
from tests.conftest import (
    TRAP_ADDED_ADDRESS, TRAP_COMMUNITY, TRAP_OWN_ADDRESSES, TRAP_PEER_COMMUNITY,
    TRAP_PEER_IP, TRAP_PEER_SYS_NAME, TRAP_RENUMBERED_COMMUNITY,
    TRAP_STRANGER_ADDRESS, TRAP_SYS_NAME,
)

# Traps are udp/162 in production; nothing in a test may need a privileged
# port, so each one takes a free high port of its own.
PORT = 1          # a cabled port that nothing has reported the state of yet
OTHER_PORT = 2


def run(coro):
    return asyncio.run(coro)


async def _is_one(awaitable, wanted):
    return (await awaitable) == wanted


async def _at_least(receiver, counter, n):
    return getattr(receiver, counter) >= n


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def seed(hostname, ip, community):
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


async def set_community(device_id, community):
    """
    Renumber a device, as far as the collector can tell.

    snmpsim serves a different recording per community, so pointing the
    device at another one is how a test changes what the next address walk
    finds - without restarting the agent mid-test.
    """
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        device = await db.get(Device, device_id)
        device.snmp_community = community
        await db.commit()


async def addresses_of(device_id):
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        return sorted((await db.execute(
            select(DeviceAddress.address)
            .where(DeviceAddress.device_id == device_id)
        )).scalars().all())


async def cable_state(device_id, port_index):
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        return (await db.execute(
            select(RawLink.oper_status)
            .where(RawLink.local_device_id == device_id)
            .where(RawLink.local_port_index == port_index)
        )).scalar_one_or_none()


async def link_alerts():
    from app.db.database import async_session_maker

    async with async_session_maker() as db:
        return [
            (row.device_id, row.alert_type, row.is_active)
            for row in (await db.execute(
                select(Alert).where(Alert.alert_type.in_(
                    ("link_down", "link_up")
                ))
            )).scalars().all()
        ]


async def send(port, source_ip, trap_oid, if_index, oper_status,
               version=1, claims=None):
    """
    One notification, from a chosen source address.

    ``version=1`` is pysnmp's mpModel for v2c and ``0`` is v1; only v1 has an
    agent-addr, which the receiver reads as RFC 3584's snmpTrapAddress.0.
    ``claims`` sets it explicitly, which is how a forged claim is forged.
    """
    target = UdpTransportTarget(("127.0.0.1", port))
    # The source address is the whole point: the receiver identifies the
    # device by the address the datagram came from.
    target.iface = (source_ip, 0)

    varbinds = [
        (f"{IF_INDEX}.{if_index}", rfc1902.Integer(if_index)),
        (f"{IF_OPER_STATUS}.{if_index}", rfc1902.Integer(oper_status)),
    ]
    if claims is not None:
        varbinds.append((SNMP_TRAP_ADDRESS, rfc1902.IpAddress(claims)))

    error, status, index, _ = await sendNotification(
        SnmpEngine(), CommunityData("public", mpModel=version), target,
        ContextData(), "trap",
        NotificationType(ObjectIdentity(trap_oid)).addVarBinds(*varbinds)
    )
    assert error is None, (error, status, index)


async def wait_for(predicate, timeout=6.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    return await predicate()


async def receiver_on(port, damper, requires_poll=False):
    receiver = TrapReceiver(
        bind_host="127.0.0.1", port=port, communities=["public"],
        damper=damper, requires_poll=requires_poll, poll_confirms_state=True
    )
    assert await receiver.start() is True, "the receiver did not bind"
    return receiver


@pytest.fixture
def quick_map(monkeypatch):
    """
    Make the receiver's address map re-read promptly.

    In production it is held for a minute, with a cooldown so that a flood
    from an unknown source cannot make every datagram a database read. Both
    are deliberate, and both would otherwise turn these tests into sleeps -
    so they are shortened here rather than waited out. docs/CONFIGURATION.md
    5.5 is where the real numbers are stated.
    """
    monkeypatch.setattr(trap_module, "DEVICE_MAP_TTL_SECONDS", 0.2)
    monkeypatch.setattr(trap_module, "DEVICE_MAP_MISS_COOLDOWN_SECONDS", 0.05)


# -- identifying the device ------------------------------------------------


def test_a_trap_from_another_of_the_devices_interfaces_is_applied(
    collector_process, quick_map
):
    """
    The case the whole feature exists for, with no trap-source anywhere.

    The device is polled on 127.0.0.1 and its own IP address table also
    reports 127.0.0.2 and 127.0.0.3. A linkDown really arriving from
    127.0.0.3 has to update that cable and alert on it.
    """
    port = free_udp_port()

    async def check():
        device_id = await seed(TRAP_SYS_NAME, "127.0.0.1", TRAP_COMMUNITY)
        await collector_process.poll_all_devices()
        learned = await addresses_of(device_id)

        receiver = await receiver_on(port, collector_process.flap_damper)
        try:
            before = await cable_state(device_id, PORT)

            await send(port, TRAP_OWN_ADDRESSES[1], LINK_DOWN_TRAP, PORT, 2)
            await wait_for(lambda: cable_state(device_id, PORT))
            down = await cable_state(device_id, PORT)
            alerts_down = await wait_for(link_alerts)

            await send(port, TRAP_OWN_ADDRESSES[1], LINK_UP_TRAP, PORT, 1)
            await wait_for(
                lambda: _is_one(cable_state(device_id, PORT), 1)
            )
            return learned, before, down, alerts_down, (
                await cable_state(device_id, PORT), await link_alerts()
            )
        finally:
            await receiver.stop()

    learned, before, down, alerts_down, (up, alerts_up) = run(check())

    assert learned == sorted(TRAP_OWN_ADDRESSES)
    # The inventory loop records the cable but not its state: per-cable
    # up/down belongs to the fast loop and the trap receiver, so "nothing has
    # reported this cable yet" is NULL rather than up
    assert before is None
    assert down == 2
    assert [(kind, active) for _, kind, active in alerts_down] == [
        ("link_down", True)
    ]
    assert up == 1
    assert [(kind, active) for _, kind, active in alerts_up] == [
        ("link_down", False)
    ]


def test_a_trap_from_an_address_no_device_holds_is_ignored(
    collector_process, quick_map
):
    port = free_udp_port()

    async def check():
        device_id = await seed(TRAP_SYS_NAME, "127.0.0.1", TRAP_COMMUNITY)
        await collector_process.poll_all_devices()

        receiver = await receiver_on(port, collector_process.flap_damper)
        try:
            await send(port, TRAP_STRANGER_ADDRESS, LINK_DOWN_TRAP, PORT, 2)
            await wait_for(lambda: _at_least(receiver, "ignored", 1))
            return (receiver.acted_on, receiver.ignored,
                    await cable_state(device_id, PORT), await link_alerts())
        finally:
            await receiver.stop()

    acted_on, ignored, state, alerts = run(check())
    assert (acted_on, ignored) == (0, 1)
    assert state is None            # the cable was not touched
    assert alerts == []


def test_a_v1_trap_is_identified_by_the_address_it_claims(
    collector_process, quick_map
):
    """
    NAT: the source address belongs to nobody, and only the trap knows.

    RFC 3584's snmpTrapAddress.0 is the last resort rather than evidence -
    it is in the packet, so anyone can write it - but a device behind NAT
    has nothing else to offer.
    """
    port = free_udp_port()

    async def check():
        device_id = await seed(TRAP_SYS_NAME, "127.0.0.1", TRAP_COMMUNITY)
        await collector_process.poll_all_devices()

        receiver = await receiver_on(port, collector_process.flap_damper)
        try:
            await send(port, TRAP_STRANGER_ADDRESS, LINK_DOWN_TRAP, PORT, 2,
                       version=0, claims="127.0.0.1")
            await wait_for(lambda: cable_state(device_id, PORT))
            return await cable_state(device_id, PORT), receiver.acted_on
        finally:
            await receiver.stop()

    state, acted_on = run(check())
    assert state == 2
    assert acted_on == 1


def test_a_forged_claim_cannot_override_the_source_address(
    collector_process, quick_map
):
    """
    The security boundary, as a real packet.

    A trap sent from trap-b's address claiming to be trap-a must be counted
    against trap-b. If the claim won, anyone able to send a datagram could
    raise an alert against any device in the fleet - and could do it from
    an address that is itself managed, so nothing would look unusual.
    """
    port = free_udp_port()

    async def check():
        trap_a = await seed(TRAP_SYS_NAME, "127.0.0.1", TRAP_COMMUNITY)
        trap_b = await seed(TRAP_PEER_SYS_NAME, TRAP_PEER_IP,
                            TRAP_PEER_COMMUNITY)
        await collector_process.poll_all_devices()

        receiver = await receiver_on(port, collector_process.flap_damper)
        try:
            await send(port, TRAP_PEER_IP, LINK_DOWN_TRAP, OTHER_PORT, 2,
                       version=0, claims="127.0.0.1")
            await wait_for(lambda: cable_state(trap_b, OTHER_PORT))
            return (trap_a, trap_b,
                    await cable_state(trap_b, OTHER_PORT),
                    await cable_state(trap_a, OTHER_PORT),
                    [device_id for device_id, _, _ in await link_alerts()])
        finally:
            await receiver.stop()

    trap_a, trap_b, peer_state, claimed_state, alerted = run(check())
    assert peer_state == 2          # the device that really sent it
    assert claimed_state is None    # the device it claimed to be
    # And the alert is against the sender, not the device named in the packet
    assert alerted == [trap_b]


# -- what the walk schedule costs ------------------------------------------


def test_an_address_added_after_the_last_walk_waits_for_the_next_one(
    collector_process, quick_map, monkeypatch
):
    """
    The price of walking the address table once an hour instead of every
    cycle, as a real trap: between the renumbering and the next walk, traps
    from the new address are ignored.

    This is the behaviour COLLECTOR_ADDRESS_INTERVAL buys its CPU saving
    with, and the reason the default is an hour rather than a day. Nothing is
    *missed* - the polling loop still finds the cable's state - but the trap
    is not what finds it (docs/CONFIGURATION.md 5.5).
    """
    import app.core.device_addresses as device_addresses

    class FakeClock:
        def __init__(self):
            self.now = 1_000.0

        def monotonic(self):
            return self.now

    clock = FakeClock()
    monkeypatch.setattr(device_addresses, "time", clock)
    port = free_udp_port()

    async def check():
        device_id = await seed(TRAP_SYS_NAME, "127.0.0.1", TRAP_COMMUNITY)
        await collector_process.poll_all_devices()
        first = await addresses_of(device_id)

        # Someone adds an address to an interface
        await set_community(device_id, TRAP_RENUMBERED_COMMUNITY)
        await collector_process.poll_all_devices()
        inside_interval = await addresses_of(device_id)

        receiver = await receiver_on(port, collector_process.flap_damper)
        try:
            await send(port, TRAP_ADDED_ADDRESS, LINK_DOWN_TRAP, PORT, 2)
            await wait_for(lambda: _at_least(receiver, "ignored", 1))
            while_stale = (receiver.ignored, receiver.acted_on,
                           await cable_state(device_id, PORT))

            # An hour later, the walk happens on the next ordinary cycle
            clock.now += 3600
            await collector_process.poll_all_devices()
            after_walk = await addresses_of(device_id)

            await send(port, TRAP_ADDED_ADDRESS, LINK_DOWN_TRAP, PORT, 2)
            await wait_for(lambda: cable_state(device_id, PORT))
            applied = (receiver.acted_on, await cable_state(device_id, PORT))
        finally:
            await receiver.stop()

        return first, inside_interval, while_stale, after_walk, applied

    first, inside, while_stale, after_walk, applied = run(check())

    assert first == sorted(TRAP_OWN_ADDRESSES)
    # The second cycle did not walk, so the new address is not known yet -
    # and, just as important, the addresses already known were not touched
    assert inside == sorted(TRAP_OWN_ADDRESSES)
    assert while_stale == (1, 0, None)
    # The cycle after the interval elapses picks it up with no other change
    assert after_walk == sorted(TRAP_OWN_ADDRESSES + (TRAP_ADDED_ADDRESS,))
    assert applied == (1, 2)
