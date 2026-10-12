"""
/api/v1/collector/status: the collectors' own report, read back.

What matters here is that the numbers come from the collector processes and
not from the API's environment. In docker-compose the ALERT_* variables are
set on the collector service only, so an API that read its own settings would
display defaults while the collector ran with something else - and an
operator would then see "confirm cycles: 1" for a fleet that is actually
waiting three cycles before alerting.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from app.core.lease import FLEET_LEASE, Lease, member_lease
from app.core.sharding import Shard
from app.db.database import async_session_maker, init_db
from app.models.collector import CollectorLease
from app.models.device import Device
from app.models.link import RawLink


def run(coro):
    return asyncio.run(coro)


async def _clean():
    await init_db()
    async with async_session_maker() as db:
        await db.execute(delete(CollectorLease))
        await db.execute(delete(RawLink))
        await db.execute(delete(Device))
        await db.commit()


async def _get():
    import app.main as main_module

    async with AsyncClient(
        transport=ASGITransport(app=main_module.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/collector/status")
        assert response.status_code == 200, response.text
        return response.json()


def _warning(body, code):
    """The one warning with this code, or None - warnings are objects so the
    UI can translate them, not sentences to match on."""
    found = [w for w in body["warnings"] if w["code"] == code]
    assert len(found) == 1, f"{code} not in {body['warnings']}"
    return found[0]


def _status(shard=0, shards=1, cycles=1, transitions=0, window=600.0,
            requires_poll=False, pending=(), flapping=(), traps=None):
    return {
        "shard": {"index": shard, "count": shards},
        "intervals": {"inventory": 300, "fast": 60},
        "damping": {
            "offline_confirm_cycles": cycles,
            "link_flap_transitions": transitions,
            "link_flap_window": window,
            "trap_requires_poll": requires_poll,
            "pending_offline": len(pending),
            "pending_offline_devices": [
                {"id": device_id, "failures": failures}
                for device_id, failures in pending
            ],
            "flapping_links": len(flapping),
            "flapping_link_ids": [
                {"id": link_id, "transitions": transitions}
                for link_id, transitions in flapping
            ],
        },
        "traps": traps,
    }


async def _heartbeat(shard=0, shards=1, **kwargs):
    """Write one shard's heartbeat through the real lease path."""
    lease = member_lease(
        Shard(shards, shard), 60,
        status=lambda: _status(shard=shard, shards=shards, **kwargs)
    )
    assert await lease.acquire()
    return lease


# -- nothing running -------------------------------------------------------

def test_no_collector_reports_nothing_rather_than_guessing():
    async def check():
        await _clean()
        return await _get()

    body = run(check())
    assert body["reporting"] is False
    assert body["processes"] == []
    assert body["sharded"] is False
    assert body["traps"] is None
    assert body["warnings"] == []


def test_an_expired_heartbeat_is_not_a_running_collector():
    async def check():
        await _clean()
        async with async_session_maker() as db:
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            db.add(CollectorLease(
                name="member-0", owner="shard-0-of-1",
                acquired_at=past, renewed_at=past, expires_at=past,
                takeovers=0, status=_status(),
            ))
            await db.commit()
        return await _get()

    body = run(check())
    assert body["reporting"] is False
    # The row is still shown - "a collector was here and stopped" is useful -
    # but it is not live and nothing is aggregated from it.
    assert len(body["processes"]) == 1
    assert body["processes"][0]["live"] is False
    assert body["damping"]["pending_offline"] == 0


# -- one collector ---------------------------------------------------------

def test_a_single_collector_reports_its_own_settings():
    async def check():
        await _clean()
        await _heartbeat(cycles=3, transitions=4, window=900.0,
                         requires_poll=True)
        return await _get()

    body = run(check())
    assert body["reporting"] is True
    assert body["sharded"] is False
    assert body["shards_configured"] == 1
    assert body["damping"]["offline_confirm_cycles"] == 3
    assert body["damping"]["link_flap_transitions"] == 4
    assert body["damping"]["link_flap_window"] == 900.0
    assert body["damping"]["trap_requires_poll"] is True
    assert body["processes"][0]["intervals"] == {"inventory": 300, "fast": 60}
    assert body["processes"][0]["age_seconds"] is not None
    assert body["warnings"] == []


def test_devices_mid_confirmation_are_named_not_just_counted():
    """
    The whole point of showing this is to tell damping from a blind spot, so
    an id is not enough - it has to say which switch is quiet.
    """
    async def check():
        await _clean()
        async with async_session_maker() as db:
            db.add(Device(hostname="sw-quiet", ip_address="10.0.0.9",
                          snmp_community="public"))
            db.add(Device(hostname="sw-fine", ip_address="10.0.0.10",
                          snmp_community="public"))
            await db.commit()
            quiet = (await db.execute(
                Device.__table__.select().where(Device.hostname == "sw-quiet")
            )).first()
        await _heartbeat(cycles=3, pending=[(quiet.id, 2)])
        return await _get(), quiet.id

    body, quiet_id = run(check())
    assert body["damping"]["pending_offline"] == 1
    # The progress is the point: 2 of 3 failures says damping is working,
    # where a bare id says only "something is up with a device".
    assert body["pending_offline_devices"] == [
        {"id": quiet_id, "hostname": "sw-quiet", "ip_address": "10.0.0.9",
         "failures": 2}
    ]


