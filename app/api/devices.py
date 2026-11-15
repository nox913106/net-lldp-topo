"""
Devices API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import asc, delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from typing import List, Optional

from app.db.database import get_db
from app.models.alert import AlertProfile
from app.models.device import Device
from app.models.group import DeviceGroupMember
from app.models.link import MergedLink
from app.schemas.device import DeviceCreate, DeviceUpdate, DeviceResponse, DeviceListResponse

router = APIRouter()


# What a list may be sorted by. A whitelist rather than getattr(Device, ...):
# the model also has relationships and methods under names a caller could
# send, and ordering by one of those is a 500.
SORTABLE = {
    "hostname": Device.hostname,
    "ip_address": Device.ip_address,
    "vendor": Device.vendor,
    "device_type": Device.device_type,
    "status": Device.status,
    "cpu_percent": Device.cpu_percent,
    "memory_percent": Device.memory_percent,
    "last_seen": Device.last_seen,
}


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    search: Optional[str] = None,
    vendor: Optional[str] = None,
    ungrouped: bool = False,
    sort_by: str = "hostname",
    sort_order: str = "asc",
    db: AsyncSession = Depends(get_db)
):
    """List all devices with optional filtering"""
    filters = []
    if status:
        filters.append(Device.status == status)
    if vendor:
        filters.append(Device.vendor == vendor)
    if search:
        search_pattern = f"%{search}%"
        filters.append(or_(
            Device.hostname.ilike(search_pattern),
            Device.ip_address.ilike(search_pattern)
        ))
    if ungrouped:
        # Devices that are in no group at all - the list the Groups page
        # assigns from.
        filters.append(Device.id.not_in(select(DeviceGroupMember.device_id)))

    column = SORTABLE.get(sort_by, Device.hostname)
    direction = desc if sort_order.lower() == "desc" else asc
    # NULLs last whichever way it is sorted: "never seen" and "not measured"
    # are not the smallest value, they are no value. The id is the
    # tie-breaker that keeps pages from overlapping.
    query = (
        select(Device).where(*filters)
        .order_by(column.is_(None), direction(column), Device.id)
    )

    total = (await db.execute(
        select(func.count(Device.id)).where(*filters)
    )).scalar()

    # Apply pagination (limit=0 means ALL)
    query = query.offset(max(skip, 0))
    if limit > 0:
        query = query.limit(limit)

    devices = (await db.execute(query)).scalars().all()
    return DeviceListResponse(devices=devices, total=total)


async def _check_profile(db: AsyncSession, profile_id: Optional[int]) -> None:
    """
    An alert profile has to exist before a device can point at it.

    Asked here rather than left to the foreign key: SQLite does not enforce
    one unless told to, so there the id of a profile that does not exist was
    simply stored, and the device silently ran on default thresholds.
    """
    if profile_id is None:
        return
    found = (await db.execute(
        select(AlertProfile.id).where(AlertProfile.id == profile_id)
    )).first()
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Alert profile {profile_id} does not exist"
        )


async def _hostname_taken(db: AsyncSession, hostname: str, except_id: Optional[int] = None) -> bool:
    query = select(Device.id).where(Device.hostname == hostname)
    if except_id is not None:
        query = query.where(Device.id != except_id)
    return (await db.execute(query)).first() is not None


@router.post("", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def create_device(
    device: DeviceCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new device (seed device for discovery)"""
    if await _hostname_taken(db, device.hostname):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device with hostname '{device.hostname}' already exists"
        )

    await _check_profile(db, device.alert_profile_id)

    db_device = Device(
        hostname=device.hostname,
        ip_address=device.ip_address,
        vendor=device.vendor,
        device_type=device.device_type,
        snmp_community=device.snmp_community,
        alert_profile_id=device.alert_profile_id,
        # Was dropped on the floor: the column's own default (True) applied
        # whatever the form said, so the checkbox on the Devices page did
        # nothing in either direction.
        auto_discover=device.auto_discover,
        status="unknown"
    )

    db.add(db_device)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not add the device: the hostname is already in use, "
                   "or the alert profile does not exist"
        )
    await db.refresh(db_device)

    return db_device


