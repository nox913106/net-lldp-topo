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
