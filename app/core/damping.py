"""
Three ways of not believing one measurement.

Every alert in this project is raised from a UDP exchange, and three of those
exchanges can be wrong in ways that have nothing to do with the network being
broken:

  * A GET can be lost. SNMP_RETRIES already asks three times, but all three
    datagrams take the same path, so a congested uplink or a busy agent can
    swallow the lot. The device is then "not answering" for exactly one cycle,
    which used to be enough to raise a critical device_offline and resolve it
    again a minute later.
  * A port can bounce. One cable that flaps every few seconds produced a
    link_down and a link_up per flap - a page per flap - when what the
    operator needs is one alert saying "this cable is flapping".
  * A trap is a single unacknowledged datagram that anybody on the network can
    forge, and it alerts in milliseconds precisely because nothing has
    confirmed it yet.

What is here is the state those three need, kept out of AlertEngine because an
AlertEngine lives for one cycle and these have to remember across cycles. It
is per-process memory, which is sound for the first two because a device has
exactly one owning process once the fleet is sharded (app/core/sharding.py),
and because the fleet-wide phase runs on one process at a time. A restart
clears it: a device that is already alerting stays alerting (the alert row is
in the database), but an in-progress confirmation starts again, which errs
towards not alerting.

Every default here reproduces the behaviour this module replaced: confirm
after one failure, no flap damping, and traps alert immediately.
"""
import logging
import time
from dataclasses import dataclass
from typing import (
    Any, Dict, Hashable, Iterable, List, Mapping, Optional, Tuple
)

logger = logging.getLogger(__name__)

# The key an alert profile's thresholds JSON uses for trap behaviour. It sits
# beside the metric thresholds ("cpu", "memory", "link_utilization") rather
# than in a column of its own because the thresholds blob is already the
# per-device alerting policy, and the API passes it through untouched.
TRAP_KEY = "trap"


@dataclass(frozen=True)
class Verdict:
    """What one unanswered poll means"""

    failures: int
    required: int
    confirmed: bool

    def describe(self) -> str:
        return f"{self.failures}/{self.required} consecutive failures"


class OfflineConfirm:
    """
    How many consecutive unanswered polls before a device is called offline.

    ``cycles=1`` is the original behaviour: the first unanswered poll writes
    ``devices.status = offline`` and the alert cycle raises device_offline from
    it. Above 1, the device keeps its current status until the n-th failure in
    a row, so a single lost exchange costs nothing - at the price of n-1 poll
    intervals of extra latency on a device that really has gone.

    Both collector loops share one instance, so a failure counts whichever
    loop saw it. That is the point: the fast loop asks every
    COLLECTOR_FAST_INTERVAL and is the one that will usually confirm, while
    the inventory loop's own failures should not be counted separately as if
    they were about a different device.

    The offline backoff is told about the same threshold
    (app/core/poll_scheduler.py), so the polls that do the confirming run at
    the normal interval instead of being backed off - otherwise confirming
    three failures with a 60 s fast loop would take 7 minutes rather than 3.
    """

    __slots__ = ("cycles", "_failures")

    def __init__(self, cycles: int = 1):
        self.cycles = max(1, int(cycles))
        self._failures: Dict[Hashable, int] = {}

    @property
    def enabled(self) -> bool:
        """Whether this delays anything at all"""
        return self.cycles > 1

    def record_failure(self, device_id: Hashable) -> Verdict:
        """Count an unanswered poll and say whether the device is now offline"""
        failures = self._failures.get(device_id, 0) + 1
        self._failures[device_id] = failures
        return Verdict(
            failures=failures,
            required=self.cycles,
            confirmed=failures >= self.cycles
        )

    def record_success(self, device_id: Hashable) -> None:
        """A device that answered is not part way to being offline"""
        self._failures.pop(device_id, None)

    def failures(self, device_id: Hashable) -> int:
        return self._failures.get(device_id, 0)

    def pending(self) -> int:
        """Devices that have failed at least once without being confirmed"""
        return sum(
            1 for failures in self._failures.values() if failures < self.cycles
        )

    def pending_devices(
        self, limit: int = 50
    ) -> List[Tuple[Hashable, int]]:
        """
        Which devices are mid-confirmation, and how far, for the API's status.

        ``(device_id, failures)`` - the progress is the point: "2 of 3
        failures" is the difference between damping working and the collector
        having stopped noticing. Bounded, because during a site outage this
        is every device behind the failed uplink and the list is only worth
        showing if it stays readable; pending() has the real count.
        """
        found = [
            (device_id, failures)
            for device_id, failures in self._failures.items()
            if failures < self.cycles
        ]
        found.sort(key=lambda pair: str(pair[0]))
        return found[:max(0, int(limit))]

    def prune(self, device_ids: Iterable[Hashable]) -> None:
        """Forget devices that no longer exist"""
        keep = set(device_ids)
        for device_id in [d for d in self._failures if d not in keep]:
            del self._failures[device_id]


