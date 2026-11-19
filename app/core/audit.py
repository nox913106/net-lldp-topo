"""
The audit trail.

Every request that changes something is recorded by the security middleware
(app/core/security_middleware.py) - not by each endpoint remembering to - so
a new endpoint is audited the day it is written. What the endpoint cannot
know, the middleware does: who, from where, and whether it worked.

Actions are stable keys ("device.update"), translated in the UI. A route that
has no key here is still recorded, under "METHOD /path/template", so nothing
is ever silently left out; tests/test_admin.py fails when a mutating route
has no key, so the raw form is a fallback, not the norm.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Dict, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.db.database import async_session_maker
from app.models.admin import AuditLog

logger = logging.getLogger(__name__)

# (method, route template) -> action key
ACTIONS: Dict[tuple, str] = {
    ("POST", "/api/v1/devices"): "device.create",
    ("PUT", "/api/v1/devices/{device_id}"): "device.update",
    ("DELETE", "/api/v1/devices/{device_id}"): "device.delete",
    ("POST", "/api/v1/groups"): "group.create",
    ("PUT", "/api/v1/groups/{group_id}"): "group.update",
    ("DELETE", "/api/v1/groups/{group_id}"): "group.delete",
    ("POST", "/api/v1/groups/{group_id}/devices"): "group.add_devices",
    ("DELETE", "/api/v1/groups/{group_id}/devices"): "group.remove_devices",
    ("POST", "/api/v1/profiles"): "profile.create",
    ("PUT", "/api/v1/profiles/{profile_id}"): "profile.update",
    ("DELETE", "/api/v1/profiles/{profile_id}"): "profile.delete",
    ("POST", "/api/v1/alerts/{alert_id}/acknowledge"): "alert.acknowledge",
    ("POST", "/api/v1/alerts/{alert_id}/unsuppress"): "alert.unsuppress",
    ("PUT", "/api/v1/alerts/{alert_id}/history/{history_id}"): "alert.history_edit",
    ("DELETE", "/api/v1/alerts/{alert_id}/history/{history_id}"): "alert.history_delete",
    ("POST", "/api/v1/settings"): "settings.snmp",
    ("POST", "/api/v1/settings/discovery/trigger"): "discovery.trigger",
    ("POST", "/api/v1/discovery/manual"): "discovery.manual",
    ("POST", "/api/v1/snmp/test"): "snmp.test",
    ("POST", "/api/v1/snmp/lldp"): "snmp.lldp_test",
    ("POST", "/api/v1/topology/exclude-rules"): "topology.exclude_add",
    ("DELETE", "/api/v1/topology/exclude-rules/{rule_id}"): "topology.exclude_delete",
    # Accounts and sessions
    ("POST", "/api/v1/auth/password"): "auth.password_change",
    ("POST", "/api/v1/admin/users"): "user.create",
    ("PUT", "/api/v1/admin/users/{user_id}"): "user.update",
    ("DELETE", "/api/v1/admin/users/{user_id}"): "user.delete",
    ("POST", "/api/v1/admin/users/{user_id}/password"): "user.password_reset",
    ("POST", "/api/v1/admin/users/{user_id}/logout"): "user.sessions_revoke",
    ("PUT", "/api/v1/admin/settings/auth"): "settings.auth",
    ("PUT", "/api/v1/admin/settings/ldap"): "settings.ldap",
    ("POST", "/api/v1/admin/ldap/test"): "ldap.test",
    # Notifications
    ("PUT", "/api/v1/admin/notifications"): "settings.notifications",
    ("POST", "/api/v1/admin/notifications/test"): "notification.test",
    ("POST", "/api/v1/admin/notifications/deliveries/{delivery_id}/retry"): "notification.retry",
    # Audit itself
    ("PUT", "/api/v1/admin/settings/audit"): "settings.audit",
    ("GET", "/api/v1/admin/audit/export"): "audit.export",
    # Operations
    ("PUT", "/api/v1/admin/settings/backup"): "settings.backup",
    ("PUT", "/api/v1/admin/settings/health"): "settings.health",
    ("POST", "/api/v1/admin/backups"): "backup.create",
    ("DELETE", "/api/v1/admin/backups/{name}"): "backup.delete",
    ("POST", "/api/v1/admin/backups/{name}/restore"): "backup.restore",
    ("POST", "/api/v1/admin/backups/{name}/download"): "backup.download",
    ("POST", "/api/v1/admin/health/run"): "health.run",
    ("POST", "/api/v1/admin/doctor"): "health.doctor",
    ("POST", "/api/v1/admin/tls"): "tls.install",
    ("POST", "/api/v1/admin/upgrade/check"): "upgrade.check",
    ("POST", "/api/v1/admin/upgrade"): "upgrade.run",
}

# GETs that are themselves worth recording (reading is not normally audited)
AUDITED_GETS = {k[1] for k in ACTIONS if k[0] == "GET"}

# A field whose name says it holds a secret is never written to the log
_SECRET_KEY = re.compile(r"password|secret|community|token|passphrase|key_pem|private", re.I)
_MAX_DETAIL = 4000


def mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if _SECRET_KEY.search(str(k)) and v not in (None, "") else mask(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


def body_detail(raw: bytes, content_type: str) -> Optional[Any]:
    if not raw:
        return None
    if "json" in (content_type or ""):
        try:
            data = mask(json.loads(raw))
        except ValueError:
            return {"body": "(not JSON)"}
        text = json.dumps(data, ensure_ascii=False)
        if len(text) > _MAX_DETAIL:
            return {"body": text[:_MAX_DETAIL] + "…"}
        return data
    if "multipart" in (content_type or ""):
        return {"body": f"(upload, {len(raw)} bytes)"}
    return {"body": f"({content_type or 'body'}, {len(raw)} bytes)"}


def target_of(path_params: Dict[str, Any], detail: Any) -> str:
    parts = [f"{k}={v}" for k, v in path_params.items()]
    if isinstance(detail, dict):
        for k in ("hostname", "name", "username", "ip_address", "target"):
            v = detail.get(k)
            if isinstance(v, str) and v:
                parts.append(v)
                break
    return " ".join(parts)[:300]


async def record(
    action: str, *, user_id: Optional[int] = None, username: Optional[str] = None,
    target: str = "", method: str = "", path: str = "", status: Optional[int] = None,
    ip: str = "", detail: Any = None, db: Optional[AsyncSession] = None,
) -> None:
    """Write one row. Never raises: losing an audit row is logged, not fatal."""
    row = AuditLog(
        at=utcnow(), user_id=user_id, username=(username or "")[:128] or None,
        action=action[:64], target=(target or "")[:300], method=method[:8],
        path=path[:300], status=status, ip=(ip or "")[:64], detail=detail,
    )
    try:
        if db is not None:
            db.add(row)
            await db.commit()
            return
        async with async_session_maker() as session:
            session.add(row)
            await session.commit()
    except Exception:
        logger.exception("Could not write audit row %s %s", action, target)


async def purge(db: AsyncSession, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    count = (await db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.at < cutoff))).scalar_one()
    if count:
        await db.execute(delete(AuditLog).where(AuditLog.at < cutoff))
        await db.commit()
        await record("audit.purge", username="system", target=f"{count}",
                     detail={"deleted": count, "older_than_days": retention_days}, db=db)
    return int(count)
