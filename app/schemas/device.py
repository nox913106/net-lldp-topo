"""
Device schemas for API request/response
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class DeviceBase(BaseModel):
    """Base device schema"""
    hostname: str = Field(..., min_length=1, max_length=255)
    ip_address: str = Field(..., min_length=7, max_length=45)
    vendor: Optional[str] = None
    device_type: Optional[str] = None


class DeviceCreate(DeviceBase):
    """Schema for creating a new device"""
    snmp_community: str = Field(..., min_length=1, max_length=255)
    alert_profile_id: Optional[int] = None
    # On by default: one seed device whose neighbours are followed is how a
    # network gets into this system at all.
    auto_discover: bool = True


class DeviceUpdate(BaseModel):
    """Schema for updating a device"""
    hostname: Optional[str] = Field(None, min_length=1, max_length=255)
    ip_address: Optional[str] = Field(None, min_length=7, max_length=45)
    vendor: Optional[str] = None
    device_type: Optional[str] = None
    snmp_community: Optional[str] = Field(None, min_length=1, max_length=255)
    alert_profile_id: Optional[int] = None
    status: Optional[str] = None
    auto_discover: Optional[bool] = None


class DeviceResponse(DeviceBase):
    """Schema for device response"""
    id: int
    # Optional on the way out, required on the way in: the column is
    # nullable (an SNMPv3 device has no community), and with it required
    # here one such row made the whole device list a 500.
    snmp_community: Optional[str] = None
    alert_profile_id: Optional[int] = None
    auto_discover: Optional[bool] = None
    parent_device_id: Optional[int] = None
    status: str
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    uptime_seconds: Optional[int] = None
    last_seen: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DeviceListResponse(BaseModel):
    """Schema for device list response"""
    devices: List[DeviceResponse]
    total: int
