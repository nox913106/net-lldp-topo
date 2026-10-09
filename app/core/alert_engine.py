"""
Alert Engine - Handles threshold checking, alert state machine, and recovery logic
"""
import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.core.damping import TRAP_KEY
from app.models.device import Device
from app.models.link import MergedLink, RawLink
from app.models.alert import Alert, AlertHistory, AlertProfile

logger = logging.getLogger(__name__)


class AlertType(str, Enum):
    """Types of alerts"""
    DEVICE_OFFLINE = "device_offline"
    DEVICE_ONLINE = "device_online"  # Recovery
    LINK_HIGH_UTILIZATION = "link_high_utilization"
    LINK_UTILIZATION_NORMAL = "link_utilization_normal"  # Recovery
    LINK_DOWN = "link_down"
    LINK_UP = "link_up"  # Recovery
    CPU_HIGH = "cpu_high"
    CPU_NORMAL = "cpu_normal"  # Recovery
    MEMORY_HIGH = "memory_high"
    MEMORY_NORMAL = "memory_normal"  # Recovery
    NEW_DEVICE_DISCOVERED = "new_device_discovered"
    NEW_LINK_DISCOVERED = "new_link_discovered"


class AlertSeverity(str, Enum):
    """Alert severity levels"""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class ThresholdConfig:
    """Threshold configuration for a metric"""
    warning: float
    critical: float
    recovery_buffer: float = 10.0  # Hysteresis for recovery


@dataclass
class AlertEvent:
    """Represents an alert event to be processed"""
    alert_type: AlertType
    severity: AlertSeverity
    device_id: Optional[int] = None
    link_id: Optional[int] = None
    raw_link_id: Optional[int] = None
    current_value: Optional[float] = None
    threshold_value: Optional[float] = None
    message: str = ""
    # Merged into the alert's details JSON. Used by the flap damper to record
    # on the alert itself why it is being held open.
    details: Optional[Dict[str, Any]] = None


