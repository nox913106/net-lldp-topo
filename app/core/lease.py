"""
The one thing sharding cannot divide: work that is about the fleet.

Each collector process polls its own slice of the devices
(app/core/sharding.py), and that part needs no coordination at all - a device
has exactly one owner, so two processes never write the same row. Two things
in a cycle are not like that:

  * ``TopologyEngine.update_merged_links_in_db()`` reads every raw link and
    rewrites the whole merged_links table. N processes doing that at the same
    time would each delete and rebuild rows the others just wrote.
  * ``AlertEngine.run_check_cycle()`` reads every device and every merged
    link. N processes doing it would evaluate the same thresholds N times and
    race to create the same alert - and since "is there already an alert for
    this?" is a SELECT followed by an INSERT, the race produces duplicate
    alerts rather than one.

So exactly one process does that phase per interval, chosen by holding a
lease: a row with an owner and an expiry. The holder renews it on every cycle;
if the holder dies, the row expires and the next process to ask takes over.
That is the whole mechanism. It is deliberately in the database rather than in
Redis: the collector cannot do anything at all without the database, so a
lease there adds no new way for the collector to fail, while a lease in Redis
would mean a Redis outage stops alerting even though every device is being
polled normally (docs/PERFORMANCE.md section 15.5 reached the same conclusion
about caching).

**The clock is the database's, never a collector's.** Two processes on two
hosts whose clocks differ by a minute would otherwise disagree about whether a
lease is live, and both would do the fleet-wide work. Every expiry written
here and every comparison against it is evaluated inside the database, so the
only clock involved is the one all shards share.
"""
import logging
from datetime import datetime, timedelta
from typing import List, NamedTuple, Optional, Tuple

from sqlalchemy import literal_column, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import async_session_maker, engine
from app.models.collector import CollectorLease

logger = logging.getLogger(__name__)

# The contended lease: merged-link refresh plus the alert check cycle.
FLEET_LEASE = "fleet-wide"

# One uncontended row per shard, so the holder of the lease above can tell
# whether every shard of the configured count is actually running.
MEMBER_PREFIX = "member-"

_WARNED_DIALECTS = set()


class _Clock:
    """
    SQL for "now" and "now + n seconds" in the dialect at hand.

    Both halves have to come from the same clock as the comparison, which is
    why these are SQL fragments and not Python values. The seconds are
    formatted into the SQL rather than bound, which is safe because they are
    cast to int here and never come from anything but a configured interval.
    """

    def __init__(self, dialect: str):
        self.dialect = dialect
        self.database_clock = dialect in ("postgresql", "sqlite")
        if not self.database_clock and dialect not in _WARNED_DIALECTS:
            _WARNED_DIALECTS.add(dialect)
            logger.warning(
                "Collector leases on %s fall back to each process's own clock; "
                "shards on hosts whose clocks differ by more than the lease "
                "TTL can both run the fleet-wide phase", dialect
            )

    @property
    def now(self) -> str:
        if self.dialect == "postgresql":
            return "now()"
        if self.dialect == "sqlite":
            return "CURRENT_TIMESTAMP"
        return ":_now"

    def plus(self, seconds: float) -> str:
        seconds = int(seconds)
        if self.dialect == "postgresql":
            return f"now() + interval '{seconds} seconds'"
        if self.dialect == "sqlite":
            return f"datetime('now', '+{seconds} seconds')"
        return ":_expiry"

    def params(self, seconds: float) -> dict:
        """Bound times for the fallback path, empty when the database has it"""
        if self.database_clock:
            return {}
        now = datetime.utcnow()
        return {"_now": now, "_expiry": now + timedelta(seconds=int(seconds))}


def _clock() -> _Clock:
    return _Clock(engine.dialect.name)


class LeaseHolder(NamedTuple):
    """Who holds a lease right now, as the database sees it"""
    name: str
    owner: str
    renewed_at: Optional[datetime]
    expires_at: Optional[datetime]
    live: bool
    takeovers: int


