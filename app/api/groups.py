"""
Device Groups API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, update
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime

from app.db.database import get_db
from app.models.group import DeviceGroup, DeviceGroupMember
from app.models.device import Device

router = APIRouter()


class GroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    parent_id: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    display_order: Optional[int] = None


class GroupResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    parent_id: Optional[int]
    color: Optional[str]
    icon: Optional[str]
    display_order: Optional[int]
    created_at: datetime
    device_count: int = 0
    
    class Config:
        from_attributes = True


class DeviceAssignment(BaseModel):
    device_ids: List[int]


def _response(group: DeviceGroup, device_count: int = 0) -> GroupResponse:
    return GroupResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        parent_id=group.parent_id,
        color=group.color,
        icon=group.icon,
        display_order=group.display_order,
        created_at=group.created_at,
        device_count=device_count
    )


async def _get_group(db: AsyncSession, group_id: int) -> DeviceGroup:
    group = (await db.execute(
        select(DeviceGroup).where(DeviceGroup.id == group_id)
    )).scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


async def _check_parent(db: AsyncSession, parent_id: Optional[int], group_id: Optional[int] = None) -> None:
    """A parent has to exist, and a group cannot end up inside itself"""
    seen = {group_id} if group_id is not None else set()
    current = parent_id
    while current is not None:
        if current in seen:
            raise HTTPException(
                status_code=400,
                detail="A group cannot be placed inside itself or one of its own sub-groups"
            )
        seen.add(current)
        row = (await db.execute(
            select(DeviceGroup.parent_id).where(DeviceGroup.id == current)
        )).first()
        if row is None:
            raise HTTPException(status_code=400, detail="Parent group not found")
        current = row[0]


@router.get("", response_model=List[GroupResponse])
async def list_groups(db: AsyncSession = Depends(get_db)):
    """List all device groups"""
    groups = (await db.execute(
        select(DeviceGroup).order_by(DeviceGroup.display_order, DeviceGroup.name)
    )).scalars().all()

    counts = dict((await db.execute(
        select(DeviceGroupMember.group_id, func.count())
        .group_by(DeviceGroupMember.group_id)
    )).all())

    return [_response(group, counts.get(group.id, 0)) for group in groups]


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    group: GroupCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new device group"""
    await _check_parent(db, group.parent_id)
    db_group = DeviceGroup(
        name=group.name,
        description=group.description,
        parent_id=group.parent_id,
        color=group.color,
        icon=group.icon
    )
    db.add(db_group)
    await db.commit()
    await db.refresh(db_group)
    
    return GroupResponse(
        id=db_group.id,
        name=db_group.name,
        description=db_group.description,
        parent_id=db_group.parent_id,
        color=db_group.color,
        icon=db_group.icon,
        display_order=db_group.display_order,
        created_at=db_group.created_at,
        device_count=0
    )


@router.get("/{group_id}/devices")
async def get_group_devices(
    group_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get devices in a group"""
    # Get group members
    result = await db.execute(
        select(DeviceGroupMember).where(DeviceGroupMember.group_id == group_id)
    )
    members = result.scalars().all()
    device_ids = [m.device_id for m in members]
    
    if not device_ids:
        return {"devices": [], "total": 0}
    
    # Get devices
    devices_result = await db.execute(
        select(Device).where(Device.id.in_(device_ids))
    )
    devices = devices_result.scalars().all()
    
    return {
        "devices": [
            {
                "id": d.id, "hostname": d.hostname, "ip_address": d.ip_address,
                "status": d.status, "device_type": d.device_type, "vendor": d.vendor
            }
            for d in sorted(devices, key=lambda d: d.hostname.lower())
        ],
        "total": len(devices)
    }


@router.post("/{group_id}/devices")
async def assign_devices_to_group(
    group_id: int,
    assignment: DeviceAssignment,
    db: AsyncSession = Depends(get_db)
):
    """Assign devices to a group"""
    # Verify group exists
    result = await db.execute(
        select(DeviceGroup).where(DeviceGroup.id == group_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Group not found")
    
    # Only devices that exist: an id that is not one would be a foreign key
    # violation at commit, and take the valid ones down with it.
    known = {row[0] for row in (await db.execute(
        select(Device.id).where(Device.id.in_(assignment.device_ids))
    )).all()} if assignment.device_ids else set()

    # Add device memberships
    added = 0
    for device_id in dict.fromkeys(assignment.device_ids):
        if device_id not in known:
            continue
        # Check if already a member
        existing = await db.execute(
            select(DeviceGroupMember).where(
                DeviceGroupMember.device_id == device_id,
                DeviceGroupMember.group_id == group_id
            )
        )
        if not existing.scalar_one_or_none():
            member = DeviceGroupMember(device_id=device_id, group_id=group_id)
            db.add(member)
            added += 1
    
    await db.commit()
    return {"status": "success", "added": added}


@router.delete("/{group_id}/devices")
async def remove_devices_from_group(
    group_id: int,
    assignment: DeviceAssignment,
    db: AsyncSession = Depends(get_db)
):
    """Remove devices from a group"""
    removed = 0
    for device_id in assignment.device_ids:
        result = await db.execute(
            select(DeviceGroupMember).where(
                DeviceGroupMember.device_id == device_id,
                DeviceGroupMember.group_id == group_id
            )
        )
        member = result.scalar_one_or_none()
        if member:
            await db.delete(member)
            removed += 1
    
    await db.commit()
    return {"status": "success", "removed": removed}


@router.put("/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: int,
    group_update: GroupUpdate,
    db: AsyncSession = Depends(get_db)
):
    """
    Rename or move a group.

    The Groups page has always had an Edit button that sent a PUT here, and
    there was no PUT: the request answered 405, the page did not look at the
    answer, and the dialog closed as if the change had been saved.
    """
    group = await _get_group(db, group_id)
    changes = group_update.model_dump(exclude_unset=True)

    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="A group needs a name")
        changes["name"] = name
    if "parent_id" in changes:
        await _check_parent(db, changes["parent_id"], group_id)

    for field, value in changes.items():
        setattr(group, field, value)
    await db.commit()
    await db.refresh(group)

    count = (await db.execute(
        select(func.count()).select_from(DeviceGroupMember)
        .where(DeviceGroupMember.group_id == group_id)
    )).scalar()
    return _response(group, count or 0)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a device group. Its devices are not deleted, only un-grouped."""
    group = await _get_group(db, group_id)

    # Sub-groups move up to this group's parent rather than blocking the
    # delete (parent_id has no ON DELETE) or being deleted along with it.
    await db.execute(
        update(DeviceGroup).where(DeviceGroup.parent_id == group_id)
        .values(parent_id=group.parent_id)
    )

    await db.delete(group)
    await db.commit()
