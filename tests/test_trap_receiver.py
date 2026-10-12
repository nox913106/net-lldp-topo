"""
Which device sent this trap?

The answer used to be "whichever one is polled on that exact address", and
that single line of code is what forced an `snmp-server trap-source`
statement onto every switch in an existing network: most platforms send a
trap from whichever interface the packet leaves by, not from the address you
poll them on (docs/CONFIGURATION.md section 5.3).
"""
import asyncio

import pytest
from sqlalchemy import delete

from app.core.trap_receiver import (
    ManagedDevice, TrapEvent, TrapReceiver, _as_address, parse_varbinds,
)
from app.core.snmp_oids import (
    IF_INDEX, IF_OPER_STATUS, LINK_DOWN_TRAP, SNMP_TRAP_ADDRESS, SNMP_TRAP_OID,
)
from app.db.database import async_session_maker, init_db
from app.models.device import Device, DeviceAddress


def run(coro):
    return asyncio.run(coro)


def receiver(**kwargs):
    return TrapReceiver(bind_host="127.0.0.1", port=0,
                        communities=["public"], **kwargs)


def device(number=1):
    return ManagedDevice(id=number, hostname=f"sw-{number}", alert_profile_id=None)


def event(source_ip, agent_ip=None, if_index=3):
    return TrapEvent(
        source_ip=source_ip, trap_oid=LINK_DOWN_TRAP, if_index=if_index,
        oper_status=2, received_at=0.0, agent_ip=agent_ip,
    )


# -- parsing ---------------------------------------------------------------

class _Value:
    """Something with prettyPrint(), like every pysnmp value object."""

    def __init__(self, text):
        self._text = text

    def prettyPrint(self):
        return self._text

    def __str__(self):
        return self._text


def test_the_agent_address_is_read_from_a_v1_trap():
    """RFC 3584 appends the v1 agent-addr field as snmpTrapAddress.0."""
    trap_oid, if_index, oper_status, agent_ip = parse_varbinds([
        (SNMP_TRAP_OID, _Value(LINK_DOWN_TRAP)),
        (f"{IF_INDEX}.3", 3),
        (f"{IF_OPER_STATUS}.3", 2),
        (SNMP_TRAP_ADDRESS, _Value("10.0.0.1")),
    ])
    assert trap_oid == LINK_DOWN_TRAP
    assert (if_index, oper_status) == (3, 2)
    assert agent_ip == "10.0.0.1"


def test_a_v2c_trap_simply_has_no_agent_address():
    _, _, _, agent_ip = parse_varbinds([
        (SNMP_TRAP_OID, _Value(LINK_DOWN_TRAP)),
        (f"{IF_INDEX}.3", 3),
    ])
    assert agent_ip is None


@pytest.mark.parametrize("text", [
    "", "not an address", "10.0.0", "10.0.0.1.2", "10.0.0.256", "10.0.0.-1",
    "::1", "10.0.0.x",
])
def test_a_junk_agent_address_is_dropped(text):
    """Anyone can put anything in that field; it is checked, not trusted."""
    assert _as_address(_Value(text)) is None


def test_a_usable_agent_address_is_kept():
    assert _as_address(_Value("192.168.1.254")) == "192.168.1.254"
    assert _as_address(_Value(" 10.0.0.1 ")) == "10.0.0.1"


# -- identification --------------------------------------------------------

def test_the_source_address_identifies_the_device():
    known = {"10.0.0.1": device(1)}
    assert receiver()._identify(known, event("10.0.0.1")).id == 1


def test_an_address_the_device_also_holds_identifies_it():
    """
    The point of the whole change: the device sent from its egress interface,
    which is not the address it is polled on, and it is still recognised
    because its own IP address table said it holds that one too.
    """
    known = {"10.0.0.1": device(1), "192.168.99.1": device(1)}
    assert receiver()._identify(known, event("192.168.99.1")).id == 1


def test_an_unknown_source_identifies_nothing():
    assert receiver()._identify({"10.0.0.1": device(1)}, event("10.9.9.9")) is None


def test_the_v1_agent_address_is_the_last_resort():
    """A device behind NAT: the datagram's source is the NAT, and the only
    thing that names the device is what the trap says about itself."""
    known = {"10.0.0.1": device(1)}
    found = receiver()._identify(known, event("203.0.113.7", agent_ip="10.0.0.1"))
    assert found.id == 1


