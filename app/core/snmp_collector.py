"""
SNMP Collector - Collects LLDP/CDP and interface data from network devices
"""
import asyncio
import logging
from typing import List, Dict, Optional, Any, Sequence, Tuple
from dataclasses import dataclass, field

from pysnmp.hlapi.asyncio import (
    SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity, getCmd
)
from pysnmp.proto import rfc1902, rfc1905

# pysnmp >= 6 exposes async generators for walking a subtree.
# Older releases only have nextCmd, so keep a fallback implementation.
try:
    from pysnmp.hlapi.asyncio import walkCmd, bulkWalkCmd
except ImportError:  # pragma: no cover - depends on pysnmp version
    walkCmd = None
    bulkWalkCmd = None

try:
    from pysnmp.hlapi.asyncio import nextCmd
except ImportError:  # pragma: no cover - depends on pysnmp version
    nextCmd = None

from app.core.snmp_oids import (
    LLDP_REM_SYS_NAME, LLDP_REM_PORT_ID, LLDP_REM_PORT_ID_SUBTYPE,
    LLDP_REM_PORT_DESC, LLDP_REM_CHASSIS_ID, LLDP_REM_CHASSIS_ID_SUBTYPE,
    LLDP_REM_MAN_ADDR_IF_SUBTYPE,
    LLDP_LOC_PORT_ID, LLDP_LOC_PORT_ID_SUBTYPE, LLDP_LOC_PORT_DESC,
    LLDP_CHASSIS_SUBTYPE_MAC, LLDP_CHASSIS_SUBTYPE_NET_ADDR,
    LLDP_PORT_SUBTYPE_MAC, LLDP_PORT_SUBTYPE_NET_ADDR,
    CDP_CACHE_DEVICE_ID, CDP_CACHE_DEVICE_PORT, CDP_CACHE_ADDRESS,
    IF_DESCR, IF_NAME, IF_HIGH_SPEED, IF_HC_IN_OCTETS, IF_HC_OUT_OCTETS,
    IF_OPER_STATUS,
    INET_ADDRESS_TYPE_IPV4, IP_ADDRESS_IF_INDEX, IP_AD_ENT_ADDR,
    SYS_NAME, SYS_DESCR, SYS_UPTIME,
    HR_STORAGE_RAM, HR_STORAGE_SIZE, HR_STORAGE_TYPE, HR_STORAGE_USED,
    VENDOR_OIDS, detect_vendor
)

logger = logging.getLogger(__name__)

# Default row cap for a single walk
_DEFAULT_MAX_ROWS = 10000

# CPU and memory tables have one row per supervisor, stack member, core or
# memory pool - a handful. The cap is only there so that a device answering
# with something unexpected cannot turn a metric read into a long walk.
_METRIC_MAX_ROWS = 256

# Tables that more than one collection step needs inside a single poll.
# Only these are cached, so the saving does not come at the price of pinning
# every walked table in memory for the whole poll.
_CACHEABLE_WALK_OIDS = frozenset({IF_DESCR, IF_NAME})

# A walked row costs roughly 400-600 bytes once the index string and the
# pysnmp value object are counted, so a large chassis is used once and then
# dropped instead of being held until the poll finishes.
_WALK_CACHE_MAX_ROWS = 2000

# Transport targets are tiny, but a collector used for recursive discovery
# touches many addresses, so the cache is bounded. One pooled collector now
# serves the whole fleet (see app/core/snmp_pool.py), so the bound has to be
# comfortably larger than the fleet or it would be cleared twice per cycle.
_MAX_CACHED_TRANSPORTS = 4096

# How many varbinds go into one GET. Keeps the request inside a single
# datagram on any sane MTU and inside what a low-end switch agent will
# answer in one PDU; an agent that still says tooBig is handled by halving.
#
# Measured on a 20 cable switch (61 varbinds): 3 requests cost 11.1 ms of CPU
# per device and one 61 varbind request costs 9.4 ms, because the cost is
# mostly per varbind rather than per request (~1 ms per extra request). Saving
# that 1.7 ms is not worth asking an agent with a small maxMsgSize for a
# response it will refuse as tooBig. See docs/PERFORMANCE.md section 20.
_MAX_GET_VARBINDS = 24

# Request varbinds, kept resolved. pysnmp resolves every OID of every request
# against its bundled MIBs before sending it, which costs 0.17 ms per varbind
# - 10.4 ms of the 21 ms a fast sample of a 20 cable switch used to cost, all
# of it spent re-resolving the same OIDs the cycle before. An ObjectType that
# has been resolved once says so, so pysnmp skips it the next time, and the
# fast loop asks for exactly the same OIDs every cycle.
#
# Only numeric OIDs go in here, so there is nothing engine-specific in a
# resolved entry and one cache can serve every collector in the process.
_VARBIND_CACHE: Dict[str, Any] = {}

# At 20 cables a device that is 61 entries, and the ifIndexes recur across
# devices, so a homogeneous fleet needs a few hundred. The bound is for the
# fleet that is not: dropped wholesale rather than one at a time, because the
# entries are interchangeable and the next cycle re-fills what it needs.
_VARBIND_CACHE_MAX = 50000


def _cached_varbind(oid: str):
    """An ObjectType for this OID, resolved once and reused"""
    var_bind = _VARBIND_CACHE.get(oid)
    if var_bind is None:
        if len(_VARBIND_CACHE) >= _VARBIND_CACHE_MAX:
            _VARBIND_CACHE.clear()
        var_bind = ObjectType(ObjectIdentity(oid))
        _VARBIND_CACHE[oid] = var_bind
    return var_bind

# Values that mean "nothing more to read here"
_EXCEPTION_VALUES = (
    rfc1905.NoSuchObject,
    rfc1905.NoSuchInstance,
    rfc1905.EndOfMibView,
)


