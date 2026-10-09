"""
Coordination between collector processes.

Only needed once the fleet is split across several collector processes
(app/core/sharding.py). A single collector writes one row here and reads it
back, which costs one statement per cycle and keeps the two deployments on the
same code path.
"""
from sqlalchemy import Column, DateTime, Integer, String

from app.db.database import Base


class CollectorLease(Base):
    """
    A named, expiring claim held by one collector process.

    Two kinds of row live here, and the difference is only in who competes
    for them:

      * ``fleet-wide`` - contended. Rebuilding merged_links and running the
        alert cycle read the whole fleet and must happen once per interval,
        not once per shard, so exactly one process holds this and the others
        skip that phase. If the holder dies, the row expires and another
        process takes it over.
      * ``member-<n>`` - uncontended. Shard n renewing its own row, so that
        the process doing the fleet-wide work can tell that every shard of
        the configured count is actually running. The failure this catches is
        COLLECTOR_SHARDS=4 with three processes started, where a quarter of
        the fleet is simply never polled and the only symptom is those
        devices slowly alerting as offline.

    ``expires_at`` is written from the *database's* clock, never from a
    collector's, so that processes on different hosts with drifting clocks
    still agree on whether a lease is live - see app/core/lease.py.
    """
    __tablename__ = "collector_leases"

    name = Column(String(64), primary_key=True)
    owner = Column(String(128), nullable=False)
    # Set on takeover (a different owner), not on renewal: the pair
    # (acquired_at, renewed_at) is what tells an operator "this process has
    # been doing the fleet-wide work for two days" from
    # "this changed hands a minute ago".
    acquired_at = Column(DateTime(timezone=True))
    renewed_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # Number of takeovers, so a lease flapping between two processes - which
    # is what a too-short TTL looks like - is visible in one SELECT.
    takeovers = Column(Integer, default=0)
