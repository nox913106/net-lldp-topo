"""
Discovery API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio
import logging
import socket
import uuid

from app.config import get_settings
from app.core.snmp_collector import SNMPCollector
from app.db.database import get_db

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

# In-memory job status (in production, use Redis)
discovery_jobs = {}


class DiscoveryStartRequest(BaseModel):
    seed_device_id: int


class DiscoveryStatusResponse(BaseModel):
    job_id: str
    status: str
    progress: dict
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None


async def run_discovery(job_id: str, seed_device_id: int):
    """Background task for running discovery"""
    # TODO: Implement actual discovery logic using SNMP
    discovery_jobs[job_id]["status"] = "running"
    
    # Placeholder - will be implemented in SNMP collector
    import asyncio
    await asyncio.sleep(2)
    
    discovery_jobs[job_id]["status"] = "completed"
    discovery_jobs[job_id]["completed_at"] = datetime.utcnow()
    discovery_jobs[job_id]["progress"] = {
        "discovered": 0,
        "pending": 0,
        "failed": 0
    }


@router.post("/start", response_model=DiscoveryStatusResponse)
async def start_discovery(
    request: DiscoveryStartRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Start device discovery from a seed device"""
    job_id = str(uuid.uuid4())[:8]
    
    discovery_jobs[job_id] = {
        "job_id": job_id,
        "status": "started",
        "progress": {"discovered": 0, "pending": 1, "failed": 0},
        "started_at": datetime.utcnow(),
        "completed_at": None,
        "error": None
    }
    
    background_tasks.add_task(run_discovery, job_id, request.seed_device_id)
    
    return DiscoveryStatusResponse(**discovery_jobs[job_id])


@router.get("/status", response_model=Optional[DiscoveryStatusResponse])
async def get_discovery_status(job_id: Optional[str] = None):
    """Get discovery job status"""
    if job_id and job_id in discovery_jobs:
        return DiscoveryStatusResponse(**discovery_jobs[job_id])
    
    # Return latest job if no job_id specified
    if discovery_jobs:
        latest = max(discovery_jobs.values(), key=lambda x: x["started_at"])
        return DiscoveryStatusResponse(**latest)
    
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
    try:
        from sqlalchemy import select
        from app.models.device import Device
        from datetime import datetime

        settings = get_settings()
        collector = SNMPCollector(
            community=request.community,
            timeout=settings.snmp_timeout,
            retries=settings.snmp_retries
        )

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
                    device_type="access",
                    vendor=device_info.vendor,
                    status="managed",
                    parent_device_id=parent.id if parent else None,
                    last_seen=datetime.utcnow()
                )
                db.add(device)
                added_count += 1
            else:
                device.hostname = hostname
                device.vendor = device_info.vendor
                device.status = "managed"
                device.last_seen = datetime.utcnow()

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
