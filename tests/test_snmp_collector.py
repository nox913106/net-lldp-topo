"""
The walk is alive: every assertion here is about rows coming back.

The outage this guards against produced no exception, no log line and no
alert - `lldpRemTable` simply answered with nothing, so every device looked
like it had no neighbours and the topology was empty. A test that asserts on
counts is the only kind that fails when that happens again.
"""
import asyncio

import pytest

from app.core.snmp_collector import SNMPCollector
from app.core.snmp_oids import (
    IF_DESCR, IF_HC_IN_OCTETS, IF_HIGH_SPEED, IF_OPER_STATUS,
    LLDP_REM_SYS_NAME,
)
from app.core.traffic import counter_rate
from tests.conftest import CABLED, DOWN_PORT, NIF, OWN_ADDRESSES, PEER


def run(coro):
    return asyncio.run(coro)


async def _with_collector(agent, fn, **kwargs):
    host, port, community = agent
    collector = SNMPCollector(community, timeout=2, retries=1, port=port, **kwargs)
    try:
        return await fn(collector, host)
    finally:
        await collector.aclose()


def test_walk_returns_rows(snmp_agent):
    """The 0-rows regression, straight up: three tables must not be empty."""
    async def check(collector, host):
        return {
            "ifDescr": await collector._snmp_walk(host, IF_DESCR),
            "ifOperStatus": await collector._snmp_walk(host, IF_OPER_STATUS),
            "lldpRemSysName": await collector._snmp_walk(host, LLDP_REM_SYS_NAME),
        }

    tables = run(_with_collector(snmp_agent, check))
    assert len(tables["ifDescr"]) == NIF
    assert len(tables["ifOperStatus"]) == NIF
    assert len(tables["lldpRemSysName"]) == len(CABLED)


def test_lldp_neighbours_are_parsed(snmp_agent):
    """Rows are not enough - they have to come back out as neighbours."""
    async def check(collector, host):
        return await collector.get_lldp_neighbors(host)

    neighbours = run(_with_collector(snmp_agent, check))
    assert len(neighbours) == len(CABLED)
    assert {n.remote_hostname for n in neighbours} == {PEER}
    assert {n.local_port for n in neighbours} == {
        f"GigabitEthernet1/0/{i}" for i in CABLED
    }
    # The management address TLV is what auto-discovery follows
    assert {n.remote_mgmt_ip for n in neighbours} == {"127.0.0.1"}


def test_device_info_and_vendor(snmp_agent):
    async def check(collector, host):
        return await collector.get_device_info(host)

    info = run(_with_collector(snmp_agent, check))
    assert info is not None
    assert info.hostname == "sim-local"
    assert info.vendor == "cisco_ios"


def test_port_samples_read_state_and_counters(snmp_agent):
    """
    The fast loop's whole SNMP footprint: ~3 varbinds per cabled port.

    A port that is down must read 2 and not None, because None means "the
    agent did not answer" and must never look like a link that is down.
    """
    async def check(collector, host):
        return await collector.get_port_samples(host, CABLED)

    samples = run(_with_collector(snmp_agent, check))
    assert samples is not None
    assert set(samples) == set(CABLED)
    assert samples[DOWN_PORT].oper_status == 2
    assert all(samples[i].oper_status == 1 for i in CABLED if i != DOWN_PORT)
    assert all(samples[i].in_octets is not None for i in CABLED)


def test_unreachable_device_is_none_not_empty(snmp_agent):
    """
    "Did not answer" and "answered with nothing" must stay distinguishable:
    the first is what offline confirmation counts, the second is a device
    whose cables were removed.
    """
    _, port, community = snmp_agent

    async def check():
        collector = SNMPCollector(community, timeout=1, retries=0, port=port)
        try:
            # 127.0.0.9 has no agent on it
            return (
                await collector.get_device_info("127.0.0.9"),
                await collector.get_port_samples("127.0.0.9", CABLED),
            )
        finally:
            await collector.aclose()

    info, samples = run(check())
    assert info is None
    assert samples is None


