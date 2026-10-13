"""
Shared fixtures. The only interesting one is a real SNMP agent.

Everything SNMP in this project is a UDP conversation with a device, and the
failure that shipped to production was a walk that returned nothing without
raising (docs/TROUBLESHOOTING_LLDP.md). Mocking the SNMP layer would have
reproduced neither the bug nor its absence, so the SNMP tests talk to an
snmpsim agent over the loopback instead.
"""
import atexit
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# app/db/database.py builds its engine at import time from DATABASE_URL, so
# the test database has to be chosen before anything imports it - and it must
# never be the developer's own topomon.db, which the default URL points at.
# `not os.environ.get(...)` rather than a membership test: CI's matrix sets
# DATABASE_URL to an empty string on the SQLite leg, and an empty string is
# not a URL.
if not os.environ.get("DATABASE_URL"):
    _db_dir = tempfile.mkdtemp(prefix="topomon-tests-")
    atexit.register(shutil.rmtree, _db_dir, True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_dir}/test.db"

# The simulated chassis: 48 interfaces, three of them cabled, port 3 down.
NIF = 48
CABLED = (1, 2, 3)
DOWN_PORT = 3
PEER = "sim-peer"
PEER_IP = "127.0.0.1"
IN_RATE = 87_500_000   # octets/s = 700 Mbps = 70% of a 1 Gbps port
OUT_RATE = 1_250_000

# The addresses the simulated device holds. The management address is the one
# it is polled on; the others are what makes a trap from another interface
# recognisable (app/core/trap_receiver.py).
OWN_ADDRESSES = ("10.0.0.1", "10.255.255.1", "192.168.99.1")

# The same idea, but in 127.0.0.0/8 - all of which is local on Linux, so a
# test really can send a datagram *from* one of these. That is what makes an
# end-to-end trap test possible: the whole point of matching a device by the
# addresses it holds is that the trap arrives from one of them rather than
# from the address the device is polled on.
#
# Three recordings, selected by community name, stand in for three states of
# one switch: trap-a as it is, trap-a after someone adds an address to an
# interface, and a second switch with no address table at all. Swapping a
# device's community is how a test renumbers a device without restarting the
# simulator - what the collector sees is a different set of addresses on the
# next walk, which is exactly what a renumbering looks like to it.
TRAP_COMMUNITY = "trapsim"
TRAP_RENUMBERED_COMMUNITY = "renumbered"
TRAP_PEER_COMMUNITY = "trapsim2"
TRAP_OWN_ADDRESSES = ("127.0.0.2", "127.0.0.3")
TRAP_ADDED_ADDRESS = "127.0.0.5"
TRAP_STRANGER_ADDRESS = "127.0.0.9"
TRAP_SYS_NAME = "trap-a"
TRAP_PEER_SYS_NAME = "trap-b"
# The second device is polled here, so a trap can arrive from a *managed*
# device that is not the one a forged agent-addr claims.
TRAP_PEER_IP = "127.0.0.4"

# The neighbour the default recording advertises, served as a device of its
# own: same chassis, but naming sim-local as *its* neighbour. Two devices
# that name each other are what makes the topology engine merge a cable into
# one link instead of leaving two halves - the only part of the merge that a
# single simulated switch cannot exercise.
PEER_COMMUNITY = "peer"

@pytest.fixture(autouse=True)
def _dispose_engine():
    """
    Give every test a clean connection pool.

    These tests drive async code with asyncio.run(), so each one runs in its
    own event loop - and a pooled database connection belongs to the loop
    that opened it. Left alone, the second test to touch the database gets
    "attached to a different loop" from asyncpg. SQLite never notices, which
    is exactly why this has to be here: without it the suite passes locally
    and fails against the database the project actually runs on.
    """
    yield
    import asyncio as _asyncio

    from app.db.database import engine

    _asyncio.run(engine.dispose())


