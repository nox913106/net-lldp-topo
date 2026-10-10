"""
Collector status: what the collector processes are doing right now.

Everything here used to be visible only in a collector's log, which is the
wrong place for it. Three things in particular:

  * **A missing shard.** With ``COLLECTOR_SHARDS=4`` and three processes
    started, a quarter of the fleet is polled by nobody and the only symptom
    is those devices slowly alerting as offline - which looks like a network
    fault rather than a collector that was never started.
  * **Devices mid-confirmation.** With ``ALERT_OFFLINE_CONFIRM_CYCLES``
    greater than 1, a device that stopped answering is deliberately not
    alerting yet. Without somewhere to see that, the damping is
    indistinguishable from the system failing to notice.
  * **Cables being held as flapping**, and traps waiting for a poll to
    corroborate them.

The API process shares no memory with the collectors, so none of this can be
read directly - and reading its *own* settings would be worse than showing
nothing, because in docker-compose the ALERT_* variables are set on the
collector service only: the API would confidently display defaults that
nothing is running with. So each collector writes what it is doing onto the
heartbeat row it renews anyway (app/collector.py ``_status``), and this reads
those rows back.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lease import FLEET_LEASE, MEMBER_PREFIX, read_leases
from app.db.database import get_db
from app.models.device import Device
from app.models.link import RawLink

router = APIRouter()


class ProcessStatus(BaseModel):
    """One collector process, as its heartbeat row describes it"""
    shard: Optional[int] = None
    shards: Optional[int] = None
    owner: str
    live: bool
    renewed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    age_seconds: Optional[float] = None
    intervals: Optional[Dict[str, Any]] = None
    damping: Optional[Dict[str, Any]] = None
    traps: Optional[Dict[str, Any]] = None


class DeviceRef(BaseModel):
    id: int
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    failures: Optional[int] = None


class LinkRef(BaseModel):
    id: int
    device_id: Optional[int] = None
    hostname: Optional[str] = None
    local_port: Optional[str] = None
    remote_hostname: Optional[str] = None
    remote_port: Optional[str] = None
    transitions: Optional[int] = None


class FleetLease(BaseModel):
    """Who is doing the once-per-interval work for the whole fleet"""
    owner: str
    live: bool
    renewed_at: Optional[datetime] = None
    takeovers: int = 0


class Warning(BaseModel):
    """
    Something an operator should act on.

    Carries a ``code`` as well as the sentence, because the UI is translated
    and the API is not: the page looks the code up in its own language and
    falls back to ``message`` for a code it does not know. ``params`` are the
    numbers to substitute, so a translation does not have to parse them back
    out of the English.
    """
    code: str
    message: str
    params: Dict[str, Any] = {}


class CollectorStatus(BaseModel):
    reporting: bool
    sharded: bool
    shards_configured: Optional[int] = None
    shards_reporting: List[int] = []
    shards_missing: List[int] = []
    processes: List[ProcessStatus] = []
    fleet_wide: Optional[FleetLease] = None
    damping: Dict[str, Any] = {}
    traps: Optional[Dict[str, Any]] = None
    pending_offline_devices: List[DeviceRef] = []
    flapping_links: List[LinkRef] = []
    warnings: List[Warning] = []


def _age(renewed_at: Optional[datetime]) -> Optional[float]:
    """Seconds since this row was last renewed, by this process's clock.

    Approximate on purpose: the authoritative liveness answer is ``live``,
    which the database evaluates against its own clock (app/core/lease.py).
    This is only here so the UI can say "12 s ago" rather than print a
    timestamp.
    """
    if renewed_at is None:
        return None
    now = datetime.now(timezone.utc)
    if renewed_at.tzinfo is None:
        renewed_at = renewed_at.replace(tzinfo=timezone.utc)
    return max(0.0, round((now - renewed_at).total_seconds(), 1))


def _shard_index(name: str) -> Optional[int]:
    try:
        return int(name[len(MEMBER_PREFIX):])
    except (TypeError, ValueError):
        return None


@router.get("/status", response_model=CollectorStatus)
async def collector_status(db: AsyncSession = Depends(get_db)):
    """
    The collectors' own report, plus the ids in it resolved to names.

    ``reporting: false`` means no collector has written a heartbeat - either
    none is running, or it predates this (then it polls fine but says
    nothing about itself).
    """
    leases = await read_leases()
    members = [
        lease for lease in leases if lease.name.startswith(MEMBER_PREFIX)
    ]
    fleet = next((l for l in leases if l.name == FLEET_LEASE), None)

    processes: List[ProcessStatus] = []
    for lease in sorted(members, key=lambda l: _shard_index(l.name) or 0):
        status = lease.status or {}
        shard = status.get("shard") or {}
        processes.append(ProcessStatus(
            shard=shard.get("index", _shard_index(lease.name)),
            shards=shard.get("count"),
            owner=lease.owner,
            live=lease.live,
            renewed_at=lease.renewed_at,
            expires_at=lease.expires_at,
            age_seconds=_age(lease.renewed_at),
            intervals=status.get("intervals"),
            damping=status.get("damping"),
            traps=status.get("traps"),
        ))

    live = [p for p in processes if p.live]
    # The configured count comes from the processes themselves, so a stale
    # row left over from a larger deployment cannot inflate it.
    configured = max((p.shards or 1 for p in live), default=None)
    reporting_indexes = sorted({p.shard for p in live if p.shard is not None})
    missing = (
        [i for i in range(configured) if i not in reporting_indexes]
        if configured else []
    )

    warnings: List[Warning] = []
    if configured and missing:
        share = len(missing) / configured
        warnings.append(Warning(
            code="shards_missing",
            message=(
                f"Shard(s) {', '.join(str(i) for i in missing)} of "
                f"{configured} are not reporting: about {share * 100:.0f}% of "
                f"the fleet is not being polled by anyone."
            ),
            params={
                "shards": ", ".join(str(i) for i in missing),
                "configured": configured,
                "percent": round(share * 100),
            },
        ))
    if live and len({p.shards or 1 for p in live}) > 1:
        warnings.append(Warning(
            code="shards_disagree",
            message=(
                "The running collectors disagree about COLLECTOR_SHARDS, so "
                "some devices are polled twice and others not at all."
            ),
        ))

    damping, traps = _combine(live, warnings)
    pending = _progress(live, "pending_offline_devices")
    flapping = _progress(live, "flapping_link_ids")

    return CollectorStatus(
        reporting=bool(live),
        sharded=bool(configured and configured > 1),
        shards_configured=configured,
        shards_reporting=reporting_indexes,
        shards_missing=missing,
        processes=processes,
        fleet_wide=FleetLease(
            owner=fleet.owner, live=fleet.live,
            renewed_at=fleet.renewed_at, takeovers=fleet.takeovers,
        ) if fleet else None,
        damping=damping,
        traps=traps,
        pending_offline_devices=await _devices(db, pending),
        flapping_links=await _links(db, flapping),
        warnings=warnings,
    )


# -- combining what several processes reported ----------------------------

_SETTINGS = (
    "offline_confirm_cycles", "link_flap_transitions",
    "link_flap_window", "trap_requires_poll",
)


def _combine(live: List[ProcessStatus], warnings: List[Warning]):
    """
    The fleet's damping and trap numbers.

    Settings are per process, and with sharding they are supposed to be the
    same everywhere. They do not have to be - the state is per process, so
    nothing breaks - but it means two devices in the same fleet are judged
    differently, which is worth saying out loud rather than averaging away.
    """
    damping: Dict[str, Any] = {}
    for key in _SETTINGS:
        values = {
            p.damping.get(key) for p in live
            if p.damping is not None and key in p.damping
        }
        if not values:
            continue
        if len(values) == 1:
            damping[key] = values.pop()
        else:
            damping[key] = sorted(values, key=str)
            warnings.append(Warning(
                code="damping_disagree",
                message=(
                    f"The running collectors disagree about {key}, so devices "
                    f"are judged differently depending on which shard polls "
                    f"them."
                ),
                params={"setting": key},
            ))

    for key in ("pending_offline", "flapping_links"):
        damping[key] = sum(
            int((p.damping or {}).get(key) or 0) for p in live
        )

    reported = [p.traps for p in live if p.traps]
    if not reported:
        return damping, None

    traps: Dict[str, Any] = {"receivers": len(reported)}
    for key in ("received", "applied", "ignored", "dropped", "deferred"):
        traps[key] = sum(int(t.get(key) or 0) for t in reported)
    unknown = sorted({
        source for t in reported for source in (t.get("unknown_sources") or [])
    })
    traps["unknown_sources"] = unknown[:20]
    traps["requires_poll"] = any(t.get("requires_poll") for t in reported)
    if unknown:
        warnings.append(Warning(
            code="trap_unknown_sources",
            message=(
                f"Traps are arriving from {len(unknown)} address(es) that are "
                f"not managed devices: {', '.join(unknown[:5])}. A device "
                f"usually sends traps from the egress interface rather than "
                f"from the address it is managed on - see "
                f"docs/CONFIGURATION.md section 5.3."
            ),
            params={"count": len(unknown), "addresses": ", ".join(unknown[:5])},
        ))
    return damping, traps


def _progress(live: List[ProcessStatus], key: str) -> Dict[int, Optional[int]]:
    """
    ``{id: how far}`` from every reporting process.

    The collectors send ``[{"id": .., "failures"/"transitions": ..}]``; an
    entry without a usable id is dropped rather than failing the whole
    response, because this is a status page and a collector that reports
    something odd should not take it down.
    """
    found: Dict[int, Optional[int]] = {}
    for process in live:
        for entry in (process.damping or {}).get(key) or []:
            if isinstance(entry, dict):
                raw, progress = entry.get("id"), entry.get(
                    "failures", entry.get("transitions")
                )
            else:
                raw, progress = entry, None
            try:
                found[int(raw)] = (
                    int(progress) if progress is not None else None
                )
            except (TypeError, ValueError):
                continue
    return found


# -- resolving ids to something readable ----------------------------------

async def _devices(db, pending: Dict[int, Optional[int]]) -> List[DeviceRef]:
    """
    A device id is not something anyone can act on, so resolve the names.

    A device that has since been deleted simply does not come back from this
    query; its id is dropped rather than shown as a number.
    """
    if not pending:
        return []
    rows = (await db.execute(
        select(Device.id, Device.hostname, Device.ip_address)
        .where(Device.id.in_(list(pending)))
        .order_by(Device.hostname)
    )).all()
    return [
        DeviceRef(
            id=row.id, hostname=row.hostname, ip_address=row.ip_address,
            failures=pending.get(row.id),
        )
        for row in rows
    ]


async def _links(db, flapping: Dict[int, Optional[int]]) -> List[LinkRef]:
    """The same for cables: which port on which switch, not a row id."""
    if not flapping:
        return []
    rows = (await db.execute(
        select(
            RawLink.id, RawLink.local_device_id, RawLink.local_port,
            RawLink.remote_hostname, RawLink.remote_port, Device.hostname
        )
        .join(Device, Device.id == RawLink.local_device_id, isouter=True)
        .where(RawLink.id.in_(list(flapping)))
        .order_by(Device.hostname, RawLink.local_port)
    )).all()
    return [
        LinkRef(
            id=row.id, device_id=row.local_device_id, hostname=row.hostname,
            local_port=row.local_port, remote_hostname=row.remote_hostname,
            remote_port=row.remote_port, transitions=flapping.get(row.id),
        )
        for row in rows
    ]