@router.get("/{device_id}/hierarchy")
async def get_device_hierarchy(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """
    Get device hierarchy (upstream ancestors + downstream children)
    Returns:
    - device: The target device info
    - ancestors: List of upstream devices (parent -> grandparent -> ...)
    - children: List of direct child devices
    """
    # Get the target device
    result = await db.execute(
        select(Device).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with id {device_id} not found"
        )
    
    # Get ancestor chain (walk up the parent_device_id)
    ancestors = []
    current = device
    while current.parent_device_id:
        parent_result = await db.execute(
            select(Device).where(Device.id == current.parent_device_id)
        )
        parent = parent_result.scalar_one_or_none()
        if parent:
            ancestors.append({
                "id": parent.id,
                "hostname": parent.hostname,
                "ip_address": parent.ip_address,
                "device_type": parent.device_type,
                "status": parent.status,
                "vendor": parent.vendor
            })
            current = parent
        else:
            break
    
    # Get ALL descendants recursively (children + grandchildren + ...)
    async def get_all_descendants(parent_id: int) -> list:
        """Recursively get all descendants of a device"""
        descendants = []
        result = await db.execute(
            select(Device).where(Device.parent_device_id == parent_id)
        )
        direct_children = result.scalars().all()
        
        for child in direct_children:
            descendants.append({
                "id": child.id,
                "hostname": child.hostname,
                "ip_address": child.ip_address,
                "device_type": child.device_type,
                "status": child.status,
                "vendor": child.vendor
            })
            # Recursively get grandchildren
            grandchildren = await get_all_descendants(child.id)
            descendants.extend(grandchildren)
        
        return descendants
    
    children_list = await get_all_descendants(device_id)
    
    return {
        "device": {
            "id": device.id,
            "hostname": device.hostname,
            "ip_address": device.ip_address,
            "device_type": device.device_type,
            "status": device.status,
            "vendor": device.vendor,
            "model": device.model,
            "firmware_version": device.firmware_version,
            "cpu_percent": device.cpu_percent,
            "memory_percent": device.memory_percent
        },
        "ancestors": ancestors,  # [parent, grandparent, ...]
        "children": children_list
    }


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific device by ID"""
    result = await db.execute(
        select(Device).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with id {device_id} not found"
        )
    
    return device


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: int,
    device_update: DeviceUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update a device"""
    result = await db.execute(
        select(Device).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with id {device_id} not found"
        )
    
    # Update fields that are provided
    update_data = device_update.model_dump(exclude_unset=True)
    new_hostname = update_data.get("hostname")
    if new_hostname and new_hostname != device.hostname \
            and await _hostname_taken(db, new_hostname, except_id=device_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Device with hostname '{new_hostname}' already exists"
        )
    await _check_profile(db, update_data.get("alert_profile_id"))
    for field, value in update_data.items():
        # These columns are NOT NULL; an explicit null means "leave it"
        if value is None and field in ("hostname", "ip_address"):
            continue
        setattr(device, field, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not save the device: the hostname is already in "
                   "use, or the alert profile does not exist"
        )
    await db.refresh(device)

    return device


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a device"""
    result = await db.execute(
        select(Device).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()
    
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device with id {device_id} not found"
        )
    
    # Things that point at this device without belonging to it. The devices
    # discovered through it keep existing, they just lose their parent -
    # devices.parent_device_id has no ON DELETE, so PostgreSQL would
    # otherwise refuse to delete any device that discovery ever went through.
    await db.execute(
        update(Device).where(Device.parent_device_id == device_id)
        .values(parent_device_id=None)
    )
    # The database cascades these on PostgreSQL; SQLite does not enforce
    # foreign keys unless asked, and a link to a device that is gone would
    # stay on the map.
    await db.execute(delete(MergedLink).where(
        or_(MergedLink.device_a_id == device_id, MergedLink.device_b_id == device_id)
    ))

    await db.delete(device)
    await db.commit()
