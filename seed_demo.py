"""
Seed script to create 50 demo devices with proper hierarchy
"""
import asyncio
import sys
import random
sys.path.insert(0, '.')

from app.db.database import async_session_maker, engine
from app.models.device import Device, DeviceStatus
from app.models.link import MergedLink
from app.models.alert import Alert
from app.models.group import DeviceGroup, DeviceGroupMember
from datetime import datetime, timedelta
from app.db.database import Base


def random_ip(base):
    return f"192.168.{base}.{random.randint(1, 254)}"

def random_cpu():
    return round(random.uniform(5, 95), 1)

def random_memory():
    return round(random.uniform(20, 90), 1)

def random_utilization():
    return round(random.uniform(5, 95), 1)


# Device models by vendor
DEVICE_MODELS = {
    'cisco_ios': ['WS-C3850-48P', 'WS-C2960X-48FPS', 'C9300-48P', 'ISR4331', 'WS-C3650-24PS'],
    'cisco_nxos': ['N9K-C93180YC-EX', 'N5K-C5672UP', 'N9K-C9336C-FX2'],
    'hp_aruba': ['2930F-48G', '6300M-48G', '2540-24G', 'JL354A', 'JL322A'],
    'fortinet': ['FG-100F', 'FG-200F', 'FG-60F', 'FG-400F'],
    'paloalto': ['PA-3260', 'PA-5250', 'PA-820', 'PA-220'],
    'ruckus': ['R750', 'R650', 'R550', 'T350', 'H550'],
    'juniper': ['EX4300-48P', 'EX2300-24P', 'MX204', 'SRX340'],
    'extreme': ['X465-48P', 'X440-G2-24p', 'X690-48x-2q'],
    'f5': ['BIG-IP i5800', 'BIG-IP i4800'],
    'dell': ['PowerEdge R760', 'PowerEdge R660'],
    'vmware': ['Virtual Machine'],
    'hikvision': ['DS-2CD2387G2', 'DS-2DE4425IW'],
}

FIRMWARE_VERSIONS = {
    'cisco_ios': ['15.2(7)E7', '16.12.8', '17.3.5a', '16.9.8'],
    'cisco_nxos': ['10.2(5)', '9.3(9)', '10.1(2)'],
    'hp_aruba': ['WC.16.11.0012', 'YC.16.10.0012', 'KB.16.09.0010'],
    'fortinet': ['7.0.12', '6.4.14', '7.2.5'],
    'paloalto': ['10.2.4-h4', '11.0.1', '10.1.9-h3'],
    'ruckus': ['6.1.2.0.3001', '5.2.2.0.2056'],
    'juniper': ['21.4R3.14', '22.2R2.17', '20.4R3-S6'],
    'extreme': ['32.5.1', '31.7.2', '32.1.1'],
    'f5': ['17.1.1', '16.1.4'],
    'dell': ['iDRAC 7.10'],
    'vmware': ['Ubuntu 24.04'],
    'hikvision': ['V5.7.15'],
}


# The fixed part of the demo network: 2 core, 6 distribution, 3 firewalls,
# 3 routers, 2 load balancers, 4 servers, 4 VMs, 8 cameras
FIXED_DEVICES = 32


def access_and_ap_counts(total):
    """
    Split what --devices leaves after the fixed part between access switches
    and access points, in the default demo's proportion (42 : 50).
    """
    rest = max(2, total - FIXED_DEVICES)
    access = max(1, round(rest * 42 / 92))
    return access, rest - access


