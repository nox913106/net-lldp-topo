"""
Offline device backoff for the collector

An unreachable device is the most expensive thing in a poll cycle: every OID
the collector asks for waits the full ``snmp_timeout`` once per ``snmp_retries``
before giving up, so a single dead device burns tens of seconds of wall time
every interval while never producing data.

``OfflineBackoff`` tracks consecutive failures per device and grows the delay
between attempts, so a device that has been down for an hour is probed every
``max_interval`` instead of every ``base_interval``.

The state is deliberately kept in memory. The schema is created with
``Base.metadata.create_all`` and the project ships no migrations, so adding a
column to ``devices`` would break every existing deployment on the next
SELECT. The price of keeping it in memory is one extra probe round per offline
device after a collector restart.
"""
import time
from dataclasses import dataclass
from typing import Dict, Hashable, Iterable, List, Optional, Tuple


@dataclass
class BackoffState:
    """Per-device failure bookkeeping"""
    failures: int = 0
    last_attempt: float = 0.0


class OfflineBackoff:
    """
    Exponential backoff for devices that do not answer SNMP.

    After ``n`` consecutive failures the next attempt is allowed
    ``base_interval * 2 ** (n - confirm_after)`` seconds after the last one,
    capped at ``max_interval``. With the defaults (base 300s, cap 1800s,
    confirm_after 1) that is 300s, 600s, 1200s, 1800s, 1800s, ...

    A single failure therefore never delays anything: the first retry happens
    at the normal poll interval, so a one-off timeout costs nothing. Only a
    device that keeps failing is backed off.

    ``confirm_after`` is ALERT_OFFLINE_CONFIRM_CYCLES: while a device's
    failures are still only *deciding* whether it is offline, every retry
    happens at the normal interval. Otherwise confirming three failures on a
    60 s loop would take 60 + 120 + 240 = 7 minutes instead of 3, and the
    confirmation window would silently become a function of the backoff
    curve.

    Times come from ``time.monotonic`` so the schedule is immune to wall-clock
    changes (NTP steps, DST) on the collector host.
    """

    def __init__(
        self,
        base_interval: int,
        max_interval: int,
        enabled: bool = True,
        clock=time.monotonic,
        confirm_after: int = 1
    ):
        self.base_interval = max(1, int(base_interval))
        self.max_interval = max(self.base_interval, int(max_interval))
        self.enabled = enabled
        self._clock = clock
        # Failures that are still deciding whether the device is offline at
        # all (ALERT_OFFLINE_CONFIRM_CYCLES) are polled at the normal
        # interval, not backed off: a confirmation that waits for the backoff
        # is not a confirmation, it is a delay. With the default of 1 the
        # schedule below is unchanged.
        self.confirm_after = max(1, int(confirm_after))
        self._states: Dict[Hashable, BackoffState] = {}

    # -- schedule ---------------------------------------------------------

    def delay_for(self, failures: int) -> float:
        """Seconds that have to pass after ``failures`` consecutive failures"""
        if failures <= self.confirm_after:
            return float(self.base_interval)
        # Cap the exponent as well, otherwise 2 ** failures overflows into a
        # huge int for a device that has been down for months
        exponent = min(failures - self.confirm_after, 32)
        return float(min(self.base_interval * (2 ** exponent), self.max_interval))

    def should_poll(self, key: Hashable) -> bool:
        """True when this device may be probed now"""
        if not self.enabled:
            return True

        state = self._states.get(key)
        if state is None or state.failures == 0:
            return True

        waited = self._clock() - state.last_attempt
        return waited >= self.delay_for(state.failures)

    def seconds_until_retry(self, key: Hashable) -> float:
        """Seconds left before this device is probed again (0 when due now)"""
        state = self._states.get(key)
        if not self.enabled or state is None or state.failures == 0:
            return 0.0
        remaining = self.delay_for(state.failures) - (self._clock() - state.last_attempt)
        return max(0.0, remaining)

    # -- outcome reporting ------------------------------------------------

    def record_success(self, key: Hashable) -> None:
        """Clear the backoff for a device that answered"""
        self._states.pop(key, None)

    def record_failure(self, key: Hashable) -> int:
        """Register a failed attempt, returns the consecutive failure count"""
        state = self._states.get(key)
        if state is None:
            state = BackoffState()
            self._states[key] = state
        state.failures += 1
        state.last_attempt = self._clock()
        return state.failures

    def failure_count(self, key: Hashable) -> int:
        state = self._states.get(key)
        return state.failures if state else 0

    # -- housekeeping -----------------------------------------------------

    def prune(self, active_keys: Iterable[Hashable]) -> int:
        """
        Drop state for devices that no longer exist.

        Called once per cycle so a long-running collector cannot accumulate
        entries for deleted devices.
        """
        active = set(active_keys)
        stale = [key for key in self._states if key not in active]
        for key in stale:
            del self._states[key]
        return len(stale)

    def tracked(self) -> int:
        """Number of devices currently in backoff"""
        return len(self._states)

    def pending(self) -> List[Tuple[Hashable, int, float]]:
        """(key, failures, seconds_until_retry) for every backed-off device"""
        return [
            (key, state.failures, self.seconds_until_retry(key))
            for key, state in self._states.items()
            if state.failures > 0
        ]