def test_flapping_cables_are_resolved_to_a_port():
    async def check():
        await _clean()
        async with async_session_maker() as db:
            device = Device(hostname="sw-a", ip_address="10.0.0.1",
                            snmp_community="public")
            db.add(device)
            await db.commit()
            await db.refresh(device)
            link = RawLink(
                local_device_id=device.id, local_port="GigabitEthernet1/0/3",
                local_port_index=3, remote_hostname="sw-b",
                remote_port="GigabitEthernet1/0/3", protocol="lldp",
            )
            db.add(link)
            await db.commit()
            await db.refresh(link)
            link_id, device_id = link.id, device.id
        await _heartbeat(transitions=3, flapping=[(link_id, 5)])
        return await _get(), link_id, device_id

    body, link_id, device_id = run(check())
    assert body["damping"]["flapping_links"] == 1
    assert body["flapping_links"] == [{
        "id": link_id, "device_id": device_id, "hostname": "sw-a",
        "local_port": "GigabitEthernet1/0/3", "remote_hostname": "sw-b",
        "remote_port": "GigabitEthernet1/0/3", "transitions": 5,
    }]


# -- several shards --------------------------------------------------------

def test_a_missing_shard_is_reported_as_a_warning():
    async def check():
        await _clean()
        for index in (0, 1, 3):
            await _heartbeat(shard=index, shards=4)
        return await _get()

    body = run(check())
    assert body["sharded"] is True
    assert body["shards_configured"] == 4
    assert body["shards_reporting"] == [0, 1, 3]
    assert body["shards_missing"] == [2]
    warning = _warning(body, "shards_missing")
    # The code is what the UI translates; the message is the fallback for a
    # client that does not know the code.
    assert warning["params"] == {"shards": "2", "configured": 4, "percent": 25}
    assert "Shard(s) 2 of 4" in warning["message"]
    assert "25%" in warning["message"]


def test_shards_that_disagree_about_the_count_are_called_out():
    """The failure is silent otherwise: some devices polled twice, others
    by nobody."""
    async def check():
        await _clean()
        await _heartbeat(shard=0, shards=2)
        await _heartbeat(shard=1, shards=4)
        return await _get()

    body = run(check())
    assert _warning(body, "shards_disagree")


def test_shards_that_damp_differently_are_called_out():
    async def check():
        await _clean()
        await _heartbeat(shard=0, shards=2, cycles=1)
        await _heartbeat(shard=1, shards=2, cycles=3)
        return await _get()

    body = run(check())
    assert body["damping"]["offline_confirm_cycles"] == [1, 3]
    assert _warning(body, "damping_disagree")["params"] == {
        "setting": "offline_confirm_cycles"
    }


def test_live_state_is_summed_across_shards():
    async def check():
        await _clean()
        await _heartbeat(shard=0, shards=2, cycles=3,
                         pending=[(1, 1), (2, 2)])
        await _heartbeat(shard=1, shards=2, cycles=3, pending=[(3, 1)])
        return await _get()

    body = run(check())
    assert body["damping"]["pending_offline"] == 3


def test_the_fleet_wide_lease_holder_is_shown():
    async def check():
        await _clean()
        await _heartbeat(shard=0, shards=2)
        lease = Lease(FLEET_LEASE, "shard-0-of-2", 60)
        assert await lease.acquire()
        return await _get()

    body = run(check())
    assert body["fleet_wide"]["owner"] == "shard-0-of-2"
    assert body["fleet_wide"]["live"] is True


# -- traps -----------------------------------------------------------------

def test_trap_counters_are_summed_and_unknown_sources_explained():
    async def check():
        await _clean()
        await _heartbeat(shard=0, shards=2, traps={
            "listening": 162, "received": 10, "applied": 8, "ignored": 2,
            "dropped": 0, "deferred": 1, "requires_poll": True,
            "known_addresses": 1200, "unknown_sources": ["10.9.9.9"],
        })
        await _heartbeat(shard=1, shards=2, traps={
            "listening": 162, "received": 5, "applied": 5, "ignored": 0,
            "dropped": 0, "deferred": 0, "requires_poll": False,
            "known_addresses": 1200, "unknown_sources": [],
        })
        return await _get()

    body = run(check())
    assert body["traps"]["receivers"] == 2
    assert body["traps"]["received"] == 15
    assert body["traps"]["deferred"] == 1
    assert body["traps"]["requires_poll"] is True
    assert body["traps"]["unknown_sources"] == ["10.9.9.9"]
    # Not summed: each receiver matches against the whole fleet, so two
    # receivers reporting 1200 means 1200 addresses, not 2400.
    assert body["traps"]["known_addresses"] == 1200
    warning = _warning(body, "trap_unknown_sources")
    assert warning["params"] == {"count": 1, "addresses": "10.9.9.9"}


def test_no_trap_receiver_means_no_trap_section():
    async def check():
        await _clean()
        await _heartbeat()
        return await _get()

    assert run(check())["traps"] is None
