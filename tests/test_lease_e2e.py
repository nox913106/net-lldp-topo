"""
The coordination table, against a real database.

Two processes polling the same fleet is not a crash - it is double the load
and double the alerts, and nothing in a log says so. The lease is what makes
"exactly one process does the fleet-wide work" true, and all of it is SQL:
the expiry is compared against the database's clock, a takeover is one
statement so two processes cannot both win it, and the status column has to
keep a previous value when a process reports nothing.

None of that can be tested without a database, and one of the bugs below
(``None`` arriving as the JSON string "null", which COALESCE then treats as a
value) was invisible on SQLite's default JSON handling. This runs on both -
CI runs the whole suite twice, once against PostgreSQL.
"""
import asyncio

import pytest

from app.core.lease import (
    FLEET_LEASE, Coverage, Lease, coordination_ttl, member_lease,
    missing_members, read_leases,
)
from app.core.sharding import Shard


def run(coro):
    return asyncio.run(coro)


def holder_of(leases, name):
    return next((lease for lease in leases if lease.name == name), None)


# -- who does the fleet-wide work ------------------------------------------


def test_only_one_of_two_processes_holds_the_fleet_lease(clean_db):
    async def check():
        one = Lease(FLEET_LEASE, "shard-0", ttl_seconds=600)
        two = Lease(FLEET_LEASE, "shard-1", ttl_seconds=600)
        held = [await one.acquire(), await two.acquire()]
        # And the loser stays the loser for as long as the winner renews
        held.append(await two.acquire())
        held.append(await one.acquire())
        return held, await read_leases()

    held, leases = run(check())
    assert held == [True, False, False, True]

    lease = holder_of(leases, FLEET_LEASE)
    assert lease is not None
    assert (lease.owner, lease.live, lease.takeovers) == ("shard-0", True, 0)


def test_an_expired_holder_is_taken_over_exactly_once(clean_db):
    """
    The holder dies: somebody else has to pick the work up, and the count of
    how often that happened has to be visible - a lease changing hands every
    cycle means two processes are fighting, which is worth seeing.
    """
    async def check():
        dead = Lease(FLEET_LEASE, "shard-0", ttl_seconds=1)
        assert await dead.acquire() is True

        # Nothing renews it, so it expires. The expiry is the database's
        # clock, not this process's, which is the whole reason it is in SQL.
        await asyncio.sleep(1.3)

        waiting = Lease(FLEET_LEASE, "shard-1", ttl_seconds=600)
        took_over = await waiting.acquire()
        # A second process arriving later must not add another takeover: the
        # lease is live now and it simply loses.
        late = Lease(FLEET_LEASE, "shard-2", ttl_seconds=600)
        also = await late.acquire()
        return took_over, also, await read_leases()

    took_over, also, leases = run(check())
    assert took_over is True
    assert also is False

    lease = holder_of(leases, FLEET_LEASE)
    assert (lease.owner, lease.live, lease.takeovers) == ("shard-1", True, 1)


def test_releasing_hands_it_over_without_waiting_for_the_ttl(clean_db):
    """A clean shutdown should not leave the fleet-wide work idle for a TTL."""
    async def check():
        one = Lease(FLEET_LEASE, "shard-0", ttl_seconds=600)
        two = Lease(FLEET_LEASE, "shard-1", ttl_seconds=600)
        assert await one.acquire() is True
        assert await two.acquire() is False

        await one.release()
        return await two.acquire(), one.held

    taken, still_held = run(check())
    assert taken is True
    assert still_held is False


# -- what each shard reports about itself ----------------------------------


def test_a_shard_heartbeat_carries_the_status_it_reports(clean_db):
    """
    The API runs in another process, so everything it shows about a collector
    comes through this column (docs/SDD-API.md). It is JSON in and JSON out,
    including the nested parts, which is where a round trip can quietly lose
    a key.
    """
    reported = {
        "shard": {"index": 0, "count": 1},
        "intervals": {"inventory": 300, "fast": 60, "addresses": 3600},
        "damping": {"pending_offline": 1, "flapping_links": 0},
        "traps": {"received": 12, "known_addresses": 7},
    }

    async def check():
        beat = member_lease(Shard(1, 0), 600, status=lambda: reported)
        assert await beat.acquire() is True
        return await read_leases(prefix="member-")

    leases = run(check())
    assert len(leases) == 1
    assert leases[0].status == reported


def test_reporting_nothing_keeps_the_last_status(clean_db):
    """
    A collector too old to report, or one whose status callable failed, must
    not blank what is there - the row is also the heartbeat, so it is written
    every cycle either way.

    This is the case that needed JSON(none_as_null=True): by default a Python
    None is handed to the database as the JSON value "null", which COALESCE
    sees as present and happily stores over the real status.
    """
    async def check():
        reporting = member_lease(Shard(1, 0), 600,
                                 status=lambda: {"damping": {"x": 1}})
        assert await reporting.acquire() is True

        silent = member_lease(Shard(1, 0), 600)      # no status callable
        assert await silent.acquire() is True
        return await read_leases(prefix="member-")

    leases = run(check())
    assert leases[0].status == {"damping": {"x": 1}}


def test_a_status_callable_that_raises_costs_the_status_not_the_lease(clean_db):
    def boom():
        raise RuntimeError("the status is not worth the lease")

    async def check():
        beat = member_lease(Shard(1, 0), 600, status=boom)
        held = await beat.acquire()
        return held, await read_leases(prefix="member-")

    held, leases = run(check())
    assert held is True              # still beating
    assert leases[0].status is None


# -- a shard that nobody is running ----------------------------------------


def test_a_missing_shard_is_named(clean_db):
    """
    The failure this exists for: COLLECTOR_SHARDS=4 with three processes
    running polls three quarters of the fleet and says nothing. The quarter
    nobody polls shows up here as a number, which the API turns into the
    shards_missing warning.
    """
    async def check():
        for index in (0, 1, 3):
            assert await member_lease(Shard(4, index), 600).acquire() is True
        return (
            await missing_members(Shard(4, 0)),
            await Coverage(Shard(4, 0), every=1).check(),
        )

    missing, coverage = run(check())
    assert missing == [2]
    assert coverage == [2]


def test_nothing_is_missing_when_every_shard_reports(clean_db):
    async def check():
        for index in range(4):
            assert await member_lease(Shard(4, index), 600).acquire() is True
        return await missing_members(Shard(4, 0))

    assert run(check()) == []


def test_an_expired_heartbeat_counts_as_missing(clean_db):
    """
    A shard that was started and then died is exactly as bad as one that was
    never started, and the row it left behind must not make it look present.
    """
    async def check():
        assert await member_lease(Shard(2, 0), 600).acquire() is True
        assert await member_lease(Shard(2, 1), 1).acquire() is True
        await asyncio.sleep(1.3)
        return await missing_members(Shard(2, 0))

    assert run(check()) == [1]


# -- the TTL itself --------------------------------------------------------


@pytest.mark.parametrize("interval,expected", [
    (60, 180.0),
    (300, 900.0),
])
def test_the_ttl_is_derived_from_the_loop_that_renews_it(interval, expected):
    """
    Three renewal intervals, so one slow cycle or one lost statement does not
    hand the fleet-wide work to another process - and a takeover still
    happens within a few minutes of a real death.
    """
    assert coordination_ttl(interval) == expected
