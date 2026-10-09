"""
Background collector service
Runs periodic SNMP polling of all managed devices
Integrates with Alert Engine and Topology Engine
"""
import asyncio
import logging
import socket
from datetime import datetime

from app.config import get_settings
from app.db.database import async_session_maker
from app.core.snmp_collector import SNMPCollector
from app.core.poll_scheduler import OfflineBackoff
from app.core.topology_engine import TopologyEngine
from app.core.alert_engine import AlertEngine
from app.core.log_exporter import get_log_exporter, LogLevel
from app.models.device import Device, DeviceStatus
from app.models.link import RawLink, MergedLink
from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Devices that do not answer SNMP are probed less and less often instead of
# costing a full timeout on every OID of every cycle. The state lives in this
# process only - see app/core/poll_scheduler.py for why.
offline_backoff = OfflineBackoff(
    base_interval=settings.collector_interval,
    max_interval=settings.collector_offline_backoff_max,
    enabled=settings.collector_offline_backoff
)


async def resolve_hostname(hostname: str) -> str:
    """Resolve a hostname to an IPv4 address without blocking the event loop"""
    if not hostname:
        return ""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, None, family=socket.AF_INET)
        return infos[0][4][0] if infos else ""
    except (socket.gaierror, UnicodeError, OSError, IndexError):
        return ""


async def auto_discover_neighbor(db, parent_device: Device, neighbor, collector: SNMPCollector, log_exporter):
    """Auto-discover and add a neighbor device to the database"""
    try:
        # Check if neighbor already exists by hostname
        existing = await db.execute(
            select(Device).where(Device.hostname == neighbor.remote_hostname)
        )
        if existing.scalar_one_or_none():
            return  # Already exists

        # Resolve the neighbor IP:
        # 1. the management address advertised through LLDP/CDP
        # 2. DNS lookup of the advertised system name
        neighbor_ip = neighbor.remote_mgmt_ip
        if not neighbor_ip:
            neighbor_ip = await resolve_hostname(neighbor.remote_hostname)

        if not neighbor_ip:
            logger.debug(f"Could not resolve IP for neighbor {neighbor.remote_hostname}")
            return

        # Check if IP already exists
        existing_ip = await db.execute(
            select(Device).where(Device.ip_address == neighbor_ip)
        )
        if existing_ip.scalar_one_or_none():
            return  # Already exists

        # Validate the device over SNMP before adding it
        device_info = await collector.get_device_info(neighbor_ip)
        if not device_info:
            logger.debug(f"SNMP probe failed for neighbor {neighbor_ip}")
            return

        hostname = device_info.hostname or neighbor.remote_hostname

        new_device = Device(
            hostname=hostname,
            ip_address=neighbor_ip,
            snmp_community=collector.community,
            device_type="access",
            vendor=device_info.vendor,
            status="managed",
            auto_discover=True,
            parent_device_id=parent_device.id,
            last_seen=datetime.utcnow()
        )
        db.add(new_device)

        logger.info(f"Auto-discovered neighbor: {hostname} ({neighbor_ip}) via {parent_device.hostname}")

        await log_exporter.log_discovery(
            event_type="new_device",
            device_hostname=hostname,
            device_ip=neighbor_ip,
            extra={"discovered_via": parent_device.hostname}
        )

    except Exception as e:
        logger.error(f"Error in auto_discover_neighbor: {e}")


async def poll_all_devices():
    """Poll all managed devices"""
    async with async_session_maker() as db:
        # Get all managed devices
        result = await db.execute(
            select(Device).where(Device.status != DeviceStatus.EXCLUDED)
        )
        devices = result.scalars().all()

        # Forget devices that no longer exist so the backoff state cannot grow
        offline_backoff.prune(device.id for device in devices)

        due = [d for d in devices if offline_backoff.should_poll(d.id)]
        due_ids = {d.id for d in due}
        skipped = len(devices) - len(due)

        if skipped:
            logger.info(
                f"Starting poll cycle for {len(due)} devices "
                f"({skipped} skipped, backing off after repeated failures)"
            )
            for device in devices:
                if device.id not in due_ids:
                    logger.debug(
                        f"Skipping {device.hostname} ({device.ip_address}): "
                        f"{offline_backoff.failure_count(device.id)} consecutive "
                        f"failures, retry in "
                        f"{offline_backoff.seconds_until_retry(device.id):.0f}s"
                    )
        else:
            logger.info(f"Starting poll cycle for {len(due)} devices")

        # Poll devices concurrently with limit
        semaphore = asyncio.Semaphore(settings.collector_concurrent)

        async def poll_with_semaphore(device):
            async with semaphore:
                # Use device-specific community, fallback to default
                community = device.snmp_community or settings.snmp_default_community
                # One collector - and therefore one SNMP engine and one UDP
                # socket - per device, released as soon as the device is done
                async with SNMPCollector(
                    community=community,
                    timeout=settings.snmp_timeout,
                    retries=settings.snmp_retries,
                    snmp_version=device.snmp_version or "v2c",
                    v3_username=device.snmpv3_username,
                    v3_auth_protocol=device.snmpv3_auth_protocol,
                    v3_auth_password=device.snmpv3_auth_password,
                    v3_priv_protocol=device.snmpv3_priv_protocol,
                    v3_priv_password=device.snmpv3_priv_password
                ) as collector:
                    reachable = await poll_single_device(collector, device, db)

                if reachable:
                    offline_backoff.record_success(device.id)
                else:
                    failures = offline_backoff.record_failure(device.id)
                    logger.debug(
                        f"{device.hostname}: {failures} consecutive failures, "
                        f"next attempt in "
                        f"{offline_backoff.delay_for(failures):.0f}s"
                    )
                return reachable

        tasks = [poll_with_semaphore(d) for d in due]
        # One device blowing up must not abandon the rest of the cycle
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for device, outcome in zip(due, outcomes):
            if isinstance(outcome, BaseException):
                logger.error(f"Polling {device.hostname} raised: {outcome}")
        
        await db.commit()
        
        # Update merged links
        logger.info("Updating merged links...")
        topology_engine = TopologyEngine(db)
        await topology_engine.update_merged_links_in_db()
        
        # Run alert checks
        logger.info("Running alert checks...")
        alert_engine = AlertEngine(db)
        await alert_engine.run_check_cycle()
        
        logger.info("Poll cycle completed")