def _is_exception_value(value: Any) -> bool:
    """True when the agent answered with noSuchObject/noSuchInstance/endOfMibView"""
    return isinstance(value, _EXCEPTION_VALUES)


def _raw_octets(value: Any) -> Optional[bytes]:
    """Return the raw bytes of an OctetString value, None for other types"""
    if isinstance(value, rfc1902.OctetString):
        try:
            return bytes(value)
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _decode_octets(raw: bytes) -> str:
    """
    Render raw octets the way a human expects to see them.

    LLDP chassis/port IDs are frequently binary (MAC address, IPv4 address),
    so a plain str() produces unreadable garbage.
    """
    if not raw:
        return ""

    text = raw.decode("utf-8", errors="ignore").replace("\x00", "").strip()
    if text and all(32 <= ord(ch) < 127 for ch in text):
        return text

    if len(raw) == 6:  # MAC address
        return ":".join(f"{b:02x}" for b in raw)
    if len(raw) == 4:  # IPv4 address
        return ".".join(str(b) for b in raw)

    return raw.hex()


def snmp_value_to_text(value: Any) -> str:
    """Convert any SNMP value into display text"""
    if value is None or _is_exception_value(value):
        return ""

    raw = _raw_octets(value)
    if raw is not None:
        return _decode_octets(raw)

    if hasattr(value, "prettyPrint"):
        return str(value.prettyPrint()).strip()
    return str(value).strip()


def snmp_value_to_int_or_none(value: Any) -> Optional[int]:
    """
    Like snmp_value_to_int, but keeps "the agent did not answer this one"
    distinguishable from a counter that really reads zero.
    """
    if value is None or _is_exception_value(value):
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def snmp_value_to_int(value: Any, default: int = 0) -> int:
    """Convert an SNMP value into an int, falling back to a default"""
    if value is None or _is_exception_value(value):
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def format_chassis_id(value: Any, subtype: int = 0) -> str:
    """Format lldpRemChassisId according to lldpRemChassisIdSubtype"""
    raw = _raw_octets(value)
    if raw is None:
        return snmp_value_to_text(value)

    if subtype == LLDP_CHASSIS_SUBTYPE_MAC and len(raw) == 6:
        return ":".join(f"{b:02x}" for b in raw)

    # networkAddress: first octet is the IANA address family (1 = IPv4)
    if subtype == LLDP_CHASSIS_SUBTYPE_NET_ADDR and len(raw) == 5 and raw[0] == 1:
        return ".".join(str(b) for b in raw[1:])

    return _decode_octets(raw)


def format_port_id(value: Any, subtype: int = 0) -> str:
    """Format lldpRemPortId according to lldpRemPortIdSubtype"""
    raw = _raw_octets(value)
    if raw is None:
        return snmp_value_to_text(value)

    if subtype == LLDP_PORT_SUBTYPE_MAC and len(raw) == 6:
        return ":".join(f"{b:02x}" for b in raw)

    if subtype == LLDP_PORT_SUBTYPE_NET_ADDR and len(raw) == 5 and raw[0] == 1:
        return ".".join(str(b) for b in raw[1:])

    return _decode_octets(raw)