class Lease:
    """
    A named claim that one process holds and the others wait for.

    ``acquire()`` is both "take it" and "renew it": the owner's own renewal
    and a takeover from an expired owner are the same statement, so there is
    no window in which the holder has to release before renewing.

    Every call runs in its own session on purpose. The caller is usually in
    the middle of a cycle with writes buffered in its own transaction, and a
    lease that lost a race must not be able to roll that back.
    """

    def __init__(
        self,
        name: str,
        owner: str,
        ttl_seconds: float,
        session_factory=None
    ):
        self.name = name
        self.owner = owner
        self.ttl = max(1.0, float(ttl_seconds))
        self._session_factory = session_factory or async_session_maker
        self._held = False
        self._since: Optional[float] = None

    @property
    def held(self) -> bool:
        """Whether the last acquire() succeeded. Not a fresh check."""
        return self._held

    async def acquire(self) -> bool:
        """
        Take the lease, or renew it if this process already holds it.

        Returns False when another live owner holds it, and on any database
        error - the caller then skips the fleet-wide phase for this cycle,
        which is the safe direction: the work is idempotent across intervals
        and will be done by whoever does hold it.
        """
        clock = _clock()
        try:
            async with self._session_factory() as db:
                taken = await self._renew_or_steal(db, clock)
                if not taken:
                    # No row yet: the first process to start creates it. A
                    # second one arriving at the same moment hits the
                    # conflict, changes nothing, and falls back to competing
                    # for the row that now exists.
                    created = await self._insert(db, clock)
                    if created:
                        taken = True
                    else:
                        taken = await self._renew_or_steal(db, clock)
                await db.commit()
        except SQLAlchemyError as e:
            logger.error(f"Lease {self.name}: {e}")
            self._held = False
            return False

        self._held = taken
        return taken

    async def _renew_or_steal(self, db, clock: _Clock) -> bool:
        """One statement: renew if ours, take over if expired, else nothing"""
        now, expiry = clock.now, clock.plus(self.ttl)
        result = await db.execute(
            text(
                f"UPDATE collector_leases SET "
                f"  owner = :owner, "
                f"  renewed_at = {now}, "
                f"  expires_at = {expiry}, "
                f"  acquired_at = CASE WHEN owner = :owner "
                f"                     THEN acquired_at ELSE {now} END, "
                f"  takeovers = CASE WHEN owner = :owner THEN takeovers "
                f"                   ELSE COALESCE(takeovers, 0) + 1 END "
                f"WHERE name = :name "
                f"  AND (owner = :owner OR expires_at <= {now})"
            ),
            {"name": self.name, "owner": self.owner, **clock.params(self.ttl)}
        )
        return (result.rowcount or 0) > 0

    async def _insert(self, db, clock: _Clock) -> bool:
        """Create the row, or do nothing if another process got there first"""
        now, expiry = clock.now, clock.plus(self.ttl)
        result = await db.execute(
            text(
                f"INSERT INTO collector_leases "
                f"  (name, owner, acquired_at, renewed_at, expires_at, takeovers) "
                f"VALUES (:name, :owner, {now}, {now}, {expiry}, 0) "
                f"ON CONFLICT (name) DO NOTHING"
            ),
            {"name": self.name, "owner": self.owner, **clock.params(self.ttl)}
        )
        return (result.rowcount or 0) > 0

    async def release(self) -> None:
        """
        Expire the lease on the way out, so failover is immediate.

        Without this, a collector restart would leave the fleet-wide phase
        unowned for up to one TTL even though another shard is ready to do it.
        """
        if not self._held:
            return
        self._held = False
        clock = _clock()
        try:
            async with self._session_factory() as db:
                await db.execute(
                    text(
                        f"UPDATE collector_leases SET expires_at = {clock.now} "
                        f"WHERE name = :name AND owner = :owner"
                    ),
                    {"name": self.name, "owner": self.owner,
                     **clock.params(0)}
                )
                await db.commit()
        except SQLAlchemyError as e:
            logger.debug(f"Releasing lease {self.name}: {e}")


