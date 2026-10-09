"""
Process-local memory for LLDP/CDP auto-discovery.

Without it, auto_discover_neighbor() asks the database "do I already know this
neighbour?" twice for every neighbour of every device on every cycle - roughly
20,000 queries per cycle at 500 devices, for an answer that barely changes
between cycles. The whole fleet's hostnames and addresses fit in a set, so the
answer is read from memory and refreshed once per cycle instead.

It also remembers which neighbours could not be turned into a device (no
management address, no DNS record, no SNMP answer - phones, printers, APs with
SNMP closed), so those are not re-probed every cycle. Those entries expire, so
a device that later becomes reachable is still picked up.

Not shared between processes: each collector process keeps its own copy, and
every entry is re-derived from the database on the next cycle.
"""
import logging
import time
from typing import Dict, Iterable, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class DiscoveryCache:
    """Known devices plus dead ends, for one collector process"""

    def __init__(self, negative_ttl: float = 3600.0, max_negative: int = 50_000):
        self.negative_ttl = negative_ttl
        self.max_negative = max_negative
        self._hostnames: Set[str] = set()
        self._ips: Set[str] = set()
        # hostname -> time it may be probed again
        self._dead_ends: Dict[str, float] = {}
        # Neighbours a coroutine is probing right now. Two devices often see
        # the same new neighbour in the same cycle; without this they would
        # both resolve it, both probe it over SNMP and both try to insert it.
        self._in_flight: Set[str] = set()

    def seed(self, rows: Iterable[Tuple[Optional[str], Optional[str]]]) -> None:
        """
        Replace what is known with (hostname, ip_address) pairs from the database.

        Called once per cycle. Must include every device, excluded ones too -
        otherwise an excluded device gets re-discovered and re-added.
        """
        hostnames = set()
        ips = set()
        for hostname, ip in rows:
            if hostname:
                hostnames.add(hostname)
            if ip:
                ips.add(ip)
        self._hostnames = hostnames
        self._ips = ips
        self._expire_dead_ends()

    def knows(self, hostname: Optional[str] = None, ip: Optional[str] = None) -> bool:
        """True when a device with this hostname or address already exists"""
        if hostname and hostname in self._hostnames:
            return True
        if ip and ip in self._ips:
            return True
        return False

    def is_dead_end(self, hostname: str) -> bool:
        """True when this neighbour recently failed to resolve into a device"""
        until = self._dead_ends.get(hostname)
        if until is None:
            return False
        if until <= time.monotonic():
            self._dead_ends.pop(hostname, None)
            return False
        return True

    def claim(self, hostname: str) -> bool:
        """
        Take responsibility for probing this neighbour.

        Returns False when it is already known, a known dead end, or another
        coroutine is probing it. Callers must release() what they claim.

        Contains no await on purpose: within one event loop the check and the
        insert cannot interleave, which is what makes it a usable lock.
        """
        if not hostname:
            return False
        if hostname in self._hostnames or hostname in self._in_flight:
            return False
        if self.is_dead_end(hostname):
            return False
        self._in_flight.add(hostname)
        return True

    def release(self, hostname: str) -> None:
        self._in_flight.discard(hostname)

    def add(self, hostname: str, ip: str) -> None:
        """Record a device that was just created"""
        if hostname:
            self._hostnames.add(hostname)
            self._dead_ends.pop(hostname, None)
        if ip:
            self._ips.add(ip)

    def mark_dead_end(self, hostname: str) -> None:
        """Do not probe this neighbour again until the entry expires"""
        if not hostname:
            return
        if len(self._dead_ends) >= self.max_negative:
            self._expire_dead_ends()
            if len(self._dead_ends) >= self.max_negative:
                # Still full: a fleet this noisy is better off forgetting the
                # oldest entries than growing without bound.
                self._dead_ends.clear()
                logger.debug("Discovery dead-end cache full, cleared")
        self._dead_ends[hostname] = time.monotonic() + self.negative_ttl

    def _expire_dead_ends(self) -> None:
        now = time.monotonic()
        expired = [h for h, until in self._dead_ends.items() if until <= now]
        for hostname in expired:
            del self._dead_ends[hostname]

    def stats(self) -> Dict[str, int]:
        return {
            "known_hostnames": len(self._hostnames),
            "known_ips": len(self._ips),
            "dead_ends": len(self._dead_ends),
        }
