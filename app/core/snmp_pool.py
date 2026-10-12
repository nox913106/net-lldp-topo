"""
Reuse of SNMP engines across devices.

Building a ``SnmpEngine`` costs about 36 ms of CPU in isolation - measured,
every single time, with no amortisation, because pysnmp re-reads and
byte-compiles the bundled MIB modules for each one. Under load it is worse:
polling a device with its own engine costs 227.5 ms of CPU against 65.3 ms
with a shared one, the difference being construction, teardown, and the
allocation and GC pressure of twenty live engines. The collector used to
build one per device per cycle, which at 500 devices is **81 seconds of CPU
per cycle** spent on engines. That was tolerable while the cycle ran every
300 seconds; it is not tolerable for a loop that runs every 60 (section 20 of
docs/PERFORMANCE.md).

An engine is not per-device state. It holds the UDP socket, the MIB machinery
and the request id space - none of which is specific to the device being
asked. What *is* per-device is the credential, so engines are pooled by
credential and shared by every device that uses it. One engine therefore
serves hundreds of devices concurrently: pysnmp demultiplexes answers by
request id and source address, and the per-poll table cache in SNMPCollector
is keyed and reference counted per address so two concurrent polls cannot see
each other's tables.

The pool is bounded. A fleet with more distinct credentials than
``max_collectors`` falls back to the old behaviour - a private collector,
closed when the device is done - for the overflow, so a pathological
deployment cannot hold hundreds of engines in memory (about 630 KiB each).
"""
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple

from app.core.snmp_collector import SNMPCollector

logger = logging.getLogger(__name__)

# Everything that changes how a device must be talked to. Two devices with the
# same tuple can share an engine; anything else needs its own.
CredentialKey = Tuple[Any, ...]

# More distinct credentials than this and the extras stop being pooled. A real
# fleet has one to a handful (one community per site, plus v3 users).
DEFAULT_MAX_COLLECTORS = 16

# A pooled engine whose credential no device uses any more is closed after
# this long, so editing a community in the UI does not leak an engine for the
# lifetime of the process.
DEFAULT_MAX_IDLE_SECONDS = 3600.0


class _PooledCollector:
    """One shared collector, plus what the pool needs to know about it"""

    __slots__ = ("collector", "users", "last_used")

    def __init__(self, collector: SNMPCollector):
        self.collector = collector
        self.users = 0
        self.last_used = time.monotonic()


class CollectorPool:
    """
    Hands out SNMPCollector instances, reusing one per credential.

    Used as an async context manager per device:

        async with pool.collector_for(device) as collector:
            result = await collector.poll_device(device.ip_address)

    The collector yielded may be shared with other devices being polled at the
    same time, so nothing that is per-device may be stored on it.
    """

    def __init__(
        self,
        default_community: str,
        timeout: int,
        retries: int,
        collect_traffic: bool,
        max_collectors: int = DEFAULT_MAX_COLLECTORS,
        label: str = "snmp",
        collect_addresses: bool = False
    ):
        self.default_community = default_community
        self.timeout = timeout
        self.retries = retries
        self.collect_traffic = collect_traffic
        self.collect_addresses = collect_addresses
        self.max_collectors = max(1, int(max_collectors))
        self.label = label
        self._pooled: Dict[CredentialKey, _PooledCollector] = {}
        self._overflow = 0

    # -- credentials ------------------------------------------------------

    def credential_key(self, device) -> CredentialKey:
        """The credential this device needs, as a hashable key"""
        return (
            (device.snmp_version or "v2c").lower(),
            device.snmp_community or self.default_community,
            device.snmpv3_username,
            device.snmpv3_auth_protocol,
            device.snmpv3_auth_password,
            device.snmpv3_priv_protocol,
            device.snmpv3_priv_password,
        )

    def _build(self, device) -> SNMPCollector:
        return SNMPCollector(
            community=device.snmp_community or self.default_community,
            timeout=self.timeout,
            retries=self.retries,
            snmp_version=device.snmp_version or "v2c",
            v3_username=device.snmpv3_username,
            v3_auth_protocol=device.snmpv3_auth_protocol,
            v3_auth_password=device.snmpv3_auth_password,
            v3_priv_protocol=device.snmpv3_priv_protocol,
            v3_priv_password=device.snmpv3_priv_password,
            collect_traffic=self.collect_traffic,
            collect_addresses=self.collect_addresses
        )

    # -- checkout ---------------------------------------------------------

    @asynccontextmanager
    async def collector_for(self, device):
        """Yield a collector for this device, shared when possible"""
        key = self.credential_key(device)
        entry = self._pooled.get(key)

        if entry is None and len(self._pooled) < self.max_collectors:
            entry = _PooledCollector(self._build(device))
            self._pooled[key] = entry

        if entry is None:
            # Over the bound: a private engine, closed as soon as this device
            # is done. Slower, but it keeps memory bounded.
            self._overflow += 1
            own = self._build(device)
            try:
                yield own
            finally:
                await own.aclose()
            return

        entry.users += 1
        try:
            yield entry.collector
        finally:
            entry.users -= 1
            entry.last_used = time.monotonic()

    # -- housekeeping -----------------------------------------------------

    async def prune(self, max_idle: float = DEFAULT_MAX_IDLE_SECONDS) -> int:
        """
        Close pooled collectors nothing has used for a while.

        Entries that are in use right now are left alone - closing one would
        pull the socket out from under a poll in flight.
        """
        now = time.monotonic()
        closed = 0
        for key, entry in list(self._pooled.items()):
            if entry.users > 0 or now - entry.last_used < max_idle:
                continue
            del self._pooled[key]
            await entry.collector.aclose()
            closed += 1
        if closed:
            logger.info(f"{self.label} pool: closed {closed} idle SNMP engines")
        return closed

    async def aclose(self) -> None:
        """Close every pooled collector"""
        for entry in list(self._pooled.values()):
            await entry.collector.aclose()
        self._pooled.clear()

    def stats(self) -> str:
        return (
            f"{len(self._pooled)} engines"
            + (f", {self._overflow} unpooled" if self._overflow else "")
        )
