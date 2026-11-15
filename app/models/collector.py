"""
Coordination between collector processes.

Only needed once the fleet is split across several collector processes
(app/core/sharding.py). A single collector writes one row here and reads it
back, which costs one statement per cycle and keeps the two deployments on the
same code path.
"""
from sqlalchemy import Column, Integer, JSON, String

from app.db.database import Base
from app.db.types import UTCDateTime


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
    acquired_at = Column(UTCDateTime)
    renewed_at = Column(UTCDateTime)
    expires_at = Column(UTCDateTime, nullable=False)
    # Number of takeovers, so a lease flapping between two processes - which
    # is what a too-short TTL looks like - is visible in one SELECT.
    takeovers = Column(Integer, default=0)

    # What this process is doing, written by the owner on every renewal.
    #
    # The API runs in a different process from the collectors and shares no
    # memory with them, so without this there is no way to show an operator
    # which damping is in effect or how many devices are mid-confirmation -
    # and worse, the API reading its *own* environment would confidently
    # display defaults while the collector ran with something else (in
    # docker-compose the ALERT_* variables are set on the collector service
    # only). So the numbers come from the process that actually has them.
    #
    # It rides along on a renewal that was happening anyway, so it costs no
    # extra statement. Nullable, because a collector from before this column
    # existed simply leaves it empty.
    status = Column(JSON)