def test_interface_speed_is_read_not_guessed(snmp_agent):
    """ifHighSpeed, so bandwidth does not come from parsing a port name."""
    async def check(collector, host):
        return await collector._snmp_walk(host, IF_HIGH_SPEED)

    speeds = run(_with_collector(snmp_agent, check))
    assert len(speeds) == NIF
    assert {int(v) for v in speeds.values()} == {1000}


def test_own_addresses_from_the_deprecated_table(snmp_agent):
    """
    Every address the device holds, so a trap that left by another interface
    is still recognisable as this device's - the alternative being an
    `snmp-server trap-source` line on every switch in the fleet.
    """
    async def check(collector, host):
        return await collector.get_own_addresses(host)

    addresses = run(_with_collector(snmp_agent, check))
    assert sorted(addresses) == sorted(OWN_ADDRESSES)


def test_own_addresses_from_the_modern_table(snmp_agent):
    """
    Same device, firmware that only has ipAddressTable. There the address is
    in the row's index rather than its value, so it is a different code path
    and some current firmware has nothing else.
    """
    host, port, _ = snmp_agent

    async def check():
        collector = SNMPCollector("modern", timeout=2, retries=1, port=port)
        try:
            return await collector.get_own_addresses(host)
        finally:
            await collector.aclose()

    assert sorted(run(check())) == sorted(OWN_ADDRESSES)


def test_a_device_with_no_address_table_reports_nothing(snmp_agent):
    """Not an error, and not a guess: an empty list."""
    _, port, community = snmp_agent

    async def check():
        collector = SNMPCollector(community, timeout=1, retries=0, port=port)
        try:
            return await collector.get_own_addresses("127.0.0.9")
        finally:
            await collector.aclose()

    assert run(check()) == []


def test_addresses_are_only_walked_when_asked(snmp_agent):
    """
    poll_device() is the inventory loop's whole SNMP footprint, and the walk
    costs a round trip on every device - so it happens only when the
    collector was built to collect them.
    """
    host, port, community = snmp_agent

    async def check(collect: bool):
        collector = SNMPCollector(
            community, timeout=2, retries=1, port=port,
            collect_addresses=collect
        )
        try:
            return await collector.poll_device(host)
        finally:
            await collector.aclose()

    # Absent rather than empty when it was not asked for: an empty list is
    # evidence that the device holds nothing else, and reconciling against
    # one of those would delete every address the fleet has.
    assert "own_addresses" not in run(check(False))
    assert sorted(run(check(True))["own_addresses"]) == sorted(OWN_ADDRESSES)


def test_one_poll_can_skip_the_walk_but_cannot_switch_it_on(snmp_agent):
    """
    The per-call flag is how the inventory loop walks a device once an hour
    instead of every cycle (AddressWalkSchedule). One collector is shared by
    every device on the same credential, so this has to be an argument rather
    than a flag on the object.

    It can only narrow: a caller passing True must not be able to switch on
    a walk the operator switched off with COLLECTOR_DEVICE_ADDRESSES=false.
    """
    host, port, community = snmp_agent

    async def check(built_with: bool, asked_for):
        collector = SNMPCollector(
            community, timeout=2, retries=1, port=port,
            collect_addresses=built_with
        )
        try:
            return await collector.poll_device(host, collect_addresses=asked_for)
        finally:
            await collector.aclose()

    # Feature on, this poll skipping it: the key is absent, which is what
    # leaves the stored addresses alone instead of reconciling them away.
    assert "own_addresses" not in run(check(True, False))
    # Feature on, this poll asking for it
    assert sorted(run(check(True, True))["own_addresses"]) == sorted(OWN_ADDRESSES)
    # Feature off: neither True nor None gets a walk
    assert "own_addresses" not in run(check(False, True))
    assert "own_addresses" not in run(check(False, None))