def _is_ipv4(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _inet_address_from_index(index: str) -> Optional[str]:
    """
    The IPv4 address out of an ipAddressTable index, or None.

    The index is InetAddressType.InetAddress, and InetAddress is itself
    length-prefixed: "1.4.10.0.0.1" is IPv4 10.0.0.1. Anything else - IPv6,
    a length that does not match, a row this walk cannot read - is skipped
    rather than guessed at, because a wrong address here would attach one
    device's traps to another.
    """
    parts = index.split(".")
    if len(parts) < 2:
        return None
    try:
        family, length = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if family != INET_ADDRESS_TYPE_IPV4 or length != 4:
        return None
    octets = parts[2:2 + 4]
    if len(octets) != 4:
        return None
    address = ".".join(octets)
    return address if _is_ipv4(address) else None


@dataclass
class DeviceInfo:
    """Device information from SNMP"""
    hostname: str
    sys_descr: str
    uptime_seconds: int
    vendor: str


@dataclass
class LLDPNeighbor:
    """LLDP neighbor information"""
    local_port: str
    local_port_index: int
    remote_hostname: str
    remote_port: str
    remote_chassis_id: str
    protocol: str = "lldp"
    remote_port_desc: str = ""
    remote_mgmt_ip: str = ""


@dataclass
class InterfaceStats:
    """
    Interface speed and traffic counters.

    in_octets/out_octets are None when traffic collection is switched off, so
    "not measured" stays distinguishable from "counter reads zero".
    port_descr is kept alongside port_name because LLDP reports a local port
    under either spelling (ifName "Gi1/0/1", ifDescr "GigabitEthernet1/0/1").
    """
    port_name: str
    port_index: int
    speed_mbps: int
    in_octets: Optional[int] = None
    out_octets: Optional[int] = None
    port_descr: str = ""


@dataclass
class DeviceMetrics:
    """
    Device CPU/Memory metrics.

    None is "could not be read", and it is stored as NULL: a metric that was
    not measured must not look like 0%, which is what an idle device reports
    and what an alert recovers on.
    """
    cpu_percent: Optional[float]
    memory_percent: Optional[float]


@dataclass
class PortSample:
    """
    One cabled port as the fast loop sees it.

    Every field is Optional and None means "the agent did not answer this
    one", never a zero: a port whose ifOperStatus is unknown must not look
    like a port that is down, and a counter that was not returned must not
    look like an idle link.
    """
    port_index: int
    oper_status: Optional[int] = None
    in_octets: Optional[int] = None
    out_octets: Optional[int] = None


class SNMPCollector:
    """SNMP data collector for network devices - supports v1, v2c and v3"""

    def __init__(
        self,
        community: str = "public",
        timeout: int = 5,
        retries: int = 2,
        snmp_version: str = "v2c",
        v3_username: str = None,
        v3_auth_protocol: str = None,  # MD5, SHA, SHA224, SHA256, SHA384, SHA512
        v3_auth_password: str = None,
        v3_priv_protocol: str = None,  # DES, 3DES, AES, AES192, AES256
        v3_priv_password: str = None,
        port: int = 161,
        max_repetitions: int = 25,
        collect_traffic: bool = True,
        collect_addresses: bool = False
    ):
        self.community = community
        self.timeout = timeout
        self.retries = retries
        self.snmp_version = (snmp_version or "v2c").lower()
        self.v3_username = v3_username
        self.v3_auth_protocol = v3_auth_protocol
        self.v3_auth_password = v3_auth_password
        self.v3_priv_protocol = v3_priv_protocol
        self.v3_priv_password = v3_priv_password
        self.port = port
        self.max_repetitions = max_repetitions
        # Walking ifHCInOctets/ifHCOutOctets is the only reason to touch those
        # two tables; a deployment that only wants the cabling inventory can
        # switch them off and keep the real ifHighSpeed reading.
        self.collect_traffic = collect_traffic
        # Walk the device's own IP address table, so a trap that left by
        # another interface can still be matched to it (get_own_addresses).
        # Off by default here because a bare SNMPCollector is also what the
        # diagnostic tools build, and they have no use for it.
        self.collect_addresses = collect_addresses

        # One SNMP engine is reused for every request this collector makes.
        # Building an SnmpEngine costs ~25 ms of CPU and ~630 KiB of RSS
        # because pysnmp re-reads and byte-compiles the bundled MIB modules
        # for each one, and a single poll issues more than 20 requests - so
        # creating an engine per request used to dominate the poll cycle.
        self._engine = None
        self._auth_data = None
        self._transports: Dict[str, Any] = {}

        # Tables cached for the duration of a poll_device() call, keyed
        # "<ip>|<oid>" - one collector now serves several devices at once, so
        # the entries are counted per address and dropped when that address is
        # done rather than when any poll finishes. See _snmp_walk.
        self._walk_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._walk_cache_users: Dict[str, int] = {}

    # -- engine lifecycle --------------------------------------------------

    def _get_engine(self):
        """
        Return this collector's SNMP engine, building it on first use.

        Created lazily rather than in __init__ so the engine binds to the
        event loop that actually runs the requests.
        """
        if self._engine is None:
            self._engine = SnmpEngine()
        return self._engine

    async def aclose(self) -> None:
        """
        Release the SNMP engine and the UDP socket it holds.

        Idempotent. The collector stays usable afterwards and will build a
        fresh engine on the next request.
        """
        engine, self._engine = self._engine, None
        self._transports.clear()
        self._walk_cache = None
        self._walk_cache_users.clear()
        if engine is not None:
            self._close_engine(engine)

    async def __aenter__(self) -> "SNMPCollector":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    def _get_auth_data(self):
        """Authentication data for this collector, built once and reused"""
        if self._auth_data is None:
            self._auth_data = self._build_auth_data()
        return self._auth_data

    def _build_auth_data(self):
        """Get authentication data based on SNMP version"""
        if self.snmp_version == "v3" and self.v3_username:
            from pysnmp.hlapi.asyncio import (
                usmHMACMD5AuthProtocol, usmHMACSHAAuthProtocol,
                usmHMAC128SHA224AuthProtocol, usmHMAC192SHA256AuthProtocol,
                usmHMAC256SHA384AuthProtocol, usmHMAC384SHA512AuthProtocol,
                usmNoAuthProtocol,
                usmDESPrivProtocol, usmAesCfb128Protocol, usmNoPrivProtocol
            )

            auth_map = {
                "MD5": usmHMACMD5AuthProtocol,
                "SHA": usmHMACSHAAuthProtocol,
                "SHA1": usmHMACSHAAuthProtocol,
                "SHA224": usmHMAC128SHA224AuthProtocol,
                "SHA256": usmHMAC192SHA256AuthProtocol,
                "SHA384": usmHMAC256SHA384AuthProtocol,
                "SHA512": usmHMAC384SHA512AuthProtocol,
            }
            priv_map = {
                "DES": usmDESPrivProtocol,
                "AES": usmAesCfb128Protocol,
                "AES128": usmAesCfb128Protocol,
            }

            # AES192/AES256 need pysnmpcrypto; fall back to AES128 if missing
            try:
                from pysnmp.hlapi.asyncio import (
                    usmAesCfb192Protocol, usmAesCfb256Protocol, usm3DESEDEPrivProtocol
                )
                priv_map["AES192"] = usmAesCfb192Protocol
                priv_map["AES256"] = usmAesCfb256Protocol
                priv_map["3DES"] = usm3DESEDEPrivProtocol
            except ImportError:  # pragma: no cover - depends on pysnmp build
                pass

            auth_key = self.v3_auth_password or None
            priv_key = self.v3_priv_password or None

            auth_protocol = auth_map.get(
                (self.v3_auth_protocol or "").upper(),
                usmHMACSHAAuthProtocol if auth_key else usmNoAuthProtocol
            )
            priv_protocol = priv_map.get(
                (self.v3_priv_protocol or "").upper(),
                usmAesCfb128Protocol if priv_key else usmNoPrivProtocol
            )

            if not auth_key:
                auth_protocol = usmNoAuthProtocol
            if not priv_key:
                priv_protocol = usmNoPrivProtocol

            return UsmUserData(
                self.v3_username,
                authKey=auth_key,
                privKey=priv_key,
                authProtocol=auth_protocol,
                privProtocol=priv_protocol
            )

        # v1 uses mpModel=0, v2c uses mpModel=1 (the pysnmp default)
        mp_model = 0 if self.snmp_version == "v1" else 1
        return CommunityData(self.community, mpModel=mp_model)

    def _transport(self, ip: str):
        """Transport target for an address, cached per collector"""
        transport = self._transports.get(ip)
        if transport is None:
            if len(self._transports) >= _MAX_CACHED_TRANSPORTS:
                self._transports.clear()
            transport = UdpTransportTarget(
                (ip, self.port), timeout=self.timeout, retries=self.retries
            )
            self._transports[ip] = transport
        return transport

    @staticmethod
    def _close_engine(engine) -> None:
        """Release the UDP socket held by an SNMP engine"""
        try:
            engine.closeDispatcher()
        except Exception:  # pragma: no cover - best effort cleanup
            pass

    async def _snmp_get(self, ip: str, oid: str) -> Optional[Any]:
        """Perform SNMP GET operation"""
        try:
            error_indication, error_status, error_index, var_binds = await getCmd(
                self._get_engine(),
                self._get_auth_data(),
                self._transport(ip),
                ContextData(),
                ObjectType(ObjectIdentity(oid))
            )

            if error_indication or error_status:
                logger.warning(f"SNMP GET {oid} on {ip} failed: {error_indication or error_status}")
                return None

            if var_binds:
                value = var_binds[0][1]
                if _is_exception_value(value):
                    return None
                return value
            return None

        except Exception as e:
            logger.error(f"SNMP GET failed for {ip}: {e}")
            return None

    def _open_walk_cache(self, ip: str) -> None:
        """Start caching this address's tables for the duration of a poll"""
        if self._walk_cache is None:
            self._walk_cache = {}
        self._walk_cache_users[ip] = self._walk_cache_users.get(ip, 0) + 1

    def _close_walk_cache(self, ip: str) -> None:
        """
        Drop this address's cached tables once nothing is polling it.

        Only this address's entries go, because a pooled collector can be
        halfway through another device at the same time - dropping the whole
        cache there would make the other poll re-walk tables it already has.
        """
        remaining = self._walk_cache_users.get(ip, 0) - 1
        if remaining > 0:
            self._walk_cache_users[ip] = remaining
            return

        self._walk_cache_users.pop(ip, None)
        if self._walk_cache is not None:
            prefix = f"{ip}|"
            for key in [k for k in self._walk_cache if k.startswith(prefix)]:
                del self._walk_cache[key]
            if not self._walk_cache_users:
                self._walk_cache = None

    def _walk_cache_key(self, ip: str, oid: str, max_rows: int) -> Optional[str]:
        """
        The cache key for a walk, or None when the result must not be cached.

        Only the interface name tables are cached, and only when the caller
        asked for the default row cap, so a truncated result can never be
        served to a caller that wanted the full table. The address is part of
        the key so one device's table can never be served for another.
        """
        if self._walk_cache is None:
            return None
        if max_rows != _DEFAULT_MAX_ROWS or oid not in _CACHEABLE_WALK_OIDS:
            return None
        return f"{ip}|{oid}"

    async def _snmp_walk(
        self, ip: str, oid: str, max_rows: int = _DEFAULT_MAX_ROWS
    ) -> Dict[str, Any]:
        """
        Walk an OID subtree and return {index: value}.

        The index is the part of the response OID after the requested base OID,
        e.g. walking 1.3.6.1.2.1.2.2.1.2 returns {"10001": "GigabitEthernet1/0/1"}.

        Callers must treat the result as read-only: during a poll some tables
        are served from a shared per-poll cache.
        """
        cache_key = self._walk_cache_key(ip, oid, max_rows)
        if cache_key is not None and cache_key in self._walk_cache:
            return self._walk_cache[cache_key]

        results: Dict[str, Any] = {}
        prefix = oid + "."

        engine = self._get_engine()
        try:
            generator = None
            if bulkWalkCmd is not None and self.snmp_version != "v1":
                # GETBULK: one request per 25 rows instead of one per row
                generator = bulkWalkCmd(
                    engine,
                    self._get_auth_data(),
                    self._transport(ip),
                    ContextData(),
                    0, self.max_repetitions,
                    ObjectType(ObjectIdentity(oid)),
                    lexicographicMode=False,
                    lookupMib=False
                )
            elif walkCmd is not None:
                generator = walkCmd(
                    engine,
                    self._get_auth_data(),
                    self._transport(ip),
                    ContextData(),
                    ObjectType(ObjectIdentity(oid)),
                    lexicographicMode=False,
                    lookupMib=False
                )

            if generator is None:
                return await self._snmp_walk_getnext(ip, oid, max_rows)

            async for error_indication, error_status, error_index, var_binds in generator:
                if error_indication:
                    logger.warning(f"SNMP WALK {oid} on {ip}: {error_indication}")
                    break
                if error_status:
                    logger.warning(f"SNMP WALK {oid} on {ip}: {error_status.prettyPrint()}")
                    break

                done = False
                for var_bind in var_binds:
                    response_oid = str(var_bind[0])
                    value = var_bind[1]

                    if not response_oid.startswith(prefix):
                        done = True
                        break
                    if _is_exception_value(value):
                        done = True
                        break

                    results[response_oid[len(prefix):]] = value

                if done or len(results) >= max_rows:
                    break

        except Exception as e:
            logger.error(f"SNMP WALK {oid} failed for {ip}: {e}")

        if cache_key is not None and len(results) <= _WALK_CACHE_MAX_ROWS:
            self._walk_cache[cache_key] = results

        return results

    async def _snmp_walk_getnext(
        self, ip: str, oid: str, max_rows: int = _DEFAULT_MAX_ROWS
    ) -> Dict[str, Any]:
        """GETNEXT based walk, used when the installed pysnmp has no walk generator"""
        results: Dict[str, Any] = {}
        prefix = oid + "."

        if nextCmd is None:  # pragma: no cover - pysnmp always ships one of them
            logger.error("Installed pysnmp exposes neither walkCmd nor nextCmd")
            return results

        engine = self._get_engine()
        try:
            current_oid = oid

            for _ in range(max_rows):
                error_indication, error_status, error_index, var_bind_table = await nextCmd(
                    engine,
                    self._get_auth_data(),
                    self._transport(ip),
                    ContextData(),
                    ObjectType(ObjectIdentity(current_oid)),
                    lookupMib=False
                )

                if error_indication or error_status or not var_bind_table:
                    if error_indication or error_status:
                        logger.warning(
                            f"SNMP WALK {oid} on {ip}: {error_indication or error_status}"
                        )
                    break

                # nextCmd returns a table of rows; each row holds the varBinds
                first_row = var_bind_table[0]
                row = first_row if isinstance(first_row, (list, tuple)) else var_bind_table

                done = False
                for var_bind in row:
                    response_oid = str(var_bind[0])
                    value = var_bind[1]

                    if not response_oid.startswith(prefix) or _is_exception_value(value):
                        done = True
                        break

                    results[response_oid[len(prefix):]] = value
                    current_oid = response_oid

                if done:
                    break

        except Exception as e:
            logger.error(f"SNMP WALK (getnext) {oid} failed for {ip}: {e}")

        return results

    async def get_device_info(self, ip: str) -> Optional[DeviceInfo]:
        """Get basic device information"""
        sys_name = await self._snmp_get(ip, SYS_NAME)
        sys_descr = await self._snmp_get(ip, SYS_DESCR)
        sys_uptime = await self._snmp_get(ip, SYS_UPTIME)

        if not sys_name:
            return None

        sys_descr_str = snmp_value_to_text(sys_descr)
        vendor = detect_vendor(sys_descr_str)

        return DeviceInfo(
            hostname=snmp_value_to_text(sys_name),
            sys_descr=sys_descr_str,
            uptime_seconds=snmp_value_to_int(sys_uptime) // 100,
            vendor=vendor
        )

    async def get_local_port_map(self, ip: str) -> Dict[str, Tuple[str, int]]:
        """
        Build {lldpLocPortNum: (port_name, ifIndex)}.

        lldpLocPortNum is the index used by the LLDP remote table and is not
        necessarily the ifIndex, so it is resolved through lldpLocPortDesc /
        lldpLocPortId first and only then through ifName / ifDescr.
        """
        loc_port_descs = await self._snmp_walk(ip, LLDP_LOC_PORT_DESC)
        loc_port_ids = await self._snmp_walk(ip, LLDP_LOC_PORT_ID)
        loc_port_subtypes = await self._snmp_walk(ip, LLDP_LOC_PORT_ID_SUBTYPE)
        if_names = await self._snmp_walk(ip, IF_NAME)
        if_descrs = await self._snmp_walk(ip, IF_DESCR)

        # port name (lower case) -> ifIndex, so a port name coming from LLDP can
        # be mapped back to the interface it belongs to
        name_to_ifindex: Dict[str, int] = {}
        for table in (if_descrs, if_names):
            for index, value in table.items():
                name = snmp_value_to_text(value)
                if name and index.isdigit():
                    name_to_ifindex.setdefault(name.lower(), int(index))

        port_map: Dict[str, Tuple[str, int]] = {}
        port_nums = set(loc_port_descs) | set(loc_port_ids)

        for port_num in port_nums:
            name = snmp_value_to_text(loc_port_descs.get(port_num))

            if not name and port_num in loc_port_ids:
                subtype = snmp_value_to_int(loc_port_subtypes.get(port_num))
                name = format_port_id(loc_port_ids[port_num], subtype)

            if not name:
                name = snmp_value_to_text(if_names.get(port_num)) or \
                    snmp_value_to_text(if_descrs.get(port_num))

            if not name:
                name = f"Port{port_num}"

            if_index = name_to_ifindex.get(name.lower())
            if if_index is None:
                # Some vendors index lldpLocPortTable by ifIndex directly
                if_index = int(port_num) if port_num.isdigit() else 0

            port_map[port_num] = (name, if_index)

        # Devices without lldpLocPortTable: fall back to the interface table
        if not port_map:
            for index, value in (if_names or if_descrs).items():
                name = snmp_value_to_text(value)
                if name and index.isdigit():
                    port_map[index] = (name, int(index))

        return port_map

    async def get_lldp_mgmt_addresses(self, ip: str) -> Dict[str, str]:
        """
        Get {lldp_remote_index: management IPv4} from lldpRemManAddrTable.

        The table index is
        timeMark.localPortNum.remIndex.addrSubtype.addrLen.address...
        so the neighbour's management IP is carried in the index itself.
        """
        addresses: Dict[str, str] = {}

        table = await self._snmp_walk(ip, LLDP_REM_MAN_ADDR_IF_SUBTYPE)
        for index in table:
            parts = index.split(".")
            if len(parts) < 6:
                continue

            try:
                addr_subtype = int(parts[3])
                addr_len = int(parts[4])
            except ValueError:
                continue

            # 1 = IPv4 (IANA address family numbers)
            if addr_subtype != 1 or addr_len != 4:
                continue

            addr_parts = parts[5:5 + addr_len]
            if len(addr_parts) != addr_len:
                continue

            rem_index = ".".join(parts[:3])
            addresses.setdefault(rem_index, ".".join(addr_parts))

        return addresses

    async def get_own_addresses(self, ip: str) -> List[str]:
        """
        Every IPv4 address this device holds, from its own IP address table.

        Read so that a trap can be matched to the device that sent it even
        when it left by an interface other than the one it is managed on,
        which is most platforms' default (docs/CONFIGURATION.md section 5.3).

        Two tables, because no single one is universal: ipAdEntAddr is
        deprecated and implemented everywhere, ipAddressTable is its
        replacement and the only one some newer agents have. The second is
        walked only when the first comes back empty, so the normal device
        costs one walk of a handful of rows.

        Loopbacks and link-local addresses are kept: a device that sources
        traps from 127.0.0.1 is broken either way, but 169.254.x and the
        loopback *range* are exactly what some stacked switches put in the
        agent-addr field, and matching one of those to the device that sent
        it is the whole point. The address this device is polled on is not
        added here - the trap receiver already knows that one.
        """
        found: List[str] = []
        seen = set()

        for index, value in (await self._snmp_walk(ip, IP_AD_ENT_ADDR)).items():
            # The value is the address; the index is the same address again.
            address = snmp_value_to_text(value) or index
            if _is_ipv4(address) and address not in seen:
                seen.add(address)
                found.append(address)

        if found:
            return found

        for index in await self._snmp_walk(ip, IP_ADDRESS_IF_INDEX):
            address = _inet_address_from_index(index)
            if address and address not in seen:
                seen.add(address)
                found.append(address)

        return found

    async def get_lldp_neighbors(self, ip: str) -> List[LLDPNeighbor]:
        """Get LLDP neighbor information"""
        chassis_ids = await self._snmp_walk(ip, LLDP_REM_CHASSIS_ID)
        chassis_subtypes = await self._snmp_walk(ip, LLDP_REM_CHASSIS_ID_SUBTYPE)
        port_ids = await self._snmp_walk(ip, LLDP_REM_PORT_ID)
        port_subtypes = await self._snmp_walk(ip, LLDP_REM_PORT_ID_SUBTYPE)
        port_descs = await self._snmp_walk(ip, LLDP_REM_PORT_DESC)
        sys_names = await self._snmp_walk(ip, LLDP_REM_SYS_NAME)

        # Every row of lldpRemTable shares the same index, but not every device
        # fills in every column (sysName is often missing on APs and phones),
        # so collect the indexes from all of them.
        indexes = set(chassis_ids) | set(port_ids) | set(sys_names)
        if not indexes:
            logger.info(f"No LLDP neighbors reported by {ip}")
            return []

        port_map = await self.get_local_port_map(ip)
        mgmt_addrs = await self.get_lldp_mgmt_addresses(ip)

        neighbors = []

        for index in sorted(indexes):
            # Index format: lldpRemTimeMark.lldpRemLocalPortNum.lldpRemIndex
            parts = index.split(".")
            if len(parts) < 2:
                continue

            local_port_num = parts[1]
            local_port, local_port_index = port_map.get(
                local_port_num,
                (f"Port{local_port_num}", int(local_port_num) if local_port_num.isdigit() else 0)
            )

            chassis_id = format_chassis_id(
                chassis_ids.get(index),
                snmp_value_to_int(chassis_subtypes.get(index))
            )
            remote_port = format_port_id(
                port_ids.get(index),
                snmp_value_to_int(port_subtypes.get(index))
            )
            remote_port_desc = snmp_value_to_text(port_descs.get(index))
            remote_hostname = snmp_value_to_text(sys_names.get(index))

            # Devices that do not advertise the sysName TLV still have to be
            # identified somehow, otherwise the link is silently dropped
            if not remote_hostname:
                remote_hostname = chassis_id or remote_port or f"lldp-{index}"

            mgmt_ip = mgmt_addrs.get(".".join(parts[:3]), "")
            if not mgmt_ip and _is_ipv4(chassis_id):
                mgmt_ip = chassis_id

            neighbors.append(LLDPNeighbor(
                local_port=local_port,
                local_port_index=local_port_index,
                remote_hostname=remote_hostname,
                remote_port=remote_port or remote_port_desc,
                remote_chassis_id=chassis_id,
                protocol="lldp",
                remote_port_desc=remote_port_desc,
                remote_mgmt_ip=mgmt_ip
            ))

        logger.info(f"{ip}: found {len(neighbors)} LLDP neighbors")
        return neighbors

    async def get_cdp_neighbors(self, ip: str) -> List[LLDPNeighbor]:
        """Get CDP neighbor information (Cisco devices)"""
        device_ids = await self._snmp_walk(ip, CDP_CACHE_DEVICE_ID)
        if not device_ids:
            return []

        device_ports = await self._snmp_walk(ip, CDP_CACHE_DEVICE_PORT)
        addresses = await self._snmp_walk(ip, CDP_CACHE_ADDRESS)
        if_descrs = await self._snmp_walk(ip, IF_DESCR)
        if_names = await self._snmp_walk(ip, IF_NAME)

        neighbors = []

        for index, device_id in device_ids.items():
            # Index format: ifIndex.cdpCacheDeviceIndex
            parts = index.split(".")
            local_port_index = int(parts[0]) if parts and parts[0].isdigit() else 0
            local_port = snmp_value_to_text(if_descrs.get(str(local_port_index))) or \
                snmp_value_to_text(if_names.get(str(local_port_index))) or \
                f"Port{local_port_index}"

            # cdpCacheAddress is a raw 4 byte IPv4 address
            mgmt_ip = ""
            raw_addr = _raw_octets(addresses.get(index))
            if raw_addr and len(raw_addr) == 4:
                mgmt_ip = ".".join(str(b) for b in raw_addr)

            neighbors.append(LLDPNeighbor(
                local_port=local_port,
                local_port_index=local_port_index,
                remote_hostname=snmp_value_to_text(device_id),
                remote_port=snmp_value_to_text(device_ports.get(index)),
                remote_chassis_id="",
                protocol="cdp",
                remote_mgmt_ip=mgmt_ip
            ))

        logger.info(f"{ip}: found {len(neighbors)} CDP neighbors")
        return neighbors

    async def get_interface_stats(self, ip: str) -> List[InterfaceStats]:
        """
        Get per-interface speed and, unless disabled, traffic counters.

        ifDescr and ifName are served from the per-poll walk cache (the LLDP
        port mapping already walked them), so the cost of this step is
        ifHighSpeed plus - when collect_traffic is on - the two octet tables.
        """
        stats = []

        if_descrs = await self._snmp_walk(ip, IF_DESCR)
        if_names = await self._snmp_walk(ip, IF_NAME)
        if_speeds = await self._snmp_walk(ip, IF_HIGH_SPEED)

        if self.collect_traffic:
            in_octets = await self._snmp_walk(ip, IF_HC_IN_OCTETS)
            out_octets = await self._snmp_walk(ip, IF_HC_OUT_OCTETS)
        else:
            in_octets = {}
            out_octets = {}

        for index in set(if_descrs) | set(if_names):
            if not index.isdigit():
                continue

            speed = snmp_value_to_int(if_speeds.get(index))

            # Skip interfaces with no speed (usually management/loopback)
            if speed <= 0:
                continue

            descr = snmp_value_to_text(if_descrs.get(index))
            name = snmp_value_to_text(if_names.get(index)) or descr

            stats.append(InterfaceStats(
                port_name=name,
                port_descr=descr,
                port_index=int(index),
                speed_mbps=speed,
                in_octets=(
                    snmp_value_to_int(in_octets[index])
                    if index in in_octets else None
                ),
                out_octets=(
                    snmp_value_to_int(out_octets[index])
                    if index in out_octets else None
                )
            ))

        return stats

    async def _column_numbers(self, ip: str, oid: str) -> Dict[str, int]:
        """A table column as {instance: number}, without the rows that are not one"""
        numbers: Dict[str, int] = {}
        for index, value in (
            await self._snmp_walk(ip, oid, max_rows=_METRIC_MAX_ROWS)
        ).items():
            number = snmp_value_to_int_or_none(value)
            if number is not None:
                numbers[index] = number
        return numbers

    async def _percent_scalar(self, ip: str, oid: str) -> Optional[float]:
        value = await self._snmp_get(ip, oid)
        number = snmp_value_to_int_or_none(value) if value is not None else None
        return float(number) if number is not None else None

    async def get_device_metrics(self, ip: str, vendor: str) -> Optional[DeviceMetrics]:
        """
        CPU and memory as percentages, or None for whichever could not be read.

        The vendor table (app/core/snmp_oids.py) says for each metric whether
        it is a scalar or a table column. That distinction is the whole job
        here: a column has no value of its own, so reading it with a GET
        answers noSuchInstance on every device - and that used to be turned
        into 0.0, which is how Cisco, HP and Palo Alto devices came to report
        an idle CPU and empty memory for ever.
        """
        if vendor not in VENDOR_OIDS:
            return None

        oids = VENDOR_OIDS[vendor]
        cpu_percent: Optional[float] = None
        memory_percent: Optional[float] = None

        try:
            if "cpu" in oids:
                cpu_percent = await self._percent_scalar(ip, oids["cpu"])
            elif "cpu_table" in oids:
                loads = list((await self._column_numbers(ip, oids["cpu_table"])).values())
                if loads:
                    cpu_percent = float(
                        max(loads) if oids.get("cpu_reduce") == "max"
                        else sum(loads) / len(loads)
                    )

            if "memory" in oids:
                memory_percent = await self._percent_scalar(ip, oids["memory"])
            elif "memory_used_table" in oids:
                used = await self._column_numbers(ip, oids["memory_used_table"])
                if "memory_free_table" in oids:
                    free = await self._column_numbers(ip, oids["memory_free_table"])
                    # Only the pools that answered both questions
                    pools = used.keys() & free.keys()
                    total = sum(used[i] + free[i] for i in pools)
                    in_use = sum(used[i] for i in pools)
                else:
                    size = await self._column_numbers(ip, oids["memory_total_table"])
                    pools = used.keys() & size.keys()
                    total = sum(size[i] for i in pools)
                    in_use = sum(used[i] for i in pools)
                if total > 0:
                    memory_percent = (in_use / total) * 100
            elif oids.get("host_resources"):
                memory_percent = await self._host_resources_memory(ip)

            if cpu_percent is None and memory_percent is None:
                return None
            return DeviceMetrics(
                cpu_percent=cpu_percent,
                memory_percent=memory_percent
            )

        except Exception as e:
            logger.error(f"Failed to get metrics for {ip}: {e}")
            return None

    async def _host_resources_memory(self, ip: str) -> Optional[float]:
        """
        RAM in use from HOST-RESOURCES-MIB.

        hrStorageTable lists disks, swap and buffers alongside RAM, in
        allocation units of their own, so the rows have to be filtered by
        hrStorageType rather than summed - used/size is a ratio, so the unit
        cancels out as long as both come from the same row.
        """
        types = await self._snmp_walk(ip, HR_STORAGE_TYPE, max_rows=_METRIC_MAX_ROWS)
        ram = [
            index for index, value in types.items()
            if snmp_value_to_text(value).strip(".") == HR_STORAGE_RAM
        ]
        if not ram:
            return None
        size = await self._column_numbers(ip, HR_STORAGE_SIZE)
        used = await self._column_numbers(ip, HR_STORAGE_USED)
        total = sum(size.get(i, 0) for i in ram if i in used)
        in_use = sum(used[i] for i in ram if i in size)
        return (in_use / total) * 100 if total > 0 else None

    async def _snmp_get_many(
        self, ip: str, oids: List[str]
    ) -> Optional[Dict[str, Any]]:
        """
        GET several OIDs in one request. Returns {oid: value}.

        None means the exchange itself failed - timeout, unreachable, wrong
        credentials - so the caller can treat the device as not answering. A
        dict that is missing some of the OIDs means the device answered and
        does not have those instances, which is the normal answer for a port
        that has been removed since the last inventory poll.

        MIB lookup is off: the response OIDs are the keys, and resolving each
        one against the bundled MIBs costs CPU for a name nothing reads.
        """
        if not oids:
            return {}

        try:
            error_indication, error_status, error_index, var_binds = await getCmd(
                self._get_engine(),
                self._get_auth_data(),
                self._transport(ip),
                ContextData(),
                *[_cached_varbind(oid) for oid in oids],
                lookupMib=False
            )
        except Exception as e:
            logger.error(f"SNMP GET ({len(oids)} OIDs) failed for {ip}: {e}")
            return None

        if error_indication:
            # No answer at all. Splitting the request would only multiply the
            # timeout, so this is reported as "the device did not answer".
            logger.warning(f"SNMP GET on {ip} failed: {error_indication}")
            return None

        if error_status:
            # The device answered but refused the request as a whole: tooBig
            # from an agent with a small PDU limit, or - on SNMPv1, which has
            # no per-varbind exception values - noSuchName because one of the
            # ports no longer exists. Both are fixed by asking for less.
            if len(oids) > 1:
                half = len(oids) // 2
                left = await self._snmp_get_many(ip, oids[:half])
                right = await self._snmp_get_many(ip, oids[half:])
                if left is None and right is None:
                    return None
                values = dict(left or {})
                values.update(right or {})
                return values
            logger.debug(
                f"SNMP GET {oids[0]} on {ip}: {error_status.prettyPrint()}"
            )
            return {}

        values = {}
        for var_bind in var_binds:
            value = var_bind[1]
            if _is_exception_value(value):
                continue
            values[str(var_bind[0])] = value
        return values

    async def get_port_samples(
        self, ip: str, port_indexes: Sequence[int]
    ) -> Optional[Dict[int, PortSample]]:
        """
        Read link state and traffic counters for named ports only.

        This is the whole SNMP footprint of a fast cycle: GETs against the
        ifIndexes the inventory poll already recorded on each cable, instead
        of walking the interface tables. About 3 varbinds per cable, against
        the ~500 a full inventory poll walks - see docs/PERFORMANCE.md
        section 20.

        Returns None when the device did not answer, which is how the caller
        tells "unreachable" from "those ports are gone".
        """
        wanted = sorted({int(i) for i in port_indexes if i is not None})

        # sysUptime rides along in the first request as the liveness probe, so
        # a device with no cables recorded yet is still checked, and a device
        # that answers nothing stays distinguishable from one whose ports have
        # all been removed.
        oids = [SYS_UPTIME]
        for index in wanted:
            oids.append(f"{IF_OPER_STATUS}.{index}")
            if self.collect_traffic:
                oids.append(f"{IF_HC_IN_OCTETS}.{index}")
                oids.append(f"{IF_HC_OUT_OCTETS}.{index}")

        values: Dict[str, Any] = {}
        for start in range(0, len(oids), _MAX_GET_VARBINDS):
            answered = await self._snmp_get_many(
                ip, oids[start:start + _MAX_GET_VARBINDS]
            )
            if answered is None:
                if start == 0:
                    return None  # never answered: the device is unreachable
                # Answered, then stopped. Keep what was measured rather than
                # throwing the whole device away.
                logger.warning(f"{ip}: stopped answering mid-sample")
                break
            values.update(answered)

        if SYS_UPTIME not in values:
            return None

        return {
            index: PortSample(
                port_index=index,
                oper_status=snmp_value_to_int_or_none(
                    values.get(f"{IF_OPER_STATUS}.{index}")
                ),
                in_octets=snmp_value_to_int_or_none(
                    values.get(f"{IF_HC_IN_OCTETS}.{index}")
                ),
                out_octets=snmp_value_to_int_or_none(
                    values.get(f"{IF_HC_OUT_OCTETS}.{index}")
                ),
            )
            for index in wanted
        }

    async def poll_device(
        self, ip: str, collect_addresses: Optional[bool] = None
    ) -> Dict:
        """
        Poll a single device for all data.

        For the duration of the call the interface name tables are cached, so
        the three steps that need them (LLDP port mapping, CDP and interface
        statistics) share one walk instead of walking them again each time.

        `collect_addresses` is per call because one collector is shared by
        every device using the same credential (app/core/snmp_pool.py), so
        "walk this device's addresses this time" cannot be a flag on the
        object. It can only narrow what the collector was built with: False
        skips a walk this time, None leaves the decision to the collector,
        and True still does nothing when COLLECTOR_DEVICE_ADDRESSES is off -
        a caller must not be able to switch on a feature the operator
        switched off.
        """
        self._open_walk_cache(ip)
        try:
            return await self._poll_device(ip, collect_addresses)
        finally:
            # Do not hold this device's tables between polls
            self._close_walk_cache(ip)

    async def _poll_device(
        self, ip: str, collect_addresses: Optional[bool] = None
    ) -> Dict:
        result = {
            "ip": ip,
            "success": False,
            "device_info": None,
            "lldp_neighbors": [],
            "cdp_neighbors": [],
            "interface_stats": [],
            "metrics": None
        }

        # Get device info
        device_info = await self.get_device_info(ip)
        if not device_info:
            return result

        result["device_info"] = device_info
        result["success"] = True

        # Get neighbors
        result["lldp_neighbors"] = await self.get_lldp_neighbors(ip)

        # Try CDP for Cisco devices, and for anything else that reported no
        # LLDP neighbors at all
        if "cisco" in device_info.vendor or not result["lldp_neighbors"]:
            result["cdp_neighbors"] = await self.get_cdp_neighbors(ip)

        # Get interface stats
        result["interface_stats"] = await self.get_interface_stats(ip)

        # Get metrics
        result["metrics"] = await self.get_device_metrics(ip, device_info.vendor)

        # The addresses this device answers to, for matching its traps. The
        # key is present only when the walk happened, so the caller can tell
        # "this device holds no other address" (an empty list) from "nobody
        # asked" (absent) - an empty list is evidence, and reconciling
        # against it would delete the addresses of every device.
        if self.collect_addresses and collect_addresses is not False:
            result["own_addresses"] = await self.get_own_addresses(ip)

        return result
