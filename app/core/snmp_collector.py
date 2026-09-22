"""
SNMP Collector - Collects LLDP/CDP and interface data from network devices
"""
import asyncio
import logging
from typing import List, Dict, Optional, Any, Tuple
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
    SYS_NAME, SYS_DESCR, SYS_UPTIME,
    VENDOR_OIDS, detect_vendor
)

logger = logging.getLogger(__name__)

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
    """Interface statistics"""
    port_name: str
    port_index: int
    speed_mbps: int
    in_octets: int
    out_octets: int


@dataclass
class DeviceMetrics:
    """Device CPU/Memory metrics"""
    cpu_percent: float
    memory_percent: float


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
        max_repetitions: int = 25
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

    def _get_auth_data(self):
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
        return UdpTransportTarget(
            (ip, self.port), timeout=self.timeout, retries=self.retries
        )

    @staticmethod
    def _close_engine(engine) -> None:
        """Release the UDP socket held by an SNMP engine"""
        try:
            engine.closeDispatcher()
        except Exception:  # pragma: no cover - best effort cleanup
            pass

    async def _snmp_get(self, ip: str, oid: str) -> Optional[Any]:
        """Perform SNMP GET operation"""
        engine = SnmpEngine()
        try:
            error_indication, error_status, error_index, var_binds = await getCmd(
                engine,
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
        finally:
            self._close_engine(engine)

    async def _snmp_walk(self, ip: str, oid: str, max_rows: int = 10000) -> Dict[str, Any]:
        """
        Walk an OID subtree and return {index: value}.

        The index is the part of the response OID after the requested base OID,
        e.g. walking 1.3.6.1.2.1.2.2.1.2 returns {"10001": "GigabitEthernet1/0/1"}.
        """
        results: Dict[str, Any] = {}
        prefix = oid + "."

        engine = SnmpEngine()
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
        finally:
            self._close_engine(engine)

        return results

    async def _snmp_walk_getnext(self, ip: str, oid: str, max_rows: int = 10000) -> Dict[str, Any]:
        """GETNEXT based walk, used when the installed pysnmp has no walk generator"""
        results: Dict[str, Any] = {}
        prefix = oid + "."

        if nextCmd is None:  # pragma: no cover - pysnmp always ships one of them
            logger.error("Installed pysnmp exposes neither walkCmd nor nextCmd")
            return results

        engine = SnmpEngine()
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
        finally:
            self._close_engine(engine)

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
        """Get interface traffic statistics"""
        stats = []

        if_descrs = await self._snmp_walk(ip, IF_DESCR)
        if_speeds = await self._snmp_walk(ip, IF_HIGH_SPEED)
        in_octets = await self._snmp_walk(ip, IF_HC_IN_OCTETS)
        out_octets = await self._snmp_walk(ip, IF_HC_OUT_OCTETS)

        for index, descr in if_descrs.items():
            if not index.isdigit():
                continue

            speed = snmp_value_to_int(if_speeds.get(index))

            # Skip interfaces with no speed (usually management/loopback)
            if speed <= 0:
                continue

            stats.append(InterfaceStats(
                port_name=snmp_value_to_text(descr),
                port_index=int(index),
                speed_mbps=speed,
                in_octets=snmp_value_to_int(in_octets.get(index)),
                out_octets=snmp_value_to_int(out_octets.get(index))
            ))

        return stats

    async def get_device_metrics(self, ip: str, vendor: str) -> Optional[DeviceMetrics]:
        """Get device CPU and memory metrics"""
        if vendor not in VENDOR_OIDS:
            return None

        oids = VENDOR_OIDS[vendor]
        cpu_percent = 0.0
        memory_percent = 0.0

        try:
            if "cpu" in oids:
                cpu_val = await self._snmp_get(ip, oids["cpu"])
                if cpu_val is not None:
                    cpu_percent = float(snmp_value_to_int(cpu_val))
            elif "cpu_5min" in oids:
                cpu_val = await self._snmp_get(ip, oids["cpu_5min"])
                if cpu_val is not None:
                    cpu_percent = float(snmp_value_to_int(cpu_val))

            if "memory" in oids:
                mem_val = await self._snmp_get(ip, oids["memory"])
                if mem_val is not None:
                    memory_percent = float(snmp_value_to_int(mem_val))
            elif "memory_used" in oids and "memory_free" in oids:
                used = snmp_value_to_int(await self._snmp_get(ip, oids["memory_used"]))
                free = snmp_value_to_int(await self._snmp_get(ip, oids["memory_free"]))
                total = used + free
                if total > 0:
                    memory_percent = (used / total) * 100

            return DeviceMetrics(
                cpu_percent=cpu_percent,
                memory_percent=memory_percent
            )

        except Exception as e:
            logger.error(f"Failed to get metrics for {ip}: {e}")
            return None

    async def poll_device(self, ip: str) -> Dict:
        """Poll a single device for all data"""
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

        return result