def _snmprec(modern: bool = False, own=OWN_ADDRESSES, sys_name="sim-local",
             peer=PEER, peer_ip=PEER_IP) -> str:
    """
    The agent's whole MIB as snmpsim's record format.

    ``modern`` swaps the deprecated ipAdEntAddr table for ipAddressTable, so
    the fallback path in SNMPCollector.get_own_addresses is exercised against
    an agent that really only has the newer one - which is what some current
    firmware ships.

    ``own``, ``sys_name`` and ``peer`` are what make one agent able to stand
    in for several switches: snmpsim serves a different recording per
    community, and the topology engine keys devices by sysName, so two
    communities naming each other as their LLDP neighbour are two devices
    cabled together as far as everything above SNMP is concerned.
    """
    rows = [
        "1.3.6.1.2.1.1.1.0|4|Cisco IOS Software, C9300, Version 17.9.4",
        "1.3.6.1.2.1.1.2.0|6|1.3.6.1.4.1.9.1.2494",
        "1.3.6.1.2.1.1.3.0|67|123456789",
        f"1.3.6.1.2.1.1.5.0|4|{sys_name}",
    ]
    for i in CABLED:
        # lldpLocPortId / lldpLocPortDesc
        rows += [
            f"1.0.8802.1.1.2.1.3.7.1.2.{i}|2|5",
            f"1.0.8802.1.1.2.1.3.7.1.3.{i}|4|Gi1/0/{i}",
            f"1.0.8802.1.1.2.1.3.7.1.4.{i}|4|GigabitEthernet1/0/{i}",
        ]
    for i in CABLED:
        idx = f"0.{i}.1"
        rows += [
            f"1.0.8802.1.1.2.1.4.1.1.4.{idx}|2|4",                    # chassis subtype: MAC
            f"1.0.8802.1.1.2.1.4.1.1.5.{idx}|4x|aabbcc00000{i}",       # chassis id
            f"1.0.8802.1.1.2.1.4.1.1.6.{idx}|2|5",                    # port subtype
            f"1.0.8802.1.1.2.1.4.1.1.7.{idx}|4|Gi1/0/{i}",            # remote port id
            f"1.0.8802.1.1.2.1.4.1.1.8.{idx}|4|GigabitEthernet1/0/{i}",
            f"1.0.8802.1.1.2.1.4.1.1.9.{idx}|4|{peer}",               # remote sysName
            f"1.0.8802.1.1.2.1.4.2.1.3.{idx}.1.4.{peer_ip}|2|2",      # mgmt address
        ]
    # The device's own addresses. 64 is snmpsim's tag for IpAddress, 2 for
    # Integer - an ipAddressTable row is indexed by the address and holds the
    # ifIndex, where an ipAddrTable row holds the address itself.
    for number, address in enumerate(own, start=1):
        if modern:
            rows.append(f"1.3.6.1.2.1.4.34.1.3.1.4.{address}|2|{number}")
        else:
            rows.append(f"1.3.6.1.2.1.4.20.1.1.{address}|64|{address}")

    for i in range(1, NIF + 1):
        rows += [
            f"1.3.6.1.2.1.2.2.1.2.{i}|4|GigabitEthernet1/0/{i}",
            f"1.3.6.1.2.1.2.2.1.8.{i}|2|{2 if i == DOWN_PORT else 1}",
            f"1.3.6.1.2.1.31.1.1.1.1.{i}|4|Gi1/0/{i}",
            f"1.3.6.1.2.1.31.1.1.1.15.{i}|66|1000",
            f"1.3.6.1.2.1.31.1.1.1.6.{i}|70:numeric|initial=1000000000,rate={IN_RATE}",
            f"1.3.6.1.2.1.31.1.1.1.10.{i}|70:numeric|initial=2000000000,rate={OUT_RATE}",
        ]

    # snmpsim needs real OID order, not lexicographic: with "...15.10" ahead
    # of "...15.2" a walk stops at the first OID that looks like it went
    # backwards - which is itself a way to get the silent "0 rows".
    def oid_key(row: str):
        return tuple(int(part) for part in row.split("|", 1)[0].split("."))

    return "\n".join(sorted(rows, key=oid_key)) + "\n"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def snmp_agent(tmp_path_factory):
    """
    (host, port, community) of a running snmpsim agent, or skip.

    SNMPSIM_BIN points at snmpsim-command-responder in its own virtualenv -
    see tests/README.md for why it must not share one with the project.
    """
    binary = os.environ.get("SNMPSIM_BIN") or shutil.which("snmpsim-command-responder")
    if not binary or not Path(binary).exists():
        # Skipping keeps a developer's machine usable without the simulator,
        # but it would also let CI pass having tested nothing - so CI sets
        # REQUIRE_SNMPSIM=1 and turns the same condition into a failure.
        message = (
            f"no snmpsim-command-responder to run: SNMPSIM_BIN={binary!r}. "
            "See tests/README.md."
        )
        if os.environ.get("REQUIRE_SNMPSIM"):
            pytest.fail(message, pytrace=False)
        pytest.skip(message)

    as_root = getattr(os, "geteuid", lambda: 1)() == 0

    # snmpsim refuses to stay root ("Must drop privileges to a non-privileged
    # user&group"), and it drops them *before* reading the data directory -
    # so under root the recordings have to live somewhere the unprivileged
    # user can still reach. pytest's tmp_path is 0700 and root-owned, which
    # would leave the agent running with zero recordings, answering nothing:
    # the very shape of the bug these tests exist to catch. As an ordinary
    # user (CI) none of that applies and tmp_path is the right place.
    if as_root:
        data_dir = Path(tempfile.mkdtemp(prefix="snmpsim-"))
        own_data_dir = True
    else:
        data_dir = tmp_path_factory.mktemp("snmpsim")
        own_data_dir = False

    (data_dir / "public.snmprec").write_text(_snmprec(), encoding="utf-8")
    # Same device, but with only the newer IP address table
    (data_dir / "modern.snmprec").write_text(
        _snmprec(modern=True), encoding="utf-8"
    )
    # The three the trap tests need: a switch holding loopback addresses, the
    # same switch with one more, and a second switch holding none at all.
    (data_dir / f"{TRAP_COMMUNITY}.snmprec").write_text(
        _snmprec(own=TRAP_OWN_ADDRESSES, sys_name=TRAP_SYS_NAME),
        encoding="utf-8"
    )
    (data_dir / f"{TRAP_RENUMBERED_COMMUNITY}.snmprec").write_text(
        _snmprec(own=TRAP_OWN_ADDRESSES + (TRAP_ADDED_ADDRESS,),
                 sys_name=TRAP_SYS_NAME),
        encoding="utf-8"
    )
    (data_dir / f"{TRAP_PEER_COMMUNITY}.snmprec").write_text(
        _snmprec(own=(), sys_name=TRAP_PEER_SYS_NAME), encoding="utf-8"
    )
    # The other end of the default recording's cables
    (data_dir / f"{PEER_COMMUNITY}.snmprec").write_text(
        _snmprec(own=(), sys_name=PEER, peer="sim-local", peer_ip="127.0.0.1"),
        encoding="utf-8"
    )
    log = data_dir / "agent.log"
    port = _free_port()

    argv = [
        binary,
        f"--data-dir={data_dir}",
        f"--agent-udpv4-endpoint=127.0.0.1:{port}",
        # A second address on the same agent, so two devices can be polled on
        # two addresses - which is what the trap tests need to tell "the
        # source address names this device" from "a trap claims it does".
        f"--agent-udpv4-endpoint={TRAP_PEER_IP}:{port}",
    ]
    if as_root:
        data_dir.chmod(0o755)
        for recording in data_dir.glob("*.snmprec"):
            recording.chmod(0o644)
        argv += ["--process-user=nobody", "--process-group=nogroup"]

    with log.open("w") as handle:
        proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT)

    try:
        _wait_until_answering("127.0.0.1", port, proc, log)
        yield "127.0.0.1", port, "public"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if own_data_dir:
            shutil.rmtree(data_dir, ignore_errors=True)


