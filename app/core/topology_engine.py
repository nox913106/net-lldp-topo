"""
Topology Engine - Builds and manages network topology graph
Handles link merging, utilization calculation, and topology queries
"""
import logging
import re
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_

from app.models.device import Device
from app.models.link import RawLink, MergedLink
from app.models.group import DeviceGroupMember
from app.core.traffic import utilization_percent

logger = logging.getLogger(__name__)


@dataclass
class PortInfo:
    """
    One physical cable inside a merged link, seen from device_a.

    in_bps/out_bps are None until the collector has two counter samples for
    the port, so that "not measured yet" is not reported as "idle".
    """
    local_port: str
    remote_port: str
    bandwidth_mbps: int
    in_bps: Optional[int] = None
    out_bps: Optional[int] = None
    utilization_percent: Optional[float] = None
    speed_source: str = "ifHighSpeed"


@dataclass
class MergedLinkInfo:
    """Merged link between two devices"""
    device_a_id: int
    device_b_id: int
    device_a_hostname: str
    device_b_hostname: str
    total_bandwidth_mbps: int
    current_in_bps: Optional[int]
    current_out_bps: Optional[int]
    utilization_in_percent: Optional[float]
    utilization_out_percent: Optional[float]
    port_details: List[PortInfo] = field(default_factory=list)