def test_counters_move_and_produce_a_rate(snmp_agent):
    """
    Two samples far enough apart must yield a plausible bps.

    This is the other half of "the walk is alive": a counter that is read but
    never changes gives every link 0 bps, which looks exactly like an idle
    network rather than a broken collector.
    """
    from datetime import datetime, timedelta

    async def check(collector, host):
        first = await collector._snmp_walk(host, IF_HC_IN_OCTETS)
        await asyncio.sleep(2.0)
        second = await collector._snmp_walk(host, IF_HC_IN_OCTETS)
        return first, second

    first, second = run(_with_collector(snmp_agent, check))
    assert len(first) == NIF and len(second) == NIF

    index = str(CABLED[0])
    before, after = int(first[index]), int(second[index])
    assert after > before, "the simulated counter did not move"

    now = datetime.utcnow()
    bps = counter_rate(
        previous=before, previous_time=now - timedelta(seconds=2),
        current=after, current_time=now,
    )
    assert bps is not None and bps > 0


# ---------------------------------------------------------------------------
# CPU and memory
#
# These are the numbers on every device row and behind two alert types, and
# for Cisco, HP and Palo Alto they were always 0%: their OIDs are table
# columns, the collector read them with a GET, and a GET on a column has no
# answer. Nothing failed - 0% is a perfectly good-looking reading - so the
# assertion here is on the value, against an agent that serves the tables
# the way the real ones do.


def _metrics(agent, community, vendor):
    host, port, _ = agent

    async def read():
        collector = SNMPCollector(community, timeout=2, retries=1, port=port)
        try:
            return await collector.get_device_metrics(host, vendor)
        finally:
            await collector.aclose()

    return run(read())


def test_cisco_cpu_and_memory_come_from_the_table_rows(snmp_agent):
    metrics = _metrics(snmp_agent, "public", "cisco_ios")
    # The busier of the two stack members, not their average
    assert metrics.cpu_percent == 37.0
    # 800 used of 2000 across both pools
    assert metrics.memory_percent == pytest.approx(40.0)


def test_hp_memory_is_allocated_over_total(snmp_agent):
    from tests.conftest import HP_COMMUNITY

    metrics = _metrics(snmp_agent, HP_COMMUNITY, "hp_aruba")
    assert metrics.cpu_percent == 22.0
    assert metrics.memory_percent == pytest.approx(75.0)


def test_host_resources_memory_counts_ram_and_not_the_disk(snmp_agent):
    from tests.conftest import HOSTRES_COMMUNITY

    metrics = _metrics(snmp_agent, HOSTRES_COMMUNITY, "paloalto")
    # Four cores averaged: one pegged core is how a dataplane normally runs
    assert metrics.cpu_percent == pytest.approx(40.0)
    # The RAM row only - the 90% full disk in the same table is not memory
    assert metrics.memory_percent == pytest.approx(25.0)


def test_a_metric_that_cannot_be_read_is_none_not_zero(snmp_agent):
    """
    0% is what an idle device reports and what an alert recovers on. A
    device that does not serve the table must not look like one.
    """
    from tests.conftest import PEER_COMMUNITY

    assert _metrics(snmp_agent, PEER_COMMUNITY, "cisco_ios") is None


def test_no_vendor_metric_is_read_as_a_scalar_unless_it_is_one():
    """
    The shape of the mistake, checked on the table itself: an OID that does
    not end in .0 is a column, and a column under a scalar key would be read
    with a GET again.
    """
    from app.core.snmp_oids import VENDOR_OIDS

    for vendor, oids in VENDOR_OIDS.items():
        for key in ("cpu", "memory"):
            if key in oids:
                assert oids[key].endswith(".0"), (
                    f"{vendor}.{key} = {oids[key]} is a table column; "
                    f"put it under {key}_table so it is walked"
                )
