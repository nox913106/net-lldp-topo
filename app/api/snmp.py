"""
SNMP Testing API endpoints
"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional, List, Any

from app.core.snmp_collector import SNMPCollector, snmp_value_to_text

router = APIRouter(prefix="/snmp", tags=["snmp"])

# Keep responses small enough for the UI
MAX_WALK_RESULTS = 200


class SnmpTestRequest(BaseModel):
    ip: str
    community: str = "public"
    oid: str = "1.3.6.1.2.1.1.1.0"
    method: str = "get"  # 'get' or 'walk'
    version: str = "v2c"
    timeout: int = 5
    retries: int = 1


class SnmpTestResponse(BaseModel):
    success: bool
    result: Optional[Any] = None
    count: Optional[int] = None
    error: Optional[str] = None


class LldpTestRequest(BaseModel):
    ip: str
    community: str = "public"
    version: str = "v2c"
    timeout: int = 5
    retries: int = 1


class LldpNeighborItem(BaseModel):
    local_port: str
    local_port_index: int
    remote_hostname: str
    remote_port: str
    remote_chassis_id: str
    remote_mgmt_ip: str = ""
    protocol: str = "lldp"


class LldpTestResponse(BaseModel):
    success: bool
    hostname: Optional[str] = None
    vendor: Optional[str] = None
    lldp_count: int = 0
    cdp_count: int = 0
    neighbors: List[LldpNeighborItem] = []
    hints: List[str] = []
    error: Optional[str] = None


def _build_collector(ip_request) -> SNMPCollector:
    return SNMPCollector(
        community=ip_request.community,
        timeout=ip_request.timeout,
        retries=ip_request.retries,
        snmp_version=ip_request.version
    )


@router.post("/test", response_model=SnmpTestResponse)
async def test_snmp(request: SnmpTestRequest):
    """
    Test SNMP connectivity to a device
    """
    try:
        async with _build_collector(request) as collector:
            return await _run_test(collector, request)

    except Exception as e:
        return SnmpTestResponse(success=False, error=str(e))


async def _run_test(collector: SNMPCollector, request: SnmpTestRequest) -> SnmpTestResponse:
    if request.method == "get":
        value = await collector._snmp_get(request.ip, request.oid)
        if value is None:
            return SnmpTestResponse(
                success=False,
                error=f"No response for {request.oid} (check IP, community, SNMP version and ACL)"
            )
        return SnmpTestResponse(
            success=True,
            result=f"{request.oid} = {snmp_value_to_text(value)}"
        )

    if request.method == "walk":
        walked = await collector._snmp_walk(
            request.ip, request.oid, max_rows=MAX_WALK_RESULTS
        )
        if not walked:
            return SnmpTestResponse(
                success=False,
                error=f"WALK on {request.oid} returned no rows "
                      f"(the device may not implement this MIB)"
            )

        results = [
            {"oid": f"{request.oid}.{index}", "value": snmp_value_to_text(value)}
            for index, value in walked.items()
        ]
        return SnmpTestResponse(success=True, result=results, count=len(results))

    return SnmpTestResponse(success=False, error=f"Unknown method: {request.method}")


@router.post("/lldp", response_model=LldpTestResponse)
async def test_lldp(request: LldpTestRequest):
    """
    Read the LLDP/CDP neighbor tables of a device and explain what is missing.

    Useful when the topology stays empty: it shows whether the device answers
    SNMP at all, whether it exposes LLDP-MIB, and what it reports.
    """
    try:
        async with _build_collector(request) as collector:
            return await _run_lldp_test(collector, request)

    except Exception as e:
        return LldpTestResponse(success=False, error=str(e))


async def _run_lldp_test(collector: SNMPCollector, request: LldpTestRequest) -> LldpTestResponse:
    device_info = await collector.get_device_info(request.ip)
    if not device_info:
        return LldpTestResponse(
            success=False,
            error="No SNMP response - check the IP, community string, "
                  "SNMP version and the SNMP ACL on the device"
        )

    lldp = await collector.get_lldp_neighbors(request.ip)
    cdp = await collector.get_cdp_neighbors(request.ip)

    hints: List[str] = []
    if not lldp and not cdp:
        hints.append(
            "The device answers SNMP but reports no LLDP/CDP neighbors. "
            "Verify that LLDP is enabled globally and on the interfaces "
            "(Cisco: 'lldp run', HP/Aruba: 'lldp run', Fortinet: "
            "'config system interface -> set lldp-transmission enable')."
        )
        hints.append(
            "Some devices only expose LLDP-MIB to a dedicated SNMP view or "
            "context - check that the community used here can read "
            "1.0.8802.1.1.2."
        )
    elif not lldp and cdp:
        hints.append(
            "No LLDP neighbors, but CDP neighbors are present - the device "
            "runs CDP only, which is collected as well."
        )

    neighbors = [
        LldpNeighborItem(
            local_port=n.local_port,
            local_port_index=n.local_port_index,
            remote_hostname=n.remote_hostname,
            remote_port=n.remote_port,
            remote_chassis_id=n.remote_chassis_id,
            remote_mgmt_ip=n.remote_mgmt_ip,
            protocol=n.protocol
        )
        for n in lldp + cdp
    ]

    return LldpTestResponse(
        success=True,
        hostname=device_info.hostname,
        vendor=device_info.vendor,
        lldp_count=len(lldp),
        cdp_count=len(cdp),
        neighbors=neighbors,
        hints=hints
    )
