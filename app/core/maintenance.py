"""
The application's background work, in one loop.

  every round (15 s)   send notifications (app/core/notify.py)
  every minute         scheduled backup, scheduled health check, notice
                       finished runner jobs
  once a day           purge the audit trail past its retention, expired
                       sessions and old notification rows

It runs inside the web process (uvicorn runs one worker). With more than one
application process - several replicas behind a load balancer - the
"app-maintenance" lease (app/core/lease.py, the same mechanism the
collectors use) makes exactly one of them do it, so a backup is not
requested twice and a message is not sent twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.core import audit, auth, health, notify, runner, sysconf
from app.core.clock import utcnow
from app.core.lease import Lease
from app.db.database import async_session_maker
from app.models.admin import SystemSetting

logger = logging.getLogger(__name__)

ROUND_SECONDS = 15
LEASE_NAME = "app-maintenance"

_task: Optional[asyncio.Task] = None


async def get_state(db, key: str) -> Dict[str, Any]:
    row = await db.get(SystemSetting, f"state.{key}")
    return dict(row.value) if row is not None and isinstance(row.value, dict) else {}


async def set_state(db, key: str, value: Dict[str, Any]) -> None:
    row = await db.get(SystemSetting, f"state.{key}")
    if row is None:
        db.add(SystemSetting(key=f"state.{key}", value=value, updated_at=utcnow(), updated_by="system"))
    else:
        row.value = value
        row.updated_at = utcnow()
    await db.commit()


def last_scheduled(conf: sysconf.BackupSettings, now: datetime) -> datetime:
    """The most recent moment the schedule said a backup should run (local time)."""
    local = now.astimezone()
    hh, mm = (int(x) for x in conf.time.split(":"))
    moment = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if conf.frequency == "daily":
        if moment > local:
            moment -= timedelta(days=1)
    else:
        moment -= timedelta(days=(moment.weekday() - conf.weekday) % 7)
        if moment > local:
            moment -= timedelta(days=7)
    return moment


def next_scheduled(conf: sysconf.BackupSettings, now: datetime) -> datetime:
    step = timedelta(days=1 if conf.frequency == "daily" else 7)
    return last_scheduled(conf, now) + step


async def scheduled_backup(db) -> Optional[str]:
    conf: sysconf.BackupSettings = await sysconf.load(db, "backup")  # type: ignore[assignment]
    if not conf.enabled or not runner.available()["available"]:
        return None
    state = await get_state(db, "backup")
    now = utcnow()
    due = last_scheduled(conf, now)
    last = state.get("last_requested")
    if last and datetime.fromisoformat(last) >= due:
        return None
    if not last:
        # First start with a schedule: do not fire for a moment that passed
        # before the system existed - start counting from now
        await set_state(db, "backup", {"last_requested": now.isoformat()})
        return None
    if runner.busy():
        return None
    job_id = runner.submit("backup", by="schedule", args={"keep": conf.keep, "trigger": "scheduled"})
    await set_state(db, "backup", {"last_requested": now.isoformat(), "job": job_id})
    await audit.record("backup.scheduled", username="system", target=job_id, db=db)
    return job_id


async def run_health(db, trigger: str) -> Dict[str, Any]:
    items = await health.run_checks(db)
    summary = health.summarize(items)
    result = {"at": utcnow().isoformat(), "trigger": trigger, "items": items, **summary}
    previous = await get_state(db, "health")
    await set_state(db, "health", {**result, "notified": previous.get("notified"),
                                   "notified_at": previous.get("notified_at")})
    conf: sysconf.HealthSettings = await sysconf.load(db, "health")  # type: ignore[assignment]
    abnormal = [i for i in items if i["level"] in ("warn", "fail")]
    signature = "|".join(sorted(f"{i['name']}:{i['level']}" for i in abnormal))
    last_at = previous.get("notified_at")
    stale = not last_at or utcnow() - datetime.fromisoformat(last_at) > timedelta(hours=24)
    if abnormal and (signature != previous.get("notified") or stale):
        notify.queue_event(db, "health_failed", {
            "count": len(abnormal),
            "items": [f"{i['group']}／{i['name']}：{i['detail']}" for i in abnormal],
        }, severity="critical" if summary["counts"]["fail"] else "warning")
        state = await get_state(db, "health")
        state.update(notified=signature, notified_at=utcnow().isoformat())
        await set_state(db, "health", state)
    elif not abnormal and previous.get("notified"):
        state = await get_state(db, "health")
        state.update(notified="", notified_at=None)
        await set_state(db, "health", state)
    elif conf.notify_ok and not abnormal and trigger == "scheduled":
        notify.queue_event(db, "health_failed", {"count": 0, "items": ["全部正常"]}, severity="info")
        await db.commit()
    return result


async def scheduled_health(db) -> None:
    conf: sysconf.HealthSettings = await sysconf.load(db, "health")  # type: ignore[assignment]
    if not conf.enabled:
        return
    state = await get_state(db, "health")
    last = state.get("at")
    if last and utcnow() - datetime.fromisoformat(last) < timedelta(hours=conf.interval_hours):
        return
    await run_health(db, "scheduled")


async def watch_jobs(db) -> None:
    """A finished job is announced once (job_finished)."""
    state = await get_state(db, "jobs")
    seen = set(state.get("seen", []))
    first = "seen" not in state
    changed = False
    for j in runner.jobs(limit=30):
        if j.get("state") not in ("ok", "failed") or j["id"] in seen:
            continue
        seen.add(j["id"])
        changed = True
        if first:
            continue  # do not announce history on the first look
        notify.queue_event(db, "job_finished", {
            "job": j.get("kind"), "ok": j.get("state") == "ok", "id": j["id"],
            "summary": j.get("summary") or "", "by": j.get("by"),
        }, severity="info" if j.get("state") == "ok" else "warning")
    if changed or first:
        await set_state(db, "jobs", {"seen": sorted(seen)[-300:]})


async def daily(db) -> None:
    state = await get_state(db, "daily")
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") == today:
        return
    conf: sysconf.AuditSettings = await sysconf.load(db, "audit")  # type: ignore[assignment]
    await audit.purge(db, conf.retention_days)
    await auth.purge_expired_sessions(db)
    await notify.purge_old(db)
    await set_state(db, "daily", {"date": today})


async def round_once(minute_work: bool) -> None:
    async with async_session_maker() as db:
        try:
            await notify.dispatch_once(db)
        except Exception:
            logger.exception("Notification dispatch failed")
            await db.rollback()
        if not minute_work:
            return
        for name, step in (("backup schedule", scheduled_backup), ("health schedule", scheduled_health),
                           ("job watch", watch_jobs), ("daily purge", daily)):
            try:
                await step(db)
            except Exception:
                logger.exception("Maintenance step %s failed", name)
                await db.rollback()


async def _loop() -> None:
    owner = f"{socket.gethostname()}:{os.getpid()}"
    lease = Lease(LEASE_NAME, owner, ttl_seconds=ROUND_SECONDS * 4)
    last_minute = 0.0
    while True:
        try:
            if await lease.acquire():
                now = time.monotonic()
                minute = now - last_minute >= 60
                if minute:
                    last_minute = now
                await round_once(minute)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Maintenance round failed")
        await asyncio.sleep(ROUND_SECONDS)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="maintenance")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
