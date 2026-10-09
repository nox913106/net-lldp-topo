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



def sample_values(previous, stat, sampled_at: datetime) -> dict:
    """
    The capacity and traffic columns a raw_links row should carry after this
    poll, as a plain dict so the write can be batched.

    `previous` is what the row holds now - anything carrying bandwidth_mbps,
    in_octets, out_octets and sampled_at, or None for a cable seen for the
    first time. `stat` is the interface this cable was matched to, or None when
    it could not be matched to one.

    The capacity and the counter samples are kept whenever there is nothing
    better; the rates are not. A stored rate is only valid for the interval it
    was measured over, so anything that interrupts measurement - the interface
    not being found, traffic collection being switched off - clears it rather
    than leaving the last one frozen in place. None means "not measured", and
    that has to stay distinguishable from an idle link.
    """
    previous_bandwidth = previous.bandwidth_mbps if previous is not None else None
    previous_in = previous.in_octets if previous is not None else None
    previous_out = previous.out_octets if previous is not None else None
    previous_at = previous.sampled_at if previous is not None else None

    def unmeasured(bandwidth):
        return {
            "bandwidth_mbps": bandwidth,
            "in_octets": previous_in,
            "out_octets": previous_out,
            "in_bps": None,
            "out_bps": None,
            "sampled_at": previous_at,
        }

    if stat is None:
        # The cable is still there; we just could not find the interface it
        # lands on. Nothing was measured this cycle.
        return unmeasured(previous_bandwidth)

    bandwidth = previous_bandwidth
    if stat.speed_mbps and stat.speed_mbps > 0:
        # ifHighSpeed answered: the device's own number, which replaces any
        # earlier guess from the port name.
        bandwidth = stat.speed_mbps

    if stat.in_octets is None and stat.out_octets is None:
        # Traffic collection is off (COLLECTOR_TRAFFIC_STATS=false). The real
        # port speed and the cabling inventory are still being collected; the
        # counter samples stay as a valid starting point if it is switched back
        # on - counter_rate rejects a gap that long on its own anyway.
        return unmeasured(bandwidth)

    return {
        "bandwidth_mbps": bandwidth,
        "in_octets": stat.in_octets,
        "out_octets": stat.out_octets,
        "in_bps": counter_rate(previous_in, previous_at, stat.in_octets, sampled_at),
        "out_bps": counter_rate(previous_out, previous_at, stat.out_octets, sampled_at),
        "sampled_at": sampled_at,
    }
