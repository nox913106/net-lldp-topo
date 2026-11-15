"""
Alert Profiles API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

from app.db.database import get_db
from app.models.alert import AlertProfile
from app.models.device import Device

router = APIRouter()


class ThresholdConfig(BaseModel):
    warning: float
    critical: float
    recovery_buffer: float = 10


class ProfileCreate(BaseModel):
    name: str
    description: Optional[str] = None
    thresholds: Dict[str, Any]
    is_default: bool = False


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    thresholds: Optional[Dict[str, Any]] = None
    is_default: Optional[bool] = None


class ProfileResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    thresholds: Dict[str, Any]
    is_default: bool
    
    class Config:
        from_attributes = True


@router.get("", response_model=List[ProfileResponse])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    """List all alert profiles"""
    result = await db.execute(select(AlertProfile))
    return result.scalars().all()


@router.post("", response_model=ProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    profile: ProfileCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new alert profile"""
    db_profile = AlertProfile(
        name=profile.name,
        description=profile.description,
        thresholds=profile.thresholds,
        is_default=profile.is_default
    )
    db.add(db_profile)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A profile named '{profile.name}' already exists"
        )
    await db.refresh(db_profile)
    return db_profile


@router.get("/{profile_id}", response_model=ProfileResponse)
async def get_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get a specific profile"""
    result = await db.execute(
        select(AlertProfile).where(AlertProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile with id {profile_id} not found"
        )
    
    return profile


@router.put("/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: int,
    profile_update: ProfileUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update a profile"""
    result = await db.execute(
        select(AlertProfile).where(AlertProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile with id {profile_id} not found"
        )
    
    update_data = profile_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(profile, field, value)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A profile named '{profile_update.name}' already exists"
        )
    await db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a profile"""
    result = await db.execute(
        select(AlertProfile).where(AlertProfile.id == profile_id)
    )
    profile = result.scalar_one_or_none()
    
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile with id {profile_id} not found"
        )
    
    # Devices using it go back to the default thresholds; without this the
    # foreign key refuses to delete any profile that is in use.
    await db.execute(
        update(Device).where(Device.alert_profile_id == profile_id)
        .values(alert_profile_id=None)
    )
    await db.delete(profile)
    await db.commit()