class TopologyEngine:
    """
    Builds and manages network topology
    Merges multiple links between same device pairs
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def merge_links(self) -> List[MergedLinkInfo]:
        """
        Merge raw links into aggregated links between device pairs

        Example:
            DeviceA:Gi0/1 <-> DeviceB:Gi0/1 (1Gbps)
            DeviceA:Gi0/2 <-> DeviceB:Gi0/2 (1Gbps)

            Result:
            DeviceA <-> DeviceB (2Gbps total)
        """
        # Every device once, indexed both ways: by hostname to resolve the
        # neighbour a raw link names, and by id to build the result without a
        # query per device per link. Only the two columns the merge needs, so
        # the whole fleet costs a few hundred KiB rather than a full set of
        # ORM instances.
        devices = (await self.db.execute(
            select(Device.id, Device.hostname)
        )).all()
        device_by_hostname = {hostname: did for did, hostname in devices}
        device_by_id = {did: hostname for did, hostname in devices}

        # Same for the links: the merge reads six columns, and at 500 devices
        # there are tens of thousands of rows.
        raw_links = (await self.db.execute(
            select(
                RawLink.local_device_id, RawLink.local_port,
                RawLink.remote_hostname, RawLink.remote_port,
                RawLink.bandwidth_mbps, RawLink.in_bps, RawLink.out_bps
            )
        )).all()

        # Group links by device pair (sorted to ensure consistent key)
        link_groups: Dict[Tuple[int, int], List[RawLink]] = {}

        for raw_link in raw_links:
            local_id = raw_link.local_device_id
            if local_id not in device_by_id:
                continue

            remote_id = device_by_hostname.get(raw_link.remote_hostname)
            if remote_id is None:
                # Remote device not in our database yet
                continue
            if remote_id == local_id:
                # A device advertising itself (stacked member, loop) is not a
                # link between two devices
                continue

            key = (local_id, remote_id) if local_id < remote_id else (remote_id, local_id)
            link_groups.setdefault(key, []).append(raw_link)

        # Build merged links
        return [
            self._merge_one_pair(
                device_a_id, device_by_id[device_a_id],
                device_b_id, device_by_id[device_b_id],
                links
            )
            for (device_a_id, device_b_id), links in link_groups.items()
        ]

    def _merge_one_pair(
        self,
        device_a_id: int, device_a_hostname: str,
        device_b_id: int, device_b_hostname: str,
        links: List
    ) -> MergedLinkInfo:
        """
        Aggregate the raw links between one pair of devices.

        When both ends are managed, every cable between them is reported twice
        - once by each end - so adding up all the raw links reports double the
        bandwidth that is physically installed, and halves every utilisation
        figure derived from it. Only one end is counted. Matching the two
        reports of the same cable by port name is not reliable (one end says
        "Gi1/0/1", the other "GigabitEthernet1/0/1"), so the end that sees more
        cables is the one that is trusted; with LLDP healthy on both ends they
        see the same number and the choice does not matter.
        """
        a_side = [l for l in links if l.local_device_id == device_a_id]
        b_side = [l for l in links if l.local_device_id == device_b_id]

        if len(b_side) > len(a_side):
            chosen, from_a = b_side, False
        else:
            chosen, from_a = a_side, True

        port_details = []
        total_bandwidth = 0
        total_in_bps = None
        total_out_bps = None

        for link in chosen:
            # ifHighSpeed as the device reported it; the port name is only a
            # fallback for ports the collector could not match to an interface
            if link.bandwidth_mbps and link.bandwidth_mbps > 0:
                bandwidth = link.bandwidth_mbps
                speed_source = "ifHighSpeed"
            else:
                bandwidth = self._estimate_bandwidth(link.local_port)
                speed_source = "port_name"
            total_bandwidth += bandwidth

            # "in" means traffic entering device_a. What the far end calls
            # incoming is outgoing from device_a's point of view.
            in_bps = link.in_bps if from_a else link.out_bps
            out_bps = link.out_bps if from_a else link.in_bps

            if in_bps is not None:
                total_in_bps = (total_in_bps or 0) + in_bps
            if out_bps is not None:
                total_out_bps = (total_out_bps or 0) + out_bps

            local_port = link.local_port if from_a else link.remote_port
            remote_port = link.remote_port if from_a else link.local_port

            port_details.append(PortInfo(
                local_port=local_port,
                remote_port=remote_port,
                bandwidth_mbps=bandwidth,
                in_bps=in_bps,
                out_bps=out_bps,
                utilization_percent=utilization_percent(
                    max(in_bps or 0, out_bps or 0)
                    if (in_bps is not None or out_bps is not None) else None,
                    bandwidth
                ),
                speed_source=speed_source
            ))

        return MergedLinkInfo(
            device_a_id=device_a_id,
            device_b_id=device_b_id,
            device_a_hostname=device_a_hostname,
            device_b_hostname=device_b_hostname,
            total_bandwidth_mbps=total_bandwidth,
            current_in_bps=total_in_bps,
            current_out_bps=total_out_bps,
            utilization_in_percent=utilization_percent(total_in_bps, total_bandwidth),
            utilization_out_percent=utilization_percent(total_out_bps, total_bandwidth),
            port_details=port_details
        )

    async def update_merged_links_in_db(self):
        """Update merged_links table from raw_links"""
        merged = await self.merge_links()

        # One query for the whole table instead of one lookup per merged link:
        # at 500 devices that loop was thousands of queries per cycle.
        existing_rows = (await self.db.execute(select(MergedLink))).scalars().all()
        by_pair = {(m.device_a_id, m.device_b_id): m for m in existing_rows}

        for link_info in merged:
            merged_link = by_pair.get(
                (link_info.device_a_id, link_info.device_b_id)
            )

            port_details_json = [
                {
                    "local_port": p.local_port,
                    "remote_port": p.remote_port,
                    "bandwidth_mbps": p.bandwidth_mbps,
                    "in_bps": p.in_bps,
                    "out_bps": p.out_bps,
                    "utilization_percent": p.utilization_percent,
                    "speed_source": p.speed_source
                }
                for p in link_info.port_details
            ]
            
            if merged_link:
                # Update existing
                merged_link.total_bandwidth_mbps = link_info.total_bandwidth_mbps
                merged_link.current_in_bps = link_info.current_in_bps
                merged_link.current_out_bps = link_info.current_out_bps
                merged_link.utilization_in_percent = link_info.utilization_in_percent
                merged_link.utilization_out_percent = link_info.utilization_out_percent
                merged_link.port_pairs = port_details_json
                merged_link.last_updated = datetime.utcnow()
            else:
                # Create new
                merged_link = MergedLink(
                    device_a_id=link_info.device_a_id,
                    device_b_id=link_info.device_b_id,
                    total_bandwidth_mbps=link_info.total_bandwidth_mbps,
                    current_in_bps=link_info.current_in_bps,
                    current_out_bps=link_info.current_out_bps,
                    utilization_in_percent=link_info.utilization_in_percent,
                    utilization_out_percent=link_info.utilization_out_percent,
                    port_pairs=port_details_json
                )
                self.db.add(merged_link)
        
        await self.db.commit()
        logger.info(f"Updated {len(merged)} merged links")
    
    async def get_topology_for_view(
        self,
        view: str = "overview",
        group_id: Optional[int] = None,
        expand_device_id: Optional[int] = None
    ) -> Dict:
        """
        Get topology data for frontend visualization
        
        Views:
        - overview: Core + Distribution devices only, Access aggregated
        - group: Devices in a group + their upstream connections
        - full: All devices
        """
        if view == "overview":
            return await self._get_overview_topology()
        elif view == "group" and group_id:
            return await self._get_group_topology(group_id)
        else:
            return await self._get_full_topology()
    
    async def _get_overview_topology(self) -> Dict:
        """Get overview topology with only Core and Distribution devices"""
        # Get core and distribution devices
        result = await self.db.execute(
            select(Device).where(
                Device.device_type.in_(["core", "distribution", "dist", "router"])
            )
        )
        devices = result.scalars().all()
        
        if not devices:
            # Fallback: get all devices if no type assigned
            result = await self.db.execute(select(Device).limit(50))
            devices = result.scalars().all()
        
        return await self._build_topology_response(devices)
    
    async def _get_group_topology(self, group_id: int) -> Dict:
        """Get topology for a specific group including upstream devices"""
        # Get devices in group
        members = await self.db.execute(
            select(DeviceGroupMember.device_id).where(
                DeviceGroupMember.group_id == group_id
            )
        )
        device_ids = [m[0] for m in members.all()]
        
        if not device_ids:
            return {"nodes": [], "links": [], "last_updated": datetime.utcnow().isoformat()}
        
        # Get the devices
        result = await self.db.execute(
            select(Device).where(Device.id.in_(device_ids))
        )
        group_devices = result.scalars().all()
        
        # Find upstream devices (devices connected to group devices but not in group)
        upstream_ids = set()
        for device in group_devices:
            # Find links where this device is involved
            links = await self.db.execute(
                select(MergedLink).where(
                    or_(
                        MergedLink.device_a_id == device.id,
                        MergedLink.device_b_id == device.id
                    )
                )
            )
            for link in links.scalars():
                other_id = link.device_b_id if link.device_a_id == device.id else link.device_a_id
                if other_id not in device_ids:
                    upstream_ids.add(other_id)
        
        # Get upstream devices
        if upstream_ids:
            upstream_result = await self.db.execute(
                select(Device).where(Device.id.in_(list(upstream_ids)))
            )
            upstream_devices = upstream_result.scalars().all()
            all_devices = list(group_devices) + list(upstream_devices)
        else:
            all_devices = list(group_devices)
        
        return await self._build_topology_response(all_devices)
    
    async def _get_full_topology(self) -> Dict:
        """Get full topology with all devices"""
        result = await self.db.execute(select(Device))
        devices = result.scalars().all()
        return await self._build_topology_response(devices)
    
    async def _build_topology_response(self, devices: List[Device]) -> Dict:
        """Build topology response for frontend"""
        device_ids = [d.id for d in devices]
        
        # Get alert counts per device
        from app.models.alert import Alert
        alert_counts = {}
        if device_ids:
            alert_result = await self.db.execute(
                select(Alert.device_id).where(
                    and_(
                        Alert.device_id.in_(device_ids),
                        Alert.is_active == True
                    )
                )
            )
            for row in alert_result:
                device_id = row[0]
                alert_counts[device_id] = alert_counts.get(device_id, 0) + 1
        
        # Build nodes
        nodes = []
        for device in devices:
            nodes.append({
                "id": str(device.id),
                "hostname": device.hostname,
                "ip_address": device.ip_address,
                "device_type": device.device_type or "switch",
                "vendor": device.vendor or "unknown",
                "status": device.status or "unknown",
                "cpu_percent": device.cpu_percent,
                "memory_percent": device.memory_percent,
                "alert_count": alert_counts.get(device.id, 0)
            })
        
        # Get links between these devices
        links = []
        if len(device_ids) >= 2:
            link_result = await self.db.execute(
                select(MergedLink).where(
                    and_(
                        MergedLink.device_a_id.in_(device_ids),
                        MergedLink.device_b_id.in_(device_ids),
                        MergedLink.is_excluded == False
                    )
                )
            )
            
            for link in link_result.scalars():
                max_util = max(
                    link.utilization_in_percent or 0,
                    link.utilization_out_percent or 0
                )
                
                links.append({
                    "id": str(link.id),
                    "source": str(link.device_a_id),
                    "target": str(link.device_b_id),
                    "total_bandwidth_mbps": link.total_bandwidth_mbps or 0,
                    "utilization_in_percent": link.utilization_in_percent or 0,
                    "utilization_out_percent": link.utilization_out_percent or 0,
                    "status": self._get_link_status(max_util),
                    "port_details": link.port_pairs or []
                })
        
        return {
            "nodes": nodes,
            "links": links,
            "last_updated": datetime.utcnow().isoformat()
        }
    
    # Interface name prefix -> speed in Mbps, most specific prefix first.
    # Matched against the leading letters of the port name only: a substring
    # match would read "GigabitEthernet" as a 10G port because of the "te"
    # in "gigabi-te-thernet".
    _SPEED_PREFIXES = [
        (("hundredgig", "hu"), 100000),
        (("fortygig", "fo"), 40000),
        (("twentyfivegig", "twe"), 25000),
        (("tengigabit", "tengig", "te", "xg", "xe"), 10000),
        (("gigabitethernet", "gigabit", "gi", "ge"), 1000),
        (("fastethernet", "fa"), 100),
        (("ethernet", "eth", "et"), 1000),
    ]

    # Explicit speed markers used by HP/Aruba/Fortinet style names
    _SPEED_TOKENS = [
        ("100g", 100000),
        ("40g", 40000),
        ("25g", 25000),
        ("10g", 10000),
        ("1g", 1000),
        ("100m", 100),
    ]

    def _estimate_bandwidth(self, port_name: str) -> int:
        """
        Guess a port's speed from its name.

        Fallback only. The real speed comes from ifHighSpeed and is stored on
        the link row; this is used for the ports the collector could not match
        to an interface, and for links recorded before traffic collection
        existed. The guess cannot tell a 1G port negotiated down to 100M from
        one running at 1G, which is why it is not the primary source.
        """
        name = (port_name or "").strip().lower()
        if not name:
            return 1000

        for token, speed in self._SPEED_TOKENS:
            if token in name:
                return speed

        prefix = re.match(r"^[a-z\-]*", name).group(0)
        for prefixes, speed in self._SPEED_PREFIXES:
            if any(prefix.startswith(p) for p in prefixes):
                return speed

        return 1000  # Default to 1Gbps
    
    def _get_link_status(self, utilization: float) -> str:
        """Determine link status based on utilization"""
        if utilization >= 90:
            return "critical"
        elif utilization >= 70:
            return "warning"
        elif utilization >= 50:
            return "elevated"
        return "normal"
