"""
Discovery Scheduler - sweeps the configured subnets for devices that answer SNMP
"""
import asyncio
import ipaddress
import logging
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.clock import utcnow
from app.core.device_type import guess_device_type
from app.core.snmp_collector import SNMPCollector
from app.db.database import async_session_maker
from app.models.device import Device

logger = logging.getLogger(__name__)
settings = get_settings()

# A sweep is not latency critical and most addresses in a subnet are empty,
# so a short timeout and no retry: 256 silent hosts cost about half a minute
# instead of the better part of an hour.
PROBE_TIMEOUT = 2
PROBE_RETRIES = 0
MAX_HOSTS_PER_SUBNET = 256
BATCH_SIZE = 20


class DiscoveryScheduler:
    """Handles automatic subnet scanning for device discovery"""

    def __init__(self, allowed_subnets: List[str], community: str, interval: int = 3600):
        self.allowed_subnets = allowed_subnets
        self.community = community
        self.interval = interval  # seconds between scans
        self.running = False
        # What the last sweep found, for GET /settings/discovery/status
        self.last_run: Optional[dict] = None
        self._wake = asyncio.Event()

    async def start(self):
        """Run sweeps until stopped"""
        self.running = True
        logger.info(f"Discovery Scheduler starting (interval: {self.interval}s)")

        while self.running:
            try:
                await self.run_discovery_cycle()
            except Exception as e:
                logger.error(f"Discovery cycle error: {e}")

            # Sleep the interval, or until trigger() / stop() cuts it short
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self):
        """Stop the discovery scheduler"""
        self.running = False
        self._wake.set()
        logger.info("Discovery Scheduler stopped")

    def trigger(self):
        """Run the next sweep now instead of at the end of the interval"""
        self._wake.set()

    async def run_discovery_cycle(self):
        """Run a single discovery cycle"""
        if not self.allowed_subnets:
            logger.debug("No subnets configured for discovery")
            return

        logger.info(f"Starting discovery cycle for {len(self.allowed_subnets)} subnets")

        total_discovered = 0
        total_added = 0

        # One collector - one SNMP engine and one socket - for the whole
        # sweep. Building an engine per probed address is what this used to
        # do, and none of them was ever closed.
        async with SNMPCollector(
            community=self.community,
            timeout=PROBE_TIMEOUT,
            retries=PROBE_RETRIES
        ) as collector:
            for subnet_str in self.allowed_subnets:
                if not self.running:
                    break
                try:
                    discovered, added = await self.scan_subnet(subnet_str, collector)
                    total_discovered += discovered
                    total_added += added
                except Exception as e:
                    logger.error(f"Error scanning subnet {subnet_str}: {e}")

        self.last_run = {
            "finished_at": utcnow().isoformat(),
            "answered": total_discovered,
            "added": total_added,
        }
        logger.info(f"Discovery cycle completed: discovered={total_discovered}, added={total_added}")

    async def scan_subnet(self, subnet_str: str, collector: SNMPCollector) -> tuple:
        """Scan a subnet for devices"""
        try:
            network = ipaddress.ip_network(subnet_str, strict=False)
        except ValueError as e:
            logger.error(f"Invalid subnet: {subnet_str} - {e}")
            return (0, 0)

        # Limit scan size to prevent overload. Taken lazily: hosts() of a /8
        # is sixteen million addresses, and list() of it is the outage.
        hosts = []
        for host in network.hosts():
            hosts.append(host)
            if len(hosts) >= MAX_HOSTS_PER_SUBNET:
                logger.warning(
                    f"{subnet_str} has more than {MAX_HOSTS_PER_SUBNET} hosts; "
                    f"only the first {MAX_HOSTS_PER_SUBNET} are swept. List "
                    f"smaller networks to cover the rest."
                )
                break

        logger.info(f"Scanning subnet {subnet_str} ({len(hosts)} hosts)")

        discovered = 0
        added = 0

        # Scan in batches to avoid overwhelming the network
        for i in range(0, len(hosts), BATCH_SIZE):
            batch = hosts[i:i + BATCH_SIZE]
            tasks = [self.probe_device(str(ip), collector) for ip in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, dict) and result.get("success"):
                    discovered += 1
                    if result.get("added"):
                        added += 1

        return (discovered, added)

    async def probe_device(self, ip: str, collector: SNMPCollector) -> dict:
        """Probe a single IP for SNMP response, and add it when it is new"""
        try:
            device_info = await collector.get_device_info(ip)
            if not device_info:
                return {"success": False, "ip": ip}

            hostname = device_info.hostname or f"device-{ip.replace('.', '-')}"

            async with async_session_maker() as db:
                # Known under this address, or under this name at another
                # address (a switch with several addresses in the subnet
                # answers on all of them and is still one device)
                result = await db.execute(
                    select(Device.id).where(
                        (Device.ip_address == ip) | (Device.hostname == hostname)
                    )
                )
                if result.first() is not None:
                    return {"success": True, "ip": ip, "added": False, "hostname": hostname}

                db.add(Device(
                    hostname=hostname,
                    ip_address=ip,
                    snmp_community=self.community,
                    device_type=guess_device_type(hostname, device_info.vendor),
                    vendor=device_info.vendor,
                    status="managed",
                    auto_discover=True,
                    last_seen=utcnow()
                ))
                try:
                    await db.commit()
                except IntegrityError:
                    # The collector found it through LLDP at the same moment
                    await db.rollback()
                    return {"success": True, "ip": ip, "added": False, "hostname": hostname}

                logger.info(f"Discovered new device: {hostname} ({ip})")
                return {"success": True, "ip": ip, "added": True, "hostname": hostname}

        except Exception as e:
            logger.warning(f"Probe failed for {ip}: {e}")
            return {"success": False, "ip": ip, "error": str(e)}


# Global scheduler instance, and the task running it. The task is kept
# because the event loop only holds a weak reference to a task: one that
# nothing else refers to can be garbage collected in the middle of a sweep.
_scheduler: Optional[DiscoveryScheduler] = None
_task: Optional[asyncio.Task] = None


def get_discovery_scheduler() -> Optional[DiscoveryScheduler]:
    """Get the global discovery scheduler instance"""
    return _scheduler


async def start_discovery_scheduler(subnets: List[str], community: str, interval: int = 3600):
    """Start the global discovery scheduler, replacing one that is running"""
    global _scheduler, _task

    stop_discovery_scheduler()
    _scheduler = DiscoveryScheduler(subnets, community, interval)
    _task = asyncio.create_task(_scheduler.start(), name="discovery-scheduler")


def stop_discovery_scheduler():
    """Stop the global discovery scheduler"""
    global _scheduler, _task

    if _scheduler:
        _scheduler.stop()
        _scheduler = None
    _task = None
