"""
Turning SNMP interface counters into rates.

ifHCInOctets / ifHCOutOctets are monotonically increasing counters, not rates.
A rate only exists between two samples, which is why the previous sample is
persisted on the link row: see app/models/link.py.
"""
from datetime import datetime, timezone
from typing import Optional

# A sample pair this far apart is not used. The device may have rebooted (and
# its counters restarted) one or more times in between, and a rate averaged
# over hours says nothing useful about a 5 minute poll cycle anyway.
MAX_SAMPLE_GAP_SECONDS = 4 * 3600

# Two samples closer together than this are not divided - the result would be
# dominated by the timing jitter of the poll cycle rather than by traffic.
MIN_SAMPLE_GAP_SECONDS = 1.0


def _as_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    """
    Normalise a timestamp to naive UTC.

    Needed because the same column comes back naive from SQLite and
    timezone-aware from PostgreSQL's timestamptz, and subtracting one from the
    other raises TypeError.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def counter_rate(
    previous: Optional[int],
    previous_time: Optional[datetime],
    current: Optional[int],
    current_time: datetime
) -> Optional[int]:
    """
    Bits per second between two octet counter samples.

    Returns None - not 0 - whenever a rate cannot be derived, so that "no
    measurement yet" stays distinguishable from "no traffic":

      * first sample for this port (nothing to subtract from)
      * the counter went backwards, which in practice means the device
        rebooted or the counter was cleared. A genuine wrap of a 64 bit octet
        counter takes about 46 years at 100 Gbps, so a decrease is never
        treated as a wrap.
      * the samples are too close together, or too far apart to be meaningful
    """
    if current is None or previous is None or previous_time is None:
        return None
    if current < previous:
        return None

    previous_time = _as_utc_naive(previous_time)
    current_time = _as_utc_naive(current_time)
    elapsed = (current_time - previous_time).total_seconds()

    if elapsed < MIN_SAMPLE_GAP_SECONDS or elapsed > MAX_SAMPLE_GAP_SECONDS:
        return None

    return int((current - previous) * 8 / elapsed)


def utilization_percent(bps: Optional[int], bandwidth_mbps: Optional[int]) -> Optional[float]:
    """Utilisation of a link in percent, or None when either input is unknown"""
    if bps is None or not bandwidth_mbps or bandwidth_mbps <= 0:
        return None
    return (bps / (bandwidth_mbps * 1_000_000)) * 100