class AlertEngine:
    """
    Alert engine with state machine for hysteresis
    
    Alert Lifecycle:
    1. TRIGGERED - First time threshold exceeded
    2. ACTIVE - Still exceeding threshold
    3. RECOVERING - Below threshold but within buffer
    4. RESOLVED - Below recovery threshold, alert closed
    """
    
    # ifOperStatus values (RFC 2863) that mean the cable is not carrying
    # traffic. lowerLayerDown(7) is included because that is what a routed or
    # tunnelled interface reports when the physical port under it fails.
    # testing(3), unknown(4), dormant(5) and notPresent(6) are deliberately
    # absent: they are not "down", they are "the device is not telling us", and
    # an alert must not be raised or resolved on them.
    LINK_DOWN_STATUSES = frozenset({2, 7})
    LINK_UP_STATUSES = frozenset({1})

    # Default thresholds (used when no profile assigned)
    DEFAULT_THRESHOLDS = {
        "link_utilization": ThresholdConfig(warning=70, critical=90, recovery_buffer=10),
        "cpu": ThresholdConfig(warning=80, critical=95, recovery_buffer=10),
        "memory": ThresholdConfig(warning=85, critical=95, recovery_buffer=10),
    }
    
    def __init__(self, db: AsyncSession, damper=None):
        self.db = db
        # Flap damping for per-cable link_down alerts, or None for the
        # original behaviour (one alert and one recovery per flap). It is
        # passed in rather than created here because an engine lives for one
        # cycle and the damper has to remember across them, and because the
        # fast loop and the trap receiver must share one (app/core/damping.py).
        self.damper = damper
        self._profile_cache: Dict[int, Dict] = {}
        # Every active alert, indexed in memory for the duration of one check
        # cycle. None means "not loaded", and then every lookup is a query -
        # which is what the trap path wants, because it is asking about three
        # cables and has no use for the fleet's alerts.
        #
        # A full cycle is the opposite case: "is there already an alert for
        # this?" is asked once per device for offline, once each for CPU and
        # memory, once per merged link and once per cable. At 500 devices that
        # was 2,349 queries per cycle, and the alert checks moved from a 300
        # second loop to a 60 second one - see docs/PERFORMANCE.md section 20.
        self._active: Optional[Dict[Tuple, Alert]] = None
        # The devices a full cycle has already read. check_link_utilization
        # needs the device at one end of every merged link for its thresholds,
        # which was another query per link - 922 of them at 500 devices.
        self._devices: Optional[Dict[int, Device]] = None
    
    async def get_device_thresholds(self, device: Device) -> Dict[str, ThresholdConfig]:
        """Get thresholds for a device based on its profile"""
        if device.alert_profile_id:
            if device.alert_profile_id in self._profile_cache:
                return self._profile_cache[device.alert_profile_id]
            
            result = await self.db.execute(
                select(AlertProfile).where(AlertProfile.id == device.alert_profile_id)
            )
            profile = result.scalar_one_or_none()
            
            if profile and profile.thresholds:
                thresholds = {}
                for key, values in profile.thresholds.items():
                    if key == TRAP_KEY:
                        # Not a metric: {"trap": {"requires_poll": true}} is
                        # the per-device trap policy the trap receiver reads
                        # (app/core/damping.py), and reading it as a threshold
                        # would invent a warning=70 for a metric called
                        # "trap".
                        continue
                    if isinstance(values, dict):
                        thresholds[key] = ThresholdConfig(
                            warning=values.get("warning", 70),
                            critical=values.get("critical", 90),
                            recovery_buffer=values.get("recovery_buffer", 10)
                        )
                self._profile_cache[device.alert_profile_id] = thresholds
                return thresholds
        
        return self.DEFAULT_THRESHOLDS
    
    async def check_device_status(self, device: Device) -> List[AlertEvent]:
        """Check device online/offline status"""
        events = []
        
        # Device offline detection
        if device.status == "offline":
            # Check if there's already an active offline alert
            existing = await self._get_active_alert(
                device_id=device.id,
                alert_type=AlertType.DEVICE_OFFLINE
            )
            
            if not existing:
                events.append(AlertEvent(
                    alert_type=AlertType.DEVICE_OFFLINE,
                    severity=AlertSeverity.CRITICAL,
                    device_id=device.id,
                    message=f"Device {device.hostname} is offline"
                ))
        
        elif device.status == "managed":
            # Check if there's an offline alert to resolve
            existing = await self._get_active_alert(
                device_id=device.id,
                alert_type=AlertType.DEVICE_OFFLINE
            )
            
            if existing:
                events.append(AlertEvent(
                    alert_type=AlertType.DEVICE_ONLINE,
                    severity=AlertSeverity.INFO,
                    device_id=device.id,
                    message=f"Device {device.hostname} is back online"
                ))
        
        return events
    
    async def check_device_metrics(self, device: Device) -> List[AlertEvent]:
        """Check CPU and memory thresholds"""
        events = []
        thresholds = await self.get_device_thresholds(device)
        
        # CPU check
        if device.cpu_percent is not None:
            cpu_thresh = thresholds.get("cpu", self.DEFAULT_THRESHOLDS["cpu"])
            cpu_events = await self._check_metric(
                device_id=device.id,
                metric_name="cpu",
                current_value=device.cpu_percent,
                threshold=cpu_thresh,
                alert_type_high=AlertType.CPU_HIGH,
                alert_type_normal=AlertType.CPU_NORMAL,
                device_name=device.hostname
            )
            events.extend(cpu_events)
        
        # Memory check
        if device.memory_percent is not None:
            mem_thresh = thresholds.get("memory", self.DEFAULT_THRESHOLDS["memory"])
            mem_events = await self._check_metric(
                device_id=device.id,
                metric_name="memory",
                current_value=device.memory_percent,
                threshold=mem_thresh,
                alert_type_high=AlertType.MEMORY_HIGH,
                alert_type_normal=AlertType.MEMORY_NORMAL,
                device_name=device.hostname
            )
            events.extend(mem_events)
        
        return events
    
    async def check_link_utilization(self, link: MergedLink) -> List[AlertEvent]:
        """Check link utilization thresholds"""
        events = []
        
        # Get device for thresholds (use device_a)
        device = await self._get_device(link.device_a_id)
        if not device:
            return events
        
        thresholds = await self.get_device_thresholds(device)
        link_thresh = thresholds.get("link_utilization", self.DEFAULT_THRESHOLDS["link_utilization"])
        
        # Use max of in/out utilization.
        #
        # Either direction is None when it has not been measured: the first
        # cycle after the collector restarts, a counter that was reset, a port
        # whose interface could not be matched. "Not measured" is not "0%" -
        # folding it to 0 would resolve an active high-utilisation alert as if
        # the link had gone quiet, so a link with nothing measured is left
        # exactly as it is until there is a real sample again.
        measured = [
            value for value in (
                link.utilization_in_percent, link.utilization_out_percent
            ) if value is not None
        ]
        if not measured:
            return events
        max_util = max(measured)
        
        # Check threshold
        link_events = await self._check_metric(
            link_id=link.id,
            metric_name="link_utilization",
            current_value=max_util,
            threshold=link_thresh,
            alert_type_high=AlertType.LINK_HIGH_UTILIZATION,
            alert_type_normal=AlertType.LINK_UTILIZATION_NORMAL,
            device_name=f"Link {link.device_a_id} <-> {link.device_b_id}"
        )
        events.extend(link_events)
        
        return events
    
    async def check_link_states(
        self, raw_link_ids: Optional[Iterable[int]] = None
    ) -> List[AlertEvent]:
        """
        Alerts for individual cables whose local port reports itself down.

        Driven from raw_links.oper_status, which the fast loop writes once per
        fast cycle and the trap receiver writes within milliseconds of a
        linkDown. Pass ``raw_link_ids`` to look at those cables only - that is
        how a trap raises an alert for one port without a pass over the fleet.

        Deliberately not a pass over every cable: the normal answer is "none
        are down", so the query asks for the cables that are, plus the ones an
        alert is already open on. At 500 devices that is a handful of rows
        instead of 10,000.
        """
        events: List[AlertEvent] = []

        scope = None
        if raw_link_ids is not None:
            scope = {int(i) for i in raw_link_ids}
            if not scope:
                return events

        columns = (
            RawLink.id, RawLink.local_device_id, RawLink.local_port,
            RawLink.remote_hostname, RawLink.remote_port, RawLink.oper_status
        )

        if scope is None:
            where = RawLink.oper_status.in_(self.LINK_DOWN_STATUSES)
        else:
            where = RawLink.id.in_(scope)

        rows = (await self.db.execute(select(*columns).where(where))).all()
        by_link_id = {row.id: row for row in rows}

        # The open alerts are what decides whether anything has to change
        active_query = select(Alert).where(and_(
            Alert.is_active == True,
            Alert.alert_type == AlertType.LINK_DOWN.value
        ))
        if scope is not None:
            active_query = active_query.where(Alert.raw_link_id.in_(scope))
        active = (await self.db.execute(active_query)).scalars().all()
        active_by_link_id = {
            alert.raw_link_id: alert for alert in active if alert.raw_link_id
        }

        # An alerted cable that is not in the rows above is either up again or
        # gone from the inventory, and those are not the same thing, so ask.
        unknown = [
            link_id for link_id in active_by_link_id if link_id not in by_link_id
        ]
        if unknown:
            extra = (await self.db.execute(
                select(*columns).where(RawLink.id.in_(unknown))
            )).all()
            by_link_id.update({row.id: row for row in extra})

        hostnames = await self._hostnames(
            {row.local_device_id for row in by_link_id.values()}
        )

        def describe(row) -> str:
            local = f"{hostnames.get(row.local_device_id, row.local_device_id)} {row.local_port}"
            if row.remote_hostname:
                return f"{local} -> {row.remote_hostname} {row.remote_port or ''}".strip()
            return local

        for row in by_link_id.values():
            alert = active_by_link_id.get(row.id)
            is_down = row.oper_status in self.LINK_DOWN_STATUSES
            is_up = row.oper_status in self.LINK_UP_STATUSES

            # Only a real change counts as a flap: a cable seen down on ten
            # consecutive cycles is one event, not ten. Statuses that mean
            # "not known" are not observed at all, so a device going
            # unreachable cannot look like every one of its cables flapping.
            changed = False
            if self.damper is not None and (is_down or is_up):
                changed = self.damper.observe(row.id, down=is_down)

            if is_down:
                if alert is None:
                    events.append(AlertEvent(
                        alert_type=AlertType.LINK_DOWN,
                        severity=AlertSeverity.CRITICAL,
                        device_id=row.local_device_id,
                        raw_link_id=row.id,
                        current_value=float(row.oper_status),
                        message=f"Link down: {describe(row)}"
                    ))
            elif alert is not None and is_up:
                if self.damper is not None and self.damper.flapping(row.id):
                    if not changed:
                        # Still up, still inside the flap window: the alert
                        # is already held and already says so. Re-emitting it
                        # every cycle would write a history row a minute for
                        # a cable that is doing nothing right now.
                        continue
                    # The cable is up again, but it has been up and down too
                    # often to believe this one. Resolving here would raise
                    # the same alert again on the next down - one page per
                    # flap for the cable that most needs reading. The alert
                    # is kept open and told why instead, and it resolves on
                    # the first up after the cable has been steady for
                    # ALERT_LINK_FLAP_WINDOW.
                    changes = self.damper.count(row.id)
                    events.append(AlertEvent(
                        alert_type=AlertType.LINK_DOWN,
                        severity=AlertSeverity.CRITICAL,
                        device_id=row.local_device_id,
                        raw_link_id=row.id,
                        message=(
                            f"Link flapping: {describe(row)} - {changes} state "
                            f"changes in the last "
                            f"{self.damper.window / 60:.0f} min, holding the "
                            f"alert open until it settles"
                        ),
                        details={
                            "flapping": True,
                            "transitions": changes,
                            "window_seconds": self.damper.window,
                        }
                    ))
                    continue
                events.append(AlertEvent(
                    alert_type=AlertType.LINK_UP,
                    severity=AlertSeverity.INFO,
                    device_id=row.local_device_id,
                    raw_link_id=row.id,
                    current_value=float(row.oper_status),
                    message=f"Link back up: {describe(row)}"
                ))
            # Anything else (NULL because the device is unreachable or has not
            # been sampled yet, testing, dormant, notPresent) is "not known",
            # so an open alert is left exactly as it is.

        # A cable that no longer exists cannot recover, and without this its
        # alert would stay active for ever. The foreign key does that on a
        # database created with this column; one upgraded in place has no
        # constraint to do it (app/models/alert.py).
        for link_id, alert in active_by_link_id.items():
            if link_id in by_link_id:
                continue
            events.append(AlertEvent(
                alert_type=AlertType.LINK_UP,
                severity=AlertSeverity.INFO,
                device_id=alert.device_id,
                raw_link_id=link_id,
                message="Link no longer in the inventory, closing its alert"
            ))

        return events

    async def _hostnames(self, device_ids: Set[int]) -> Dict[int, str]:
        """{device_id: hostname} for the devices named, in one query"""
        if not device_ids:
            return {}
        if self._devices is not None:
            # A full cycle has already read them
            return {
                device_id: device.hostname
                for device_id, device in self._devices.items()
                if device_id in device_ids
            }
        rows = (await self.db.execute(
            select(Device.id, Device.hostname).where(Device.id.in_(device_ids))
        )).all()
        return {device_id: hostname for device_id, hostname in rows}

    async def _check_metric(
        self,
        metric_name: str,
        current_value: float,
        threshold: ThresholdConfig,
        alert_type_high: AlertType,
        alert_type_normal: AlertType,
        device_name: str,
        device_id: Optional[int] = None,
        link_id: Optional[int] = None
    ) -> List[AlertEvent]:
        """Generic metric threshold checking with hysteresis"""
        events = []
        
        # Get existing active alert
        existing = await self._get_active_alert(
            device_id=device_id,
            link_id=link_id,
            alert_type=alert_type_high
        )
        
        if current_value >= threshold.critical:
            if not existing:
                events.append(AlertEvent(
                    alert_type=alert_type_high,
                    severity=AlertSeverity.CRITICAL,
                    device_id=device_id,
                    link_id=link_id,
                    current_value=current_value,
                    threshold_value=threshold.critical,
                    message=f"{device_name} {metric_name} critical: {current_value:.1f}% (threshold: {threshold.critical}%)"
                ))
            elif existing.severity != AlertSeverity.CRITICAL.value:
                # Escalate to critical
                existing.severity = AlertSeverity.CRITICAL.value
                existing.current_value = current_value
                existing.threshold_value = threshold.critical
                existing.message = f"{device_name} {metric_name} critical: {current_value:.1f}%"
                
        elif current_value >= threshold.warning:
            if not existing:
                events.append(AlertEvent(
                    alert_type=alert_type_high,
                    severity=AlertSeverity.WARNING,
                    device_id=device_id,
                    link_id=link_id,
                    current_value=current_value,
                    threshold_value=threshold.warning,
                    message=f"{device_name} {metric_name} warning: {current_value:.1f}% (threshold: {threshold.warning}%)"
                ))
                
        elif existing:
            # Check recovery with hysteresis
            recovery_threshold = threshold.warning - threshold.recovery_buffer
            if current_value < recovery_threshold:
                events.append(AlertEvent(
                    alert_type=alert_type_normal,
                    severity=AlertSeverity.INFO,
                    device_id=device_id,
                    link_id=link_id,
                    current_value=current_value,
                    message=f"{device_name} {metric_name} recovered: {current_value:.1f}%"
                ))
        
        return events
    
    async def process_events(self, events: List[AlertEvent]):
        """Process alert events - create new alerts or resolve existing ones"""
        for event in events:
            # RECOVERY_CLOSES is the list of recovery event types, so it
            # decides the routing too. Sniffing the name for "_normal" meant
            # every new recovery type had to remember to be named that way -
            # link_up is not, and would have been filed as a new alert.
            if event.alert_type in self.RECOVERY_CLOSES:
                await self._resolve_alert(event)
            else:
                await self._create_or_update_alert(event)
        
        await self.db.commit()
    
    async def _create_or_update_alert(self, event: AlertEvent):
        """Create new alert or update existing one"""
        # Only non-recovery events reach here (process_events routes the
        # others), so the event's own type is the one to look for.
        existing = await self._get_active_alert(
            device_id=event.device_id,
            link_id=event.link_id,
            raw_link_id=event.raw_link_id,
            alert_type=event.alert_type
        )
        
        if existing:
            # Update existing alert. current_value only when the event
            # carries one: an event that is about the alert rather than about
            # a new reading (a flapping cable that is up at this instant)
            # must not overwrite the reading that raised it.
            if event.current_value is not None:
                existing.current_value = event.current_value
            existing.severity = event.severity.value
            existing.message = event.message
            if event.details:
                existing.details = {**(existing.details or {}), **event.details}
            
            # Add to history
            history = AlertHistory(
                alert_id=existing.id,
                event_type="updated",
                details={"value": event.current_value, "severity": event.severity.value}
            )
            self.db.add(history)
        else:
            # Create new alert
            alert = Alert(
                device_id=event.device_id,
                link_id=event.link_id,
                raw_link_id=event.raw_link_id,
                alert_type=event.alert_type.value,
                severity=event.severity.value,
                message=event.message,
                current_value=event.current_value,
                threshold_value=event.threshold_value,
                details=event.details,
                is_active=True
            )
            self.db.add(alert)
            await self.db.flush()
            if self._active is not None:
                # A second event for the same thing in the same cycle has to
                # find this one, or it would be filed as another new alert
                self._active[self._active_key(
                    event.device_id, event.link_id, event.raw_link_id,
                    event.alert_type.value
                )] = alert
            
            # Add to history
            history = AlertHistory(
                alert_id=alert.id,
                event_type="triggered",
                details={"value": event.current_value, "threshold": event.threshold_value}
            )
            self.db.add(history)
            
            logger.info(f"New alert: {event.alert_type.value} - {event.message}")
    
    # Which alert a recovery event closes. Spelled out rather than derived
    # from the name: "link_utilization_normal".replace("_normal", "_high")
    # gives "link_utilization_high", which is not a member of AlertType, so
    # resolving a link utilisation alert raised ValueError and took the whole
    # alert phase of the cycle down with it. It was unreachable while
    # utilisation was permanently 0 (section 9 of docs/PERFORMANCE.md); real
    # utilisation made it reachable.
    RECOVERY_CLOSES = {
        AlertType.DEVICE_ONLINE: AlertType.DEVICE_OFFLINE,
        AlertType.LINK_UTILIZATION_NORMAL: AlertType.LINK_HIGH_UTILIZATION,
        AlertType.LINK_UP: AlertType.LINK_DOWN,
        AlertType.CPU_NORMAL: AlertType.CPU_HIGH,
        AlertType.MEMORY_NORMAL: AlertType.MEMORY_HIGH,
    }

    async def _resolve_alert(self, event: AlertEvent):
        """Resolve an existing alert"""
        high_type = self.RECOVERY_CLOSES.get(event.alert_type)
        if high_type is None:
            logger.error(
                f"No alert type for {event.alert_type.value} to resolve - "
                f"add it to AlertEngine.RECOVERY_CLOSES"
            )
            return

        existing = await self._get_active_alert(
            device_id=event.device_id,
            link_id=event.link_id,
            raw_link_id=event.raw_link_id,
            alert_type=high_type
        )
        
        if existing:
            existing.is_active = False
            existing.recovered_at = datetime.utcnow()
            if self._active is not None:
                self._active.pop(self._active_key(
                    existing.device_id, existing.link_id,
                    existing.raw_link_id, existing.alert_type
                ), None)
            
            # Add to history
            history = AlertHistory(
                alert_id=existing.id,
                event_type="recovered",
                details={"value": event.current_value}
            )
            self.db.add(history)
            
            logger.info(f"Alert resolved: {event.message}")
    
    @staticmethod
    def _active_key(
        device_id: Optional[int],
        link_id: Optional[int],
        raw_link_id: Optional[int],
        alert_type: str
    ) -> Tuple:
        """
        The identity of an alert, as both the index and its lookups see it.

        An alert is about exactly one thing - a device, a merged link, or a
        cable - and every caller in this class looks it up by the same
        combination it was created with, so an exact key is equivalent to the
        WHERE clause it replaces. It is also better behaved than that clause
        on a database that somehow holds two active alerts for the same thing:
        this picks one, where scalar_one_or_none() raised.
        """
        return (alert_type, device_id or None, link_id or None, raw_link_id or None)

    async def preload_active_alerts(self) -> int:
        """Read every active alert into the index. One query."""
        alerts = (await self.db.execute(
            select(Alert).where(Alert.is_active == True)
        )).scalars().all()
        index: Dict[Tuple, Alert] = {}
        for alert in alerts:
            index.setdefault(
                self._active_key(alert.device_id, alert.link_id,
                                 alert.raw_link_id, alert.alert_type),
                alert
            )
        self._active = index
        return len(alerts)

    async def _get_active_alert(
        self,
        device_id: Optional[int] = None,
        link_id: Optional[int] = None,
        alert_type: Optional[AlertType] = None,
        raw_link_id: Optional[int] = None
    ) -> Optional[Alert]:
        """Get active alert matching criteria"""
        if self._active is not None and alert_type is not None:
            return self._active.get(self._active_key(
                device_id, link_id, raw_link_id, alert_type.value
            ))

        conditions = [Alert.is_active == True]
        
        if device_id:
            conditions.append(Alert.device_id == device_id)
        if link_id:
            conditions.append(Alert.link_id == link_id)
        if raw_link_id:
            # A device with two dead ports has two link_down alerts, so this
            # is what keeps the lookup single-valued.
            conditions.append(Alert.raw_link_id == raw_link_id)
        if alert_type:
            conditions.append(Alert.alert_type == alert_type.value)
        
        result = await self.db.execute(
            select(Alert).where(and_(*conditions))
        )
        return result.scalar_one_or_none()
    
    async def _get_device(self, device_id: int) -> Optional[Device]:
        """Get device by ID, from the cycle's own list when it has one"""
        if self._devices is not None:
            return self._devices.get(device_id)
        result = await self.db.execute(
            select(Device).where(Device.id == device_id)
        )
        return result.scalar_one_or_none()
    
    async def run_check_cycle(self):
        """Run a complete alert check cycle for all devices and links"""
        all_events = []

        # One query for every alert that is currently open, instead of one per
        # question asked below
        open_alerts = await self.preload_active_alerts()
        
        # Check all devices
        devices_result = await self.db.execute(select(Device))
        devices = devices_result.scalars().all()
        self._devices = {device.id: device for device in devices}
        
        for device in devices:
            events = await self.check_device_status(device)
            all_events.extend(events)
            
            events = await self.check_device_metrics(device)
            all_events.extend(events)
        
        # Check all links
        links_result = await self.db.execute(select(MergedLink))
        links = links_result.scalars().all()
        
        for link in links:
            events = await self.check_link_utilization(link)
            all_events.extend(events)

        # Per-cable link state. Separate from the merged link above: a bundle
        # of four cables with one of them dead is not a link at 0%, and the
        # port that died is the thing an operator has to go and look at.
        all_events.extend(await self.check_link_states())

        # Forget cables that are up and have not changed state inside the
        # flap window, so the damper's memory tracks the cables that are
        # misbehaving rather than every cable that ever has.
        if self.damper is not None:
            self.damper.prune()
        
        # Process all events
        await self.process_events(all_events)
        
        logger.info(
            f"Alert check cycle completed: {len(all_events)} events processed, "
            f"{open_alerts} alerts were open"
        )
