"""
Device model - Network device information
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, Enum as SQLEnum, Float,
    ForeignKey, Integer, String, UniqueConstraint
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.database import Base
from app.db.types import UTCDateTime
import enum


class DeviceStatus(str, enum.Enum):
    """Device status enumeration"""
    UNKNOWN = "unknown"
    MANAGED = "managed"
    UNMANAGED = "unmanaged"
    OFFLINE = "offline"
    EXCLUDED = "excluded"


class Device(Base):
    """Network device model"""
    __tablename__ = "devices"
    
    id = Column(Integer, primary_key=True, index=True)
    hostname = Column(String(255), unique=True, nullable=False, index=True)
    ip_address = Column(String(45), nullable=False, index=True)
    vendor = Column(String(100))
    model = Column(String(100))  # Device model (e.g., "WS-C3850-48P")
    firmware_version = Column(String(100))  # Firmware/OS version
    device_type = Column(String(50))  # router, switch, firewall
    
    # SNMP Settings
    snmp_community = Column(String(255), nullable=True)
    snmp_version = Column(String(10), default="v2c")  # "v2c" or "v3"
    snmpv3_username = Column(String(255), nullable=True)
    snmpv3_auth_protocol = Column(String(20), nullable=True)  # MD5, SHA, SHA256
    snmpv3_auth_password = Column(String(255), nullable=True)
    snmpv3_priv_protocol = Column(String(20), nullable=True)  # DES, AES, AES256
    snmpv3_priv_password = Column(String(255), nullable=True)
    
    # Auto-discovery setting (default True)
    auto_discover = Column(Boolean, default=True)
    
    # Parent device for hierarchy (e.g., access switch -> distribution -> core)
    parent_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True, index=True)
    
    # Alert profile reference
    alert_profile_id = Column(Integer, ForeignKey("alert_profiles.id"))
    
    # Status and metrics
    status = Column(String(50), default=DeviceStatus.UNKNOWN)
    cpu_percent = Column(Float)
    memory_percent = Column(Float)
    uptime_seconds = Column(BigInteger)
    
    # Timestamps
    last_seen = Column(UTCDateTime)
    created_at = Column(UTCDateTime, server_default=func.now())
    updated_at = Column(UTCDateTime, server_default=func.now(), onupdate=func.now())
    
    # Relationships
    parent = relationship("Device", remote_side=[id], backref="children", foreign_keys=[parent_device_id])
    alert_profile = relationship("AlertProfile", back_populates="devices")
    raw_links = relationship("RawLink", back_populates="local_device", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="device", cascade="all, delete-orphan")
    group_memberships = relationship("DeviceGroupMember", back_populates="device", cascade="all, delete-orphan")
    addresses = relationship("DeviceAddress", back_populates="device", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Device(id={self.id}, hostname='{self.hostname}', ip='{self.ip_address}')>"


class DeviceAddress(Base):
    """
    Another address a device answers to, besides the one it is managed on.

    This exists for one reason: a trap is matched to a device by the source
    address of its datagram, and plenty of platforms send traps from whichever
    interface the packet leaves by rather than from the address you poll them
    on. Without this, the only way to receive those traps is an
    ``snmp-server trap-source`` line on every device in the fleet - which, in
    an existing network, is a change request per switch (see
    docs/CONFIGURATION.md section 5.3).

    The rows come from the device itself: the inventory poll walks its IP
    address table and reconciles this table against what it finds - an
    address the device no longer holds is deleted, because an address that
    moves to another switch must stop matching the old one's traps.

    There is deliberately no ``last_seen``: the reconcile means a row exists
    exactly while the last successful poll saw that address, and
    ``devices.last_seen`` already says when that poll was. Keeping one here
    would cost an UPDATE per device per cycle to restate what another column
    already says. Nothing is ever learned from a
    trap, because a trap is an unauthenticated datagram - accepting addresses
    from one would let anybody attach an address of their choosing to a device
    and then send traps as it.
    """
    __tablename__ = "device_addresses"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    # Indexed because the trap receiver reads the whole table by address, and
    # unique per device so a re-poll updates rather than accumulates.
    address = Column(String(45), nullable=False, index=True)
    # Where it came from, so an operator can tell a polled fact from a
    # configured one. Today always "interface"; the column exists because
    # "which of these do I trust?" is the first question anyone asks of it.
    source = Column(String(20), default="interface")
    first_seen = Column(UTCDateTime, server_default=func.now())

    device = relationship("Device", back_populates="addresses")

    __table_args__ = (
        UniqueConstraint("device_id", "address", name="uq_device_address"),
    )

    def __repr__(self):
        return f"<DeviceAddress(device_id={self.device_id}, address='{self.address}')>"