def _wait_until_answering(host: str, port: int, proc, log: Path, timeout: float = 30.0) -> None:
    """Block until a sysName GET comes back, so no test races the agent."""
    import asyncio

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.core.snmp_collector import SNMPCollector

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"snmpsim exited with {proc.returncode}:\n{log.read_text()}"
            )

        async def probe():
            collector = SNMPCollector("public", timeout=1, retries=0, port=port)
            try:
                return await collector.get_device_info(host)
            finally:
                await collector.aclose()

        last = asyncio.run(probe())
        if last is not None:
            return
        time.sleep(0.5)
    raise RuntimeError(f"snmpsim never answered on {host}:{port}:\n{log.read_text()}")


# -- driving the real loops ------------------------------------------------
#
# The fixtures below exist so a test can run app.collector's own cycle
# functions against the simulated agent. Everything above this line tests a
# piece; these let a test assert on what the process actually does with it -
# a cable written, an alert raised, a trap applied - which is the only level
# at which some of these failures are visible at all.


@pytest.fixture
def clean_db():
    """An empty schema, created the way a first start would create it."""
    import asyncio

    from app.db.database import Base, engine
    from app.models import alert, collector, device, group, link, profile  # noqa: F401

    async def reset():
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            # In this loop, before it closes. The connection this just used
            # goes back to the pool otherwise, and the test body runs in a
            # loop of its own - asyncpg then raises "attached to a different
            # loop" on the first query. Same reason as _dispose_engine
            # above, one step earlier: a fixture that touches the database
            # has to hand it back empty.
            await engine.dispose()

    asyncio.run(reset())
    yield


