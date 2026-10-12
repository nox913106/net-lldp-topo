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
if "DATABASE_URL" not in os.environ:
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


def _snmprec(modern: bool = False) -> str:
    """
    The agent's whole MIB as snmpsim's record format.

    ``modern`` swaps the deprecated ipAdEntAddr table for ipAddressTable, so
    the fallback path in SNMPCollector.get_own_addresses is exercised against
    an agent that really only has the newer one - which is what some current
    firmware ships.
    """
    rows = [
        "1.3.6.1.2.1.1.1.0|4|Cisco IOS Software, C9300, Version 17.9.4",
        "1.3.6.1.2.1.1.2.0|6|1.3.6.1.4.1.9.1.2494",
        "1.3.6.1.2.1.1.3.0|67|123456789",
        "1.3.6.1.2.1.1.5.0|4|sim-local",
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
            f"1.0.8802.1.1.2.1.4.1.1.9.{idx}|4|{PEER}",               # remote sysName
            f"1.0.8802.1.1.2.1.4.2.1.3.{idx}.1.4.{PEER_IP}|2|2",      # mgmt address
        ]
    # The device's own addresses. 64 is snmpsim's tag for IpAddress, 2 for
    # Integer - an ipAddressTable row is indexed by the address and holds the
    # ifIndex, where an ipAddrTable row holds the address itself.
    for number, address in enumerate(OWN_ADDRESSES, start=1):
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
    log = data_dir / "agent.log"
    port = _free_port()

    argv = [
        binary,
        f"--data-dir={data_dir}",
        f"--agent-udpv4-endpoint=127.0.0.1:{port}",
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
