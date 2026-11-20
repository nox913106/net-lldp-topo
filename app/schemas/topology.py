"""
Topology schemas for API response
"""
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class PortDetail(BaseModel):
    """
    One physical cable inside a merged link.

    in_bps/out_bps and utilization_percent are None until the collector has
    two counter samples for the port, so the first cycle after a restart
    reports "not measured" rather than "idle". speed_source says whether
    bandwidth_mbps is the port's own ifHighSpeed reading or a guess from its
    name, which is the fallback for ports that could not be matched to an
    interface.
    """
    local_port: str
    remote_port: str
    bandwidth_mbps: int
    in_bps: Optional[int] = None
    out_bps: Optional[int] = None
    utilization_percent: Optional[float] = None
    speed_source: Optional[str] = None


class TopologyNode(BaseModel):
    """Node in topology graph"""
    id: str
    hostname: str
    ip_address: str
    device_type: Optional[str] = None
    vendor: Optional[str] = None
    status: str
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    alert_count: int = 0
    # The upstream device, as discovery found it (the device whose neighbour
    # table this one was learned from). The map's tree layout hangs each
    # device under it; None for a core or anything added by hand.
    parent_id: Optional[str] = None
    # For aggregated nodes
    is_aggregated: bool = False
    child_count: int = 0


class TopologyLink(BaseModel):
    """Link in topology graph"""
    id: str
    source: str  # device_a id
    target: str  # device_b id
    total_bandwidth_mbps: int
    # None while no traffic measurement exists yet for any of the link's ports
    utilization_in_percent: Optional[float] = None
    utilization_out_percent: Optional[float] = None
    status: str  # normal, warning, critical
    port_details: List[PortDetail]


class TopologyResponse(BaseModel):
    """Full topology response"""
    nodes: List[TopologyNode]
    links: List[TopologyLink]
    last_updated: datetime


class ExcludeRuleCreate(BaseModel):
    """Schema for creating exclude rule"""
    rule_type: str  # hostname_pattern, port_pattern, device_pair
    pattern: Optional[str] = None
    device_a_id: Optional[int] = None
    device_b_id: Optional[int] = None


class ExcludeRuleResponse(BaseModel):
    """Schema for exclude rule response"""
    id: int
    rule_type: str
    pattern: Optional[str] = None
    device_a_id: Optional[int] = None
    device_b_id: Optional[int] = None
    created_at: datetime
    
    class Config:
        from_attributes = True