@pytest.fixture
def collector_process(snmp_agent, clean_db, monkeypatch):
    """
    app.collector, pointed at the simulated agent, with its memory cleared.

    Two things have to be dealt with to run the real loops in a test:

    * **The port.** The collector always talks to udp/161, which is right in
      production and impossible in a test - the agent is on a free high port.
      Nothing in the production path parameterises it, so the collector class
      the pool builds from is replaced with one that is already bound to the
      agent's port. The pool, the loops and the storage code are untouched.
    * **Process memory.** The backoff, the confirmation counters, the flap
      damper and the address-walk schedule are module-level by design (they
      are per process, not per cycle), so one test's state is visible to the
      next. They are cleared here rather than rebuilt, so the test exercises
      the objects the process really runs with.

    The SNMP engines are closed afterwards because each test drives its own
    event loop and a pysnmp transport belongs to the loop that opened it -
    the same reason the database engine is disposed between tests.
    """
    import asyncio
    import functools

    import app.core.snmp_pool as snmp_pool
    from app.core.damping import FlapDamper
    from app.core.snmp_collector import SNMPCollector

    _, port, _ = snmp_agent
    monkeypatch.setattr(
        snmp_pool, "SNMPCollector", functools.partial(SNMPCollector, port=port)
    )

    import app.collector as col

    def forget():
        # prune([]) is each object's own "no device exists any more", so the
        # test runs against the objects the process runs with rather than
        # against replacements built from a copied argument list.
        col.offline_backoff.prune([])
        col.fast_backoff.prune([])
        col.offline_confirm.prune([])
        col.address_walk.prune([])
        asyncio.run(col.inventory_pool.aclose())
        asyncio.run(col.fast_pool.aclose())

    forget()
    # The damper is the exception: its prune() deliberately keeps a cable
    # that is down or still inside its window, which is most of what a test
    # leaves behind. A fresh one, configured from the live one, is the only
    # clean slate - and the settings come off the object rather than from
    # settings, so this cannot drift from what the process uses.
    monkeypatch.setattr(col, "flap_damper", FlapDamper(
        transitions=col.flap_damper.transitions,
        window_seconds=col.flap_damper.window,
    ))
    try:
        yield col
    finally:
        forget()