async def poll_single_device(collector: SNMPCollector, device: Device, db) -> bool:
    """
    Poll a single device and update database.

    Returns True when the device answered SNMP, so the caller can keep the
    offline backoff up to date.
    """
    log_exporter = get_log_exporter()
    
    try:
        result = await collector.poll_device(device.ip_address)
        
        if result["success"]:
            # Update device info
            previous_status = device.status
            device.status = DeviceStatus.MANAGED
            device.last_seen = datetime.utcnow()
            
            if result["metrics"]:
                device.cpu_percent = result["metrics"].cpu_percent
                device.memory_percent = result["metrics"].memory_percent
            
            if result["device_info"]:
                device.vendor = result["device_info"].vendor
                device.uptime_seconds = result["device_info"].uptime_seconds
            
            # Log recovery if device was offline
            if previous_status == DeviceStatus.OFFLINE:
                await log_exporter.log(
                    level=LogLevel.INFO,
                    source="collector",
                    message=f"Device {device.hostname} is back online",
                    device_hostname=device.hostname,
                    device_ip=device.ip_address
                )
            
            # Update raw links
            for neighbor in result["lldp_neighbors"] + result["cdp_neighbors"]:
                # Check if link already exists
                existing = await db.execute(
                    select(RawLink).where(
                        RawLink.local_device_id == device.id,
                        RawLink.local_port == neighbor.local_port,
                        RawLink.remote_hostname == neighbor.remote_hostname
                    )
                )
                raw_link = existing.scalar_one_or_none()
                
                if raw_link:
                    # Refresh the details, they can change (and older versions
                    # of the collector stored unusable values here)
                    raw_link.local_port_index = neighbor.local_port_index
                    raw_link.remote_port = neighbor.remote_port
                    raw_link.remote_chassis_id = neighbor.remote_chassis_id
                    raw_link.protocol = neighbor.protocol
                    raw_link.last_seen = datetime.utcnow()
                else:
                    raw_link = RawLink(
                        local_device_id=device.id,
                        local_port=neighbor.local_port,
                        local_port_index=neighbor.local_port_index,
                        remote_hostname=neighbor.remote_hostname,
                        remote_port=neighbor.remote_port,
                        remote_chassis_id=neighbor.remote_chassis_id,
                        protocol=neighbor.protocol
                    )
                    db.add(raw_link)
                    
                    # Log new link discovery
                    await log_exporter.log_discovery(
                        event_type="new_link",
                        device_hostname=device.hostname,
                        device_ip=device.ip_address,
                        extra={
                            "local_port": neighbor.local_port,
                            "remote_hostname": neighbor.remote_hostname,
                            "remote_port": neighbor.remote_port
                        }
                    )
                
                # Auto-discover neighbor devices if enabled
                if device.auto_discover:
                    await auto_discover_neighbor(
                        db, device, neighbor, collector, log_exporter
                    )
            
            logger.info(
                f"Polled {device.hostname} ({device.ip_address}): "
                f"{len(result['lldp_neighbors'])} LLDP + "
                f"{len(result['cdp_neighbors'])} CDP neighbors, "
                f"{len(result['interface_stats'])} interfaces"
            )
            return True
        else:
            # Mark device as potentially offline
            if device.status != DeviceStatus.OFFLINE:
                await log_exporter.log(
                    level=LogLevel.WARNING,
                    source="collector",
                    message=f"Device {device.hostname} is not responding",
                    device_hostname=device.hostname,
                    device_ip=device.ip_address
                )
            device.status = DeviceStatus.OFFLINE
            logger.warning(f"Polled {device.hostname}: FAILED")
            return False

    except Exception as e:
        logger.error(f"Error polling {device.hostname}: {e}")
        device.status = DeviceStatus.OFFLINE
        return False


async def main():
    """Main collector loop"""
    logger.info(f"SNMP Collector starting (interval: {settings.collector_interval}s)")
    
    # Initial wait for database to be ready
    await asyncio.sleep(5)
    
    while True:
        try:
            await poll_all_devices()
        except Exception as e:
            logger.error(f"Poll cycle error: {e}")
        
        await asyncio.sleep(settings.collector_interval)


if __name__ == "__main__":
    asyncio.run(main())

