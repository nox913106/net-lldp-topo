"""
Discovery API endpoints.

One endpoint, on purpose. Discovery happens in two places and neither of
them is a job this API runs: the collector discovers every cycle from the
LLDP/CDP neighbours of the devices it already polls (``auto_discover``), and
the scheduler sweeps the configured subnets. What is left for the API is the
one thing an operator does by hand - "start from this IP and follow its
neighbours" - which is ``POST /manual`` below, and it answers with the result
rather than with a job id.

There used to be ``POST /start`` and ``GET /status`` for a background job.
They were a placeholder from before the collector existed: /start slept two
seconds and reported "discovered 0", which is worse than a 404 because it
looks like an answer. Removed in favour of the endpoint that does the work.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio
import logging
import socket

from app.config import get_settings
from app.core.app_settings import address_allowed, refusal
from app.core.device_type import guess_device_type
from app.core.snmp_collector import SNMPCollector
from app.db.database import get_db
from app.core.clock import utcnow

logger = logging.getLogger(__name__)

router = APIRouter()


async def resolve_hostname(hostname: str) -> Optional[str]:
    """Resolve a hostname to an IPv4 address without blocking the event loop"""
    if not hostname:
        return None
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
        return infos[0][4][0] if infos else None
    except (socket.gaierror, UnicodeError, OSError, IndexError):
        return None


class ManualDiscoveryRequest(BaseModel):
    ip: str
    community: str = "public"
    recursive: bool = True


class DeviceDiscovered(BaseModel):
    ip: str
    hostname: Optional[str] = None
    is_new: bool = False


class ManualDiscoveryResponse(BaseModel):
    success: bool
    discovered_count: int = 0
    added_count: int = 0
    devices: list = []
    error: Optional[str] = None


@router.post("/manual", response_model=ManualDiscoveryResponse)
async def manual_discover(request: ManualDiscoveryRequest, db: AsyncSession = Depends(get_db)):
    """
    Manually discover devices starting from an IP address.

    With recursive=True the LLDP/CDP neighbor table of the seed device is read
    and every neighbor that advertises a management IP (and answers SNMP with
    the same community) is added as well.
    """
    from sqlalchemy import select
    from app.models.device import Device

    if not address_allowed(request.ip):
        return ManualDiscoveryResponse(success=False, error=refusal(request.ip))

    settings = get_settings()
    collector = SNMPCollector(
        community=request.community,
        timeout=settings.snmp_timeout,
        retries=settings.snmp_retries
    )

    try:
        discovered_devices = []
        added_count = 0

        async def upsert_device(ip: str, parent: Optional[Device] = None) -> Optional[Device]:
            """Add or refresh a device, returns None when it does not answer SNMP"""
            nonlocal added_count

            device_info = await collector.get_device_info(ip)
            if not device_info:
                return None

            hostname = device_info.hostname or f"device-{ip.replace('.', '-')}"

            result = await db.execute(select(Device).where(Device.ip_address == ip))
            device = result.scalar_one_or_none()

            if device is None:
                # A device already known under this hostname but another IP
                result = await db.execute(select(Device).where(Device.hostname == hostname))
                device = result.scalar_one_or_none()

            is_new = device is None

            if is_new:
                device = Device(
                    hostname=hostname,
                    ip_address=ip,
                    snmp_community=request.community,
                    device_type=guess_device_type(hostname, device_info.vendor),
                    vendor=device_info.vendor,
                    status="managed",
                    parent_device_id=parent.id if parent else None,
                    last_seen=utcnow()
                )
                db.add(device)
                added_count += 1
            else:
                device.hostname = hostname
                device.vendor = device_info.vendor
                device.status = "managed"
                device.last_seen = utcnow()

            await db.commit()
            await db.refresh(device)

            discovered_devices.append({
                "ip": ip,
                "hostname": device.hostname,
                "is_new": is_new
            })
            return device

        seed = await upsert_device(request.ip)
        if seed is None:
            return ManualDiscoveryResponse(
                success=False,
                error=f"{request.ip} did not answer SNMP (check community / version / ACL)"
            )

        # Recursive discovery: walk the neighbor table of the seed device
        if request.recursive:
            neighbors = await collector.get_lldp_neighbors(request.ip)
            neighbors += await collector.get_cdp_neighbors(request.ip)

            seen_ips = {request.ip}
            for neighbor in neighbors:
                neighbor_ip = neighbor.remote_mgmt_ip
                if not neighbor_ip:
                    # No management address advertised - try DNS on the sysName
                    neighbor_ip = await resolve_hostname(neighbor.remote_hostname)
                if not neighbor_ip:
                    logger.info(
                        "Neighbor %s of %s has no management IP, skipped",
                        neighbor.remote_hostname, request.ip
                    )
                    continue

                if neighbor_ip in seen_ips:
                    continue
                seen_ips.add(neighbor_ip)

                if not address_allowed(neighbor_ip):
                    # The seed is inside the allowed subnets; what it is
                    # cabled to need not be, and the community is not
                    # offered to it.
                    logger.info(
                        "Neighbor %s (%s) is outside the allowed subnets, skipped",
                        neighbor.remote_hostname, neighbor_ip
                    )
                    continue

                await upsert_device(neighbor_ip, parent=seed)

        return ManualDiscoveryResponse(
            success=True,
            discovered_count=len(discovered_devices),
            added_count=added_count,
            devices=discovered_devices
        )

    except Exception as e:
        logger.exception("Manual discovery failed")
        return ManualDiscoveryResponse(
            success=False,
            error=str(e)
        )
    finally:
        # Release the SNMP engine and its UDP socket
        await collector.aclose()