class FlapDamper:
    """
    One alert for a flapping cable instead of one per flap.

    A cable that goes down and up every few seconds is a real fault, but it is
    *one* fault. Without this, every down raised a link_down and every up
    resolved it, so a fibre flapping once a minute produced an alert and a
    recovery every minute, and the alert history became unreadable for exactly
    the cable that most needed reading.

    This counts state changes per cable inside a sliding window. Once a cable
    has changed state ``transitions`` times inside ``window_seconds`` the
    caller holds its link_down alert open through the ups instead of resolving
    it (app/core/alert_engine.py), until the cable has been steady for long
    enough that the window empties.

    The first down is never delayed: a cable that goes down and stays down
    alerts exactly as fast as before. What is suppressed is the stream of
    recoveries and re-triggers after it.

    ``transitions < 2`` disables the whole thing, which is the default.
    """

    __slots__ = ("transitions", "window", "_clock", "_changes", "_down")

    def __init__(
        self,
        transitions: int = 0,
        window_seconds: float = 600.0,
        clock=time.monotonic
    ):
        self.transitions = int(transitions)
        self.window = max(1.0, float(window_seconds))
        self._clock = clock
        # Only the last `transitions` timestamps matter: flapping is "the
        # n-th most recent change is still inside the window". A list rather
        # than a deque because there is one of these per misbehaving cable
        # and a deque object is ~600 bytes against a 3-element list's ~120.
        self._changes: Dict[int, List[float]] = {}
        self._down: Dict[int, bool] = {}

    @property
    def enabled(self) -> bool:
        return self.transitions >= 2

    def observe(self, link_id: int, down: bool) -> bool:
        """
        Record this cable's state. Returns whether this was a *change*: being
        seen down on ten consecutive cycles is one event, not ten, and the
        caller uses the answer to annotate the alert once per flap rather
        than once per cycle.
        """
        if not self.enabled:
            return False

        previous = self._down.get(link_id)
        self._down[link_id] = down
        if previous is None or previous == down:
            return False

        changes = self._changes.get(link_id)
        if changes is None:
            changes = []
            self._changes[link_id] = changes
        changes.append(self._clock())
        if len(changes) > self.transitions:
            del changes[:-self.transitions]
        return True

    def flapping(self, link_id: int) -> bool:
        """Whether this cable has changed state too often to be believed"""
        return self.enabled and self._count(link_id) >= self.transitions

    def count(self, link_id: int) -> int:
        return self._count(link_id)

    def flapping_links(self, limit: int = 50) -> List[Tuple[int, int]]:
        """
        Every cable counted as flapping, with its change count, for the API.

        Only cables that have changed state recently are in the dictionary at
        all (prune() drops the rest), so this walks a handful of entries on a
        healthy fleet. Bounded for the same reason as
        OfflineConfirm.pending_devices().
        """
        if not self.enabled:
            return []
        found = [
            (link_id, count) for link_id, count in (
                (link_id, self._count(link_id))
                for link_id in list(self._changes)
            )
            if count >= self.transitions
        ]
        found.sort()
        return found[:max(0, int(limit))]

    def flapping_count(self) -> int:
        """How many cables are flapping, unbounded"""
        if not self.enabled:
            return 0
        return sum(
            1 for link_id in list(self._changes)
            if self._count(link_id) >= self.transitions
        )

    def forget(self, link_id: int) -> None:
        self._changes.pop(link_id, None)
        self._down.pop(link_id, None)

    def prune(self) -> None:
        """
        Drop cables that are up and have not changed state inside the window.

        Called once per cycle: without it, every cable that has ever been down
        would stay in these dictionaries for the life of the process.
        """
        if not self._down and not self._changes:
            return
        for link_id in [
            link_id for link_id, down in self._down.items()
            if not down and self._count(link_id) == 0
        ]:
            self.forget(link_id)

    def _count(self, link_id: int) -> int:
        changes = self._changes.get(link_id)
        if not changes:
            return 0
        cutoff = self._clock() - self.window
        while changes and changes[0] < cutoff:
            # At most `transitions` entries, so the shift is on a 3-element
            # list rather than a long one
            del changes[0]
        if not changes:
            self._changes.pop(link_id, None)
            return 0
        return len(changes)


def trap_requires_poll(thresholds: Optional[Mapping[str, Any]], default: bool) -> bool:
    """
    Whether a trap about this device's cables waits for a poll to confirm it.

    Read from the device's alert profile, falling back to the global default
    (ALERT_TRAP_REQUIRES_POLL). A profile that says nothing about traps gets
    the default, so adding this changed no existing profile's behaviour.

    False (the default) is what the trap receiver has always done: the
    notification itself raises the alert, in milliseconds. True trades that
    latency for corroboration - the trap still writes the port's state, so the
    topology and the UI are current immediately, but the alert waits for the
    next fast cycle to agree with it. That is the right choice for a device
    whose traps have to cross an untrusted network, and the wrong one for the
    device whose link_down is the whole reason traps were turned on, which is
    why it is per device rather than global.
    """
    if not thresholds:
        return default
    section = thresholds.get(TRAP_KEY)
    if not isinstance(section, Mapping):
        return default
    value = section.get("requires_poll")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default