async def seed_demo_data(total=124):
    """Create `total` demo devices with proper hierarchy (124 by default)"""
    n_access, n_ap = access_and_ap_counts(total)
    print("Dropping and recreating all tables...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    
    async with async_session_maker() as db:
        print("Creating devices with hierarchy...")
        
        # =============================================
        # LAYER 1: Core Switches (2)
        # =============================================
        core_switches = []
        for i in range(1, 3):
            device = Device(
                hostname=f"core-sw-{str(i).zfill(2)}",
                ip_address=f"192.168.1.{i}",
                vendor="cisco_nxos",
                model=random.choice(DEVICE_MODELS['cisco_nxos']),
                firmware_version=random.choice(FIRMWARE_VERSIONS['cisco_nxos']),
                device_type="core",
                status=DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=random_cpu(),
                memory_percent=random_memory(),
                last_seen=datetime.utcnow() - timedelta(minutes=random.randint(1, 5)),
                parent_device_id=None  # Core has no parent
            )
            core_switches.append(device)
            db.add(device)
        
        await db.commit()
        for d in core_switches:
            await db.refresh(d)
        print(f"  Created {len(core_switches)} core switches")
        
        # =============================================
        # LAYER 2: Distribution Switches (6)
        # Each dist connects to a core
        # =============================================
        dist_switches = []
        for i in range(1, 7):
            parent_core = core_switches[(i - 1) % 2]
            vendor = random.choice(['cisco_ios', 'hp_aruba', 'juniper'])
            device = Device(
                hostname=f"dist-sw-{str(i).zfill(2)}",
                ip_address=f"192.168.2.{i}",
                vendor=vendor,
                model=random.choice(DEVICE_MODELS[vendor]),
                firmware_version=random.choice(FIRMWARE_VERSIONS[vendor]),
                device_type="distribution",
                status=DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=random_cpu(),
                memory_percent=random_memory(),
                last_seen=datetime.utcnow() - timedelta(minutes=random.randint(1, 5)),
                parent_device_id=parent_core.id
            )
            dist_switches.append(device)
            db.add(device)
        
        await db.commit()
        for d in dist_switches:
            await db.refresh(d)
        print(f"  Created {len(dist_switches)} distribution switches")
        
        # =============================================
        # LAYER 3: Access Switches - a third per floor (42 by default)
        # Floor 1 -> dist-sw-01, dist-sw-02
        # Floor 2 -> dist-sw-03, dist-sw-04
        # Floor 3 -> dist-sw-05, dist-sw-06
        # =============================================
        access_switches = []
        floor_assignments = {}  # Track which floor each access switch belongs to
        per_floor = -(-n_access // 3)  # ceiling
        
        for i in range(1, n_access + 1):
            floor = min(3, (i - 1) // per_floor + 1)
            parent_dist = dist_switches[(floor - 1) * 2 + ((i - 1) % 2)]
            
            vendor = random.choice(['cisco_ios', 'hp_aruba', 'extreme'])
            is_offline = random.random() < 0.05
            
            device = Device(
                hostname=f"access-sw-{str(i).zfill(2)}",
                ip_address=f"192.168.{10 + floor}.{i}",
                vendor=vendor,
                model=random.choice(DEVICE_MODELS[vendor]),
                firmware_version=random.choice(FIRMWARE_VERSIONS[vendor]),
                device_type="access",
                status=DeviceStatus.OFFLINE if is_offline else DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=None if is_offline else random_cpu(),
                memory_percent=None if is_offline else random_memory(),
                last_seen=None if is_offline else datetime.utcnow() - timedelta(minutes=random.randint(1, 10)),
                parent_device_id=parent_dist.id
            )
            access_switches.append(device)
            floor_assignments[device.hostname] = floor
            db.add(device)
        
        await db.commit()
        for d in access_switches:
            await db.refresh(d)
        print(f"  Created {len(access_switches)} access switches")
        
        # =============================================
        # LAYER 4: Access Points (50 by default) - connected to access switches
        # =============================================
        aps = []
        for i in range(1, n_ap + 1):
            parent_access = access_switches[(i - 1) % len(access_switches)]
            
            device = Device(
                hostname=f"ap-{str(i).zfill(2)}",
                ip_address=f"192.168.50.{i}",
                vendor="ruckus",
                model=random.choice(DEVICE_MODELS['ruckus']),
                firmware_version=random.choice(FIRMWARE_VERSIONS['ruckus']),
                device_type="ap",
                status=DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=random_cpu(),
                memory_percent=random_memory(),
                last_seen=datetime.utcnow() - timedelta(minutes=random.randint(1, 10)),
                parent_device_id=parent_access.id
            )
            aps.append(device)
            db.add(device)
        
        await db.commit()
        for d in aps:
            await db.refresh(d)
        print(f"  Created {len(aps)} access points")
        
        # =============================================
        # Firewalls (3) - connected to core
        # =============================================
        firewalls = []
        for i in range(1, 4):
            parent_core = core_switches[(i - 1) % 2]
            vendor = random.choice(['fortinet', 'paloalto'])
            is_offline = random.random() < 0.1
            
            device = Device(
                hostname=f"fw-{str(i).zfill(2)}",
                ip_address=f"192.168.100.{i}",
                vendor=vendor,
                model=random.choice(DEVICE_MODELS[vendor]),
                firmware_version=random.choice(FIRMWARE_VERSIONS[vendor]),
                device_type="firewall",
                status=DeviceStatus.OFFLINE if is_offline else DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=None if is_offline else random_cpu(),
                memory_percent=None if is_offline else random_memory(),
                last_seen=None if is_offline else datetime.utcnow() - timedelta(minutes=random.randint(1, 10)),
                parent_device_id=parent_core.id
            )
            firewalls.append(device)
            db.add(device)
        
        await db.commit()
        for d in firewalls:
            await db.refresh(d)
        print(f"  Created {len(firewalls)} firewalls")
        
        # =============================================
        # Routers (3) - connected to core
        # =============================================
        routers = []
        for i in range(1, 4):
            parent_core = core_switches[(i - 1) % 2]
            vendor = random.choice(['cisco_ios', 'juniper'])
            
            device = Device(
                hostname=f"rtr-{str(i).zfill(2)}",
                ip_address=f"192.168.200.{i}",
                vendor=vendor,
                model=random.choice(DEVICE_MODELS[vendor]),
                firmware_version=random.choice(FIRMWARE_VERSIONS[vendor]),
                device_type="router",
                status=DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=random_cpu(),
                memory_percent=random_memory(),
                last_seen=datetime.utcnow() - timedelta(minutes=random.randint(1, 5)),
                parent_device_id=parent_core.id
            )
            routers.append(device)
            db.add(device)
        
        await db.commit()
        for d in routers:
            await db.refresh(d)
        print(f"  Created {len(routers)} routers")
        
        # =============================================
        # Data center: load balancers on the core, servers behind them,
        # VMs on the first hypervisors (seen through the vSwitch's LLDP);
        # CCTV cameras on the floor access switches
        # =============================================
        def make(hostname, ip, vendor, device_type, parent, offline=False):
            device = Device(
                hostname=hostname, ip_address=ip, vendor=vendor,
                model=random.choice(DEVICE_MODELS[vendor]),
                firmware_version=random.choice(FIRMWARE_VERSIONS[vendor]),
                device_type=device_type,
                status=DeviceStatus.OFFLINE if offline else DeviceStatus.MANAGED,
                snmp_community="public",
                cpu_percent=None if offline else random_cpu(),
                memory_percent=None if offline else random_memory(),
                last_seen=None if offline else datetime.utcnow() - timedelta(minutes=random.randint(1, 10)),
                parent_device_id=parent.id,
            )
            db.add(device)
            return device

        lbs = [make(f"lb-{i:02d}", f"192.168.150.{i}", "f5", "loadbalancer", core_switches[i - 1]) for i in (1, 2)]
        await db.commit()
        for d in lbs:
            await db.refresh(d)
        servers = [make(f"srv-pve-{i:02d}", f"192.168.151.{i}", "dell", "server", lbs[(i - 1) % 2]) for i in range(1, 5)]
        await db.commit()
        for d in servers:
            await db.refresh(d)
        vms = [make(f"vm-app-{i:02d}", f"192.168.152.{i}", "vmware", "vm", servers[(i - 1) % 2]) for i in range(1, 5)]
        cameras = [make(f"cam-{i:02d}", f"192.168.60.{i}", "hikvision", "camera",
                        access_switches[(i * 5) % len(access_switches)], offline=(i == 3)) for i in range(1, 9)]
        await db.commit()
        for d in vms + cameras:
            await db.refresh(d)
        print(f"  Created {len(lbs)} load balancers, {len(servers)} servers, {len(vms)} VMs, {len(cameras)} cameras")

        all_devices = (core_switches + dist_switches + access_switches + aps + firewalls + routers
                       + lbs + servers + vms + cameras)
        print(f"\nTotal devices: {len(all_devices)}")
        
        # =============================================
        # Create MergedLinks based on parent-child relationships
        # =============================================
        print("\nCreating network links based on hierarchy...")
        links = []
        
        for device in all_devices:
            if device.parent_device_id:
                # Create link from parent to child
                bw = 10000 if device.device_type in ['distribution', 'firewall', 'router', 'loadbalancer', 'server', 'vm'] else 1000
                link = MergedLink(
                    device_a_id=device.parent_device_id,
                    device_b_id=device.id,
                    port_pairs=[{"a": f"Gi0/{random.randint(1,48)}", "b": "Gi0/1"}],
                    total_bandwidth_mbps=bw,
                    utilization_in_percent=random_utilization(),
                    utilization_out_percent=random_utilization()
                )
                links.append(link)
                db.add(link)
        
        # Core-to-core link
        if len(core_switches) >= 2:
            link = MergedLink(
                device_a_id=core_switches[0].id,
                device_b_id=core_switches[1].id,
                port_pairs=[{"a": "Te1/1", "b": "Te1/1"}, {"a": "Te1/2", "b": "Te1/2"}],
                total_bandwidth_mbps=80000,
                utilization_in_percent=random_utilization(),
                utilization_out_percent=random_utilization()
            )
            links.append(link)
            db.add(link)
        
        await db.commit()
        print(f"  Created {len(links)} links")
        
        # =============================================
        # Create Groups
        # =============================================
        print("\nCreating groups...")
        groups = [
            DeviceGroup(name="Core Network", description="Core and distribution layer"),
            DeviceGroup(name="Floor 1", description="Floor 1 access switches"),
            DeviceGroup(name="Floor 2", description="Floor 2 access switches"),
            DeviceGroup(name="Floor 3", description="Floor 3 access switches"),
            DeviceGroup(name="Wireless", description="All access points"),
            DeviceGroup(name="Security", description="Firewalls"),
            DeviceGroup(name="WAN Edge", description="WAN routers"),
            DeviceGroup(name="Data Center", description="Load balancers, servers and VMs"),
            DeviceGroup(name="CCTV", description="Security cameras"),
        ]
        
        for group in groups:
            db.add(group)
        
        await db.commit()
        for group in groups:
            await db.refresh(group)
        
        # Assign devices to groups
        memberships = []
        
        # Core Network: core + distribution
        for d in core_switches + dist_switches:
            memberships.append(DeviceGroupMember(group_id=groups[0].id, device_id=d.id))
        
        # Floor 1, 2, 3 based on floor_assignments
        for access in access_switches:
            floor = floor_assignments.get(access.hostname, 1)
            memberships.append(DeviceGroupMember(group_id=groups[floor].id, device_id=access.id))
        
        # Wireless
        for ap in aps:
            memberships.append(DeviceGroupMember(group_id=groups[4].id, device_id=ap.id))
        
        # Security
        for fw in firewalls:
            memberships.append(DeviceGroupMember(group_id=groups[5].id, device_id=fw.id))
        
        # WAN Edge
        for rtr in routers:
            memberships.append(DeviceGroupMember(group_id=groups[6].id, device_id=rtr.id))
        
        for d in lbs + servers + vms:
            memberships.append(DeviceGroupMember(group_id=groups[7].id, device_id=d.id))
        for d in cameras:
            memberships.append(DeviceGroupMember(group_id=groups[8].id, device_id=d.id))

        for m in memberships:
            db.add(m)
        
        await db.commit()
        print(f"  Created {len(groups)} groups with {len(memberships)} memberships")
        
        # =============================================
        # Create Alerts
        # =============================================
        print("\nCreating demo alerts...")
        alerts = []
        
        # Offline device alerts
        for device in all_devices:
            if device.status == DeviceStatus.OFFLINE:
                alerts.append(Alert(
                    device_id=device.id,
                    alert_type="device_offline",
                    severity="critical",
                    message=f"Device {device.hostname} is offline",
                    is_active=True,
                    triggered_at=datetime.utcnow() - timedelta(hours=random.randint(1, 24))
                ))
        
        # High CPU alerts
        for device in all_devices:
            if device.cpu_percent and device.cpu_percent > 70:
                severity = "critical" if device.cpu_percent > 90 else "warning"
                alerts.append(Alert(
                    device_id=device.id,
                    alert_type="cpu_high",
                    severity=severity,
                    message=f"CPU utilization is {device.cpu_percent:.1f}%",
                    current_value=device.cpu_percent,
                    threshold_value=70 if severity == "warning" else 90,
                    is_active=True,
                    triggered_at=datetime.utcnow() - timedelta(minutes=random.randint(10, 60))
                ))
        
        for alert in alerts:
            db.add(alert)
        
        await db.commit()
        print(f"  Created {len(alerts)} alerts")
        
        # =============================================
        # Summary
        # =============================================
        print("\n" + "=" * 60)
        print("✅ Demo data with HIERARCHY created successfully!")
        print("=" * 60)
        print(f"\n📊 Device Hierarchy:")
        print(f"   Core ({len(core_switches)}) → Distribution ({len(dist_switches)}) → Access ({len(access_switches)}) → AP ({len(aps)})")
        print(f"   Core → Firewall ({len(firewalls)})")
        print(f"   Core → Router ({len(routers)})")
        print(f"\n📁 Floor Assignments:")
        print(f"   Floor 1: access-sw-01 to access-sw-07 (7 devices)")
        print(f"   Floor 2: access-sw-08 to access-sw-14 (7 devices)")
        print(f"   Floor 3: access-sw-15 to access-sw-21 (7 devices)")
        print(f"\n🔗 Links: {len(links)} (based on parent-child relationships)")
        print(f"⚠️  Alerts: {len(alerts)}")



async def enable_cascade_offline_scenario():
    """
    🔴 連鎖離線情境演示
    模擬 dist-sw-03 離線，導致其下游設備全部離線
    
    拓撲結構:
    core-sw-01
        └── dist-sw-03 (ROOT CAUSE - OFFLINE)
            ├── access-sw-08 (OFFLINE)
            │   └── ap-08 (OFFLINE)
            ├── access-sw-10 (OFFLINE)
            │   └── ap-10 (OFFLINE)
            ├── access-sw-12 (OFFLINE)
            │   └── ap-12 (OFFLINE)
            └── access-sw-14 (OFFLINE)
                └── ap-14 (OFFLINE)
    """
    print("\n" + "=" * 60)
    print("🔴 啟用連鎖離線演示情境")
    print("=" * 60)
    
    async with async_session_maker() as db:
        from sqlalchemy import select, update
        from sqlalchemy.orm import selectinload
        
        # 1. 找到 dist-sw-03 (根因設備)
        result = await db.execute(
            select(Device).where(Device.hostname == "dist-sw-03")
        )
        root_device = result.scalar_one_or_none()
        
        if not root_device:
            print("❌ 找不到 dist-sw-03，請先執行 seed_demo.py")
            return
        
        print(f"\n📍 根因設備: {root_device.hostname} (ID: {root_device.id})")
        
        # 2. 將 dist-sw-03 設為離線
        root_device.status = DeviceStatus.OFFLINE
        root_device.cpu_percent = None
        root_device.memory_percent = None
        root_device.last_seen = datetime.utcnow() - timedelta(minutes=30)
        
        # 3. 遞迴找出所有下游設備並設為離線
        affected_devices = []
        
        async def mark_children_offline(parent_id: int, depth: int = 0):
            result = await db.execute(
                select(Device).where(Device.parent_device_id == parent_id)
            )
            children = result.scalars().all()
            
            for child in children:
                prefix = "  " * depth
                print(f"{prefix}├── {child.hostname} → OFFLINE")
                child.status = DeviceStatus.OFFLINE
                child.cpu_percent = None
                child.memory_percent = None
                child.last_seen = datetime.utcnow() - timedelta(minutes=30)
                affected_devices.append(child)
                await mark_children_offline(child.id, depth + 1)
        
        print(f"\n連鎖影響:")
        await mark_children_offline(root_device.id)
        
        # 4. 為根因設備建立告警
        root_alert = Alert(
            device_id=root_device.id,
            alert_type="device_offline",
            severity="critical",
            message=f"[ROOT CAUSE] Device {root_device.hostname} is offline - affecting {len(affected_devices)} downstream devices",
            is_active=True,
            triggered_at=datetime.utcnow() - timedelta(minutes=30),
            details={
                "is_root_cause": True,
                "impact_count": len(affected_devices),
                "affected_hostnames": [d.hostname for d in affected_devices]
            }
        )
        db.add(root_alert)
        await db.flush()
        await db.refresh(root_alert)
        
        # 5. 為下游設備建立被抑制的告警
        for device in affected_devices:
            alert = Alert(
                device_id=device.id,
                alert_type="device_offline",
                severity="critical",
                message=f"Device {device.hostname} is offline (caused by {root_device.hostname})",
                is_active=True,
                triggered_at=datetime.utcnow() - timedelta(minutes=29),
                details={
                    "is_root_cause": False,
                    "root_cause_device": root_device.hostname,
                    "is_suppressed": True
                }
            )
            db.add(alert)
        
        await db.commit()
        
        # 6. 統計輸出
        print(f"\n📊 影響統計:")
        print(f"   根因設備: {root_device.hostname}")
        print(f"   受影響設備: {len(affected_devices)} 台")
        
        device_types = {}
        for d in affected_devices:
            device_types[d.device_type] = device_types.get(d.device_type, 0) + 1
        for t, c in device_types.items():
            print(f"   - {t}: {c} 台")
        
        print(f"\n✅ 連鎖離線情境已啟用!")
        print(f"   Root Alert ID: {root_alert.id}")
        print(f"   總告警數: {1 + len(affected_devices)}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Seed demo data")
    parser.add_argument("--cascade", action="store_true", help="Enable cascade offline scenario")
    parser.add_argument("--seed", type=int, default=None, help="Random seed, for the same data every run")
    parser.add_argument("--devices", type=int, default=124,
                        help=f"How many devices in total (at least {FIXED_DEVICES + 2}; default 124)")
    args = parser.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    
    asyncio.run(seed_demo_data(args.devices))
    
    if args.cascade:
        asyncio.run(enable_cascade_offline_scenario())
