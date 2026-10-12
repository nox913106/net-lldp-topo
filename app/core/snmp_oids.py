"""
SNMP OID definitions for various vendors
"""

# LLDP MIB OIDs (IEEE 802.1AB)
LLDP_REM_TABLE = "1.0.8802.1.1.2.1.4.1"
LLDP_REM_CHASSIS_ID_SUBTYPE = "1.0.8802.1.1.2.1.4.1.1.4"
LLDP_REM_CHASSIS_ID = "1.0.8802.1.1.2.1.4.1.1.5"
LLDP_REM_PORT_ID_SUBTYPE = "1.0.8802.1.1.2.1.4.1.1.6"
LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"

# lldpRemManAddrTable - remote management address
# Index: timeMark.localPortNum.remIndex.addrSubtype.addrLen.address...
LLDP_REM_MAN_ADDR_IF_SUBTYPE = "1.0.8802.1.1.2.1.4.2.1.3"

# lldpLocPortTable - maps lldpLocPortNum to a human readable port name.
# lldpLocPortNum is NOT always equal to ifIndex (Cisco/HP use their own
# numbering), so the LLDP remote table must be mapped through this table.
LLDP_LOC_PORT_TABLE = "1.0.8802.1.1.2.1.3.7"
LLDP_LOC_PORT_ID_SUBTYPE = "1.0.8802.1.1.2.1.3.7.1.2"
LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"

# LLDP id subtypes (IEEE 802.1AB)
LLDP_CHASSIS_SUBTYPE_MAC = 4
LLDP_CHASSIS_SUBTYPE_NET_ADDR = 5
LLDP_PORT_SUBTYPE_MAC = 3
LLDP_PORT_SUBTYPE_NET_ADDR = 4

# CDP OIDs (Cisco)
CDP_CACHE_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
CDP_CACHE_DEVICE_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"
CDP_CACHE_PLATFORM = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"
CDP_CACHE_ADDRESS = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"
CDP_CACHE_CAPABILITIES = "1.3.6.1.4.1.9.9.23.1.2.1.1.9"

# Interface MIB OIDs
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

# ifTable index column, which is what a link trap names the port with
IF_INDEX = "1.3.6.1.2.1.2.2.1.1"

# The device's own IPv4 addresses. Read so that a trap can be matched to the
# device that sent it even when it left by an interface other than the one it
# is managed on (app/core/trap_receiver.py).
#
# ipAdEntAddr is deprecated in IP-MIB and still the one everything implements;
# the value of each row is the address itself. ipAddressIfIndex is its
# replacement, read only when the first comes back empty, and there the
# address is in the OID suffix rather than the value: the suffix is
# <InetAddressType>.<length>.<bytes>, so 1.4.10.0.0.1 is IPv4 10.0.0.1.
IP_AD_ENT_ADDR = "1.3.6.1.2.1.4.20.1.1"
IP_ADDRESS_IF_INDEX = "1.3.6.1.2.1.4.34.1.3"
INET_ADDRESS_TYPE_IPV4 = 1

# Notification OIDs (RFC 3418 / IF-MIB). snmpTrapOID.0 is the varbind every
# v2c notification carries to say what it is about; the rest are the values it
# can hold that this collector acts on.
SNMP_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"
COLD_START_TRAP = "1.3.6.1.6.3.1.1.5.1"
WARM_START_TRAP = "1.3.6.1.6.3.1.1.5.2"
LINK_DOWN_TRAP = "1.3.6.1.6.3.1.1.5.3"
LINK_UP_TRAP = "1.3.6.1.6.3.1.1.5.4"

# RFC 3584: when a v1 trap is translated to v2c, the agent-addr field of the
# v1 PDU is appended as this varbind. It is the address the device says it is,
# which can differ from the address the datagram arrived from - a device
# behind NAT, or one that fills the field from a loopback while sending from
# an egress interface.
SNMP_TRAP_ADDRESS = "1.3.6.1.6.3.18.1.3.0"

# System MIB OIDs
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"

# Vendor-specific CPU/Memory OIDs
VENDOR_OIDS = {
    "cisco_ios": {
        "cpu_5min": "1.3.6.1.4.1.9.9.109.1.1.1.1.8",
        "memory_used": "1.3.6.1.4.1.9.9.48.1.1.1.5",
        "memory_free": "1.3.6.1.4.1.9.9.48.1.1.1.6"
    },
    "cisco_nxos": {
        "cpu_5min": "1.3.6.1.4.1.9.9.109.1.1.1.1.8",
        "memory_used": "1.3.6.1.4.1.9.9.48.1.1.1.5",
        "memory_free": "1.3.6.1.4.1.9.9.48.1.1.1.6"
    },
    "fortinet": {
        "cpu": "1.3.6.1.4.1.12356.101.4.1.3.0",
        "memory": "1.3.6.1.4.1.12356.101.4.1.4.0"
    },
    "paloalto": {
        "cpu": "1.3.6.1.2.1.25.3.3.1.2",  # HOST-RESOURCES-MIB
        "memory_used": "1.3.6.1.2.1.25.2.3.1.6",
        "memory_size": "1.3.6.1.2.1.25.2.3.1.5"
    },
    "hp_aruba": {
        "cpu": "1.3.6.1.4.1.11.2.14.11.5.1.9.6.1.0",
        "memory": "1.3.6.1.4.1.11.2.14.11.5.1.1.2.1.1.1.5"
    },
    "ruckus": {
        "cpu": "1.3.6.1.4.1.25053.1.2.2.1.1.1.15.1.0",  # Ruckus CPU util
        "memory": "1.3.6.1.4.1.25053.1.2.2.1.1.1.15.2.0"  # Ruckus Memory util
    }
}

# Vendor detection patterns
VENDOR_PATTERNS = {
    "cisco ios": "cisco_ios",
    "cisco nx-os": "cisco_nxos",
    "cisco nexus": "cisco_nxos",
    "fortigate": "fortinet",
    "fortios": "fortinet",
    "palo alto": "paloalto",
    "pan-os": "paloalto",
    "procurve": "hp_aruba",
    "aruba": "hp_aruba",
    "hpe": "hp_aruba",
    "ruckus": "ruckus",
    "unleashed": "ruckus",
    "smartzone": "ruckus"
}


def detect_vendor(sys_descr: str) -> str:
    """Detect vendor from sysDescr"""
    if not sys_descr:
        return "unknown"
    
    sys_descr_lower = sys_descr.lower()
    
    for pattern, vendor in VENDOR_PATTERNS.items():
        if pattern in sys_descr_lower:
            return vendor
    
    return "unknown"