async def read_leases(
    prefix: Optional[str] = None, session_factory=None
) -> List[LeaseHolder]:
    """
    Every lease, with whether the database considers it live.

    The liveness test is evaluated in SQL against the database's clock for the
    same reason the expiry is written there - see the module docstring.
    """
    clock = _clock()
    live = literal_column(
        f"CASE WHEN collector_leases.expires_at > {clock.now} THEN 1 ELSE 0 END"
    ).label("live")
    statement = select(
        CollectorLease.name,
        CollectorLease.owner,
        CollectorLease.renewed_at,
        CollectorLease.expires_at,
        CollectorLease.takeovers,
        live,
    )
    if prefix:
        statement = statement.where(CollectorLease.name.like(f"{prefix}%"))

    factory = session_factory or async_session_maker
    try:
        async with factory() as db:
            rows = (await db.execute(
                statement, clock.params(0) if not clock.database_clock else {}
            )).all()
    except SQLAlchemyError as e:
        logger.error(f"Reading collector leases failed: {e}")
        return []

    return [
        LeaseHolder(
            name=row.name,
            owner=row.owner,
            renewed_at=row.renewed_at,
            expires_at=row.expires_at,
            live=bool(row.live),
            takeovers=row.takeovers or 0,
        )
        for row in rows
    ]


def member_lease(shard, ttl_seconds: float, session_factory=None) -> Lease:
    """
    This shard's heartbeat.

    Uncontended - nobody else ever claims ``member-<n>`` - so acquire() on it
    is just "I am still here", and the point of it is what the fleet-wide
    holder can then read: if ``member-2`` is missing or expired while
    COLLECTOR_SHARDS is 4, a quarter of the devices are not being polled by
    anyone. Without this that failure is invisible until those devices start
    alerting as offline, which looks like a network problem rather than a
    collector that was never started.
    """
    return Lease(
        name=f"{MEMBER_PREFIX}{shard.index}",
        owner=shard.name,
        ttl_seconds=ttl_seconds,
        session_factory=session_factory
    )


async def missing_members(shard, session_factory=None) -> List[int]:
    """
    Shard numbers of the configured count that are not reporting.

    A shard number outside the configured count (left over from a deployment
    that used to have more shards) is not reported as missing: nothing is
    expected of it. Its row stays until it is reused, which is harmless and
    tells an operator what the previous layout was.
    """
    leases = await read_leases(MEMBER_PREFIX, session_factory)
    alive = set()
    for lease in leases:
        if not lease.live:
            continue
        try:
            alive.add(int(lease.name[len(MEMBER_PREFIX):]))
        except ValueError:  # pragma: no cover - a name nothing here writes
            continue
    return [index for index in range(shard.count) if index not in alive]


class Coverage:
    """
    Is every shard of the configured count actually running?

    Checked by whichever process holds the fleet-wide lease, because it is the
    one place in a sharded deployment that looks at the fleet as a whole. It
    reports a change immediately and then only every ``every`` checks: a shard
    that is down stays down for a while, and a line a minute about it would
    bury everything else in the log without adding anything.
    """

    def __init__(self, shard, every: int = 10, session_factory=None):
        self.shard = shard
        self.every = max(1, int(every))
        self._session_factory = session_factory
        self._last: Optional[Tuple[int, ...]] = None
        self._since_report = 0

    async def check(self) -> List[int]:
        """Shard numbers not reporting. Logs when that changes."""
        if not self.shard.enabled:
            return []

        missing = await missing_members(self.shard, self._session_factory)
        fingerprint = tuple(missing)
        changed = fingerprint != self._last
        self._since_report += 1

        if changed or self._since_report >= self.every:
            self._since_report = 0
            if missing:
                share = len(missing) / self.shard.count
                logger.warning(
                    "Shard(s) %s of %d are not reporting: about %.0f%% of the "
                    "fleet is not being polled by anyone. Start them, or lower "
                    "COLLECTOR_SHARDS on every process to the number you run.",
                    ", ".join(str(index) for index in missing),
                    self.shard.count, share * 100
                )
            elif changed and self._last:
                logger.info(
                    "All %d shards are reporting again", self.shard.count
                )

        self._last = fingerprint
        return missing


def coordination_ttl(renew_interval: float) -> float:
    """
    How long a lease outlives its last renewal.

    Three renewal intervals. One would mean a cycle that ran a little long
    loses the lease it is still using; a longer TTL would leave the fleet-wide
    phase unowned for that much longer after a process dies. At a 60 second
    fast interval that is a 180 second TTL, so a crashed holder costs at most
    three missed alert evaluations, and a clean shutdown costs none because
    release() expires the lease on the way out.
    """
    return max(30.0, 3.0 * float(renew_interval))