def test_the_source_address_wins_over_the_claim():
    """A forged agent-addr must not be able to move a trap to another device
    when the source address already names one."""
    known = {"10.0.0.1": device(1), "10.0.0.2": device(2)}
    found = receiver()._identify(known, event("10.0.0.2", agent_ip="10.0.0.1"))
    assert found.id == 2


def test_a_claim_nobody_holds_identifies_nothing():
    known = {"10.0.0.1": device(1)}
    assert receiver()._identify(known, event("10.9.9.9", agent_ip="10.9.9.8")) is None


def test_each_unknown_source_is_reported_once(caplog):
    trap_receiver = receiver()
    with caplog.at_level("WARNING"):
        for _ in range(5):
            trap_receiver._warn_unknown(event("10.9.9.9"))
        trap_receiver._warn_unknown(event("10.9.9.8"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "10.9.9.9" in warnings[0].message


def test_the_warning_says_what_the_trap_claimed():
    trap_receiver = receiver()
    import logging
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger("app.core.trap_receiver").addHandler(handler)
    try:
        trap_receiver._warn_unknown(event("203.0.113.7", agent_ip="10.0.0.1"))
    finally:
        logging.getLogger("app.core.trap_receiver").removeHandler(handler)
    assert "claims to be 10.0.0.1" in records[0].getMessage()


def test_the_set_of_remembered_sources_is_bounded():
    """
    A spoofing flood must not be able to grow the receiver's memory. The
    warnings are already one per address; past the cap it simply stops
    remembering which ones it has seen.
    """
    from app.core.trap_receiver import MAX_REMEMBERED_SOURCES

    trap_receiver = receiver()
    for number in range(MAX_REMEMBERED_SOURCES + 500):
        trap_receiver._warn_unknown(
            event(f"10.{number // 256}.{number % 256}.1")
        )
    assert len(trap_receiver._unknown_sources) == MAX_REMEMBERED_SOURCES


def test_agent_matches_are_remembered_within_the_same_bound():
    from app.core.trap_receiver import MAX_REMEMBERED_SOURCES

    trap_receiver = receiver()
    known = {"10.0.0.1": device(1)}
    for number in range(MAX_REMEMBERED_SOURCES + 100):
        found = trap_receiver._identify(
            known,
            event(f"203.0.{number // 256}.{number % 256}", agent_ip="10.0.0.1"),
        )
        # Still identified; only the "said this already" memory is capped
        assert found.id == 1
    assert len(trap_receiver._agent_matched) == MAX_REMEMBERED_SOURCES


# -- the map the identification runs against -------------------------------

async def _seed(addresses_for_two=()):
    await init_db()
    async with async_session_maker() as db:
        await db.execute(delete(DeviceAddress))
        await db.execute(delete(Device))
        first = Device(hostname="sw-one", ip_address="10.0.0.1",
                       snmp_community="public")
        second = Device(hostname="sw-two", ip_address="10.0.0.2",
                        snmp_community="public")
        db.add_all([first, second])
        await db.commit()
        await db.refresh(first)
        await db.refresh(second)
        for address in addresses_for_two:
            db.add(DeviceAddress(device_id=second.id, address=address))
        await db.commit()
        return first.id, second.id


def test_the_map_holds_management_and_extra_addresses():
    async def check():
        one, two = await _seed(["192.168.99.2", "172.16.0.2"])
        async with async_session_maker() as db:
            return await receiver()._device_map(db, set()), one, two

    found, one, two = run(check())
    assert found["10.0.0.1"].id == one
    assert found["10.0.0.2"].id == two
    assert found["192.168.99.2"].id == two
    assert found["172.16.0.2"].id == two
    assert found["192.168.99.2"].hostname == "sw-two"


def test_a_management_address_wins_a_collision():
    """
    Two devices claiming one address is a misconfiguration somewhere. The
    device actually polled on it is the less surprising answer.
    """
    async def check():
        one, two = await _seed(["10.0.0.1"])      # sw-two also claims sw-one's
        async with async_session_maker() as db:
            return await receiver()._device_map(db, set()), one

    found, one = run(check())
    assert found["10.0.0.1"].id == one


def test_a_device_with_no_extra_addresses_still_works():
    async def check():
        one, _ = await _seed()
        async with async_session_maker() as db:
            return await receiver()._device_map(db, set()), one

    found, one = run(check())
    assert found["10.0.0.1"].id == one
    assert len(found) == 2
