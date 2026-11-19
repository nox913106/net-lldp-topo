"""
Back office: accounts, login settings, the directory, and the audit trail.

Everything under /api/v1/admin is admin-only by path (app/core/security_middleware.py).
"""
import csv
import io
import json
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current
from app.core import auth, ldap_auth, sysconf
from app.core.audit import ACTIONS
from app.core.clock import utcnow
from app.core.passwords import hash_password, password_problem
from app.db.database import get_db
from app.models.admin import AuditLog, User, UserSession

router = APIRouter()

Role = Literal["guest", "viewer", "operator", "admin"]


# -- Accounts ---------------------------------------------------------------

class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    role: Role = "viewer"
    # local: password required. ldap: pre-registers a directory account so it
    # has its role from the first login instead of starting as a guest.
    source: Literal["local", "ldap"] = "local"
    password: str = ""
    must_change_password: bool = True


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320)
    role: Optional[Role] = None
    is_active: Optional[bool] = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    must_change_password: bool = True


def _user_row(u: User, sessions: int) -> dict:
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name or "",
        "email": u.email or "", "source": u.source, "role": u.role,
        "is_active": bool(u.is_active), "must_change_password": bool(u.must_change_password),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "locked_until": u.locked_until.isoformat() if u.locked_until else None,
        "sessions": sessions,
    }


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    now = utcnow()
    counts = dict((await db.execute(
        select(UserSession.user_id, func.count()).where(UserSession.expires_at > now)
        .group_by(UserSession.user_id))).all())
    users = (await db.execute(select(User).order_by(User.source, User.username))).scalars().all()
    return [_user_row(u, counts.get(u.id, 0)) for u in users]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)):
    username = ldap_auth.clean_username(body.username)
    exists = (await db.execute(select(User).where(func.lower(User.username) == username))).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail=f"帳號 {username} 已經存在")
    user = User(username=username, display_name=body.display_name or username,
                email=body.email or None, role=body.role, source=body.source, is_active=True)
    if body.source == "local":
        problem = password_problem(body.password, username)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        user.password_hash = hash_password(body.password)
        user.password_changed_at = utcnow()
        user.must_change_password = body.must_change_password
    db.add(user)
    await db.commit()
    return _user_row(user, 0)


async def _guard_last_admin(db: AsyncSession, user: User, demoting: bool) -> None:
    """Never leave the system with no way in as an administrator."""
    if not demoting or user.role != "admin":
        return
    if await auth.count_active_admins(db) <= 1:
        raise HTTPException(status_code=400, detail="這是最後一個系統管理員，不能停用、刪除或降級")
    if user.source == "local" and await auth.count_active_admins(db, local_only=True) <= 1:
        raise HTTPException(
            status_code=400,
            detail="這是最後一個本機管理員帳號。目錄伺服器故障時要靠它登入，請先另建一個本機管理員")


@router.put("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, request: Request,
                      db: AsyncSession = Depends(get_db)):
    me = current(request)
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="找不到這個帳號")
    demoting = (body.role is not None and body.role != "admin") or body.is_active is False
    if user.id == me.id and demoting:
        raise HTTPException(status_code=400, detail="不能降級或停用自己正在使用的帳號")
    await _guard_last_admin(db, user, demoting)
    for field in ("display_name", "email", "role", "is_active"):
        value = getattr(body, field)
        if value is not None:
            setattr(user, field, value)
    if body.is_active is True:
        user.locked_until = None
        user.failed_logins = 0
    await db.commit()
    if body.is_active is False or body.role is not None:
        # A role is read on every request, so a change applies at once; a
        # disabled account must not keep the sessions it already has
        if body.is_active is False:
            await auth.end_all_sessions(db, user.id)
    return _user_row(user, 0)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    me = current(request)
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="找不到這個帳號")
    if user.id == me.id:
        raise HTTPException(status_code=400, detail="不能刪除自己正在使用的帳號")
    await _guard_last_admin(db, user, True)
    await auth.end_all_sessions(db, user.id)
    await db.delete(user)
    await db.commit()


@router.post("/users/{user_id}/password")
async def reset_password(user_id: int, body: PasswordReset, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="找不到這個帳號")
    if user.source != "local":
        raise HTTPException(status_code=400, detail="目錄帳號的密碼請在 AD 重設")
    problem = password_problem(body.password, user.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    user.password_hash = hash_password(body.password)
    user.password_changed_at = utcnow()
    user.must_change_password = body.must_change_password
    user.locked_until = None
    user.failed_logins = 0
    await db.commit()
    ended = await auth.end_all_sessions(db, user.id)
    return {"success": True, "sessions_ended": ended}


@router.post("/users/{user_id}/logout")
async def revoke_sessions(user_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    me = current(request)
    keep = me.session_id if user_id == me.id else None
    ended = await auth.end_all_sessions(db, user_id, keep_session_id=keep)
    return {"success": True, "sessions_ended": ended}


# -- Settings sections ------------------------------------------------------

@router.get("/settings/{section}")
async def get_section(section: str, db: AsyncSession = Depends(get_db)):
    if section not in sysconf.SECTIONS or section == "notifications":
        raise HTTPException(status_code=404, detail="沒有這個設定區段")
    value = await sysconf.load(db, section)
    return {"value": sysconf.masked(value), "updated": await sysconf.updated(db, section)}


@router.put("/settings/auth")
async def put_auth(body: sysconf.AuthSettings, request: Request, db: AsyncSession = Depends(get_db)):
    await sysconf.save(db, "auth", body, current(request).username)
    return {"success": True}


@router.put("/settings/ldap")
async def put_ldap(body: sysconf.LdapSettings, request: Request, db: AsyncSession = Depends(get_db)):
    if body.ca_pem.strip() and "BEGIN CERTIFICATE" not in body.ca_pem:
        raise HTTPException(status_code=400, detail="CA 憑證要是 PEM 格式（-----BEGIN CERTIFICATE-----）")
    if body.enabled and not ldap_auth.configured(body):
        raise HTTPException(status_code=400, detail="啟用之前，伺服器網址、網域、搜尋起點（Base DN）都必須填寫")
    await sysconf.save(db, "ldap", body, current(request).username)
    return {"success": True}


@router.put("/settings/audit")
async def put_audit(body: sysconf.AuditSettings, request: Request, db: AsyncSession = Depends(get_db)):
    await sysconf.save(db, "audit", body, current(request).username)
    return {"success": True}


class LdapTest(BaseModel):
    settings: sysconf.LdapSettings
    username: str = ""
    password: str = ""


@router.post("/ldap/test")
async def ldap_test(body: LdapTest):
    """Tests the settings *as sent* - nothing is saved, no account created."""
    steps = await ldap_auth.test(body.settings, body.username, body.password)
    return {"steps": [{"ok": ok, "step": name, "detail": detail} for ok, name, detail in steps]}


# -- Audit trail ------------------------------------------------------------

def _range(date_from: Optional[date], date_to: Optional[date], tz_offset: int):
    """[start, end) in UTC for whole local days. tz_offset: minutes east of UTC."""
    tz = timezone(timedelta(minutes=tz_offset))
    today = datetime.now(tz).date()
    d1 = date_to or today
    d0 = date_from or (d1 - timedelta(days=6))
    if d0 > d1:
        d0, d1 = d1, d0
    if (d1 - d0).days > 366:
        raise HTTPException(status_code=400, detail="一次最多查 366 天")
    start = datetime.combine(d0, time.min, tz).astimezone(timezone.utc)
    end = datetime.combine(d1 + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    return start, end


def _audit_query(start, end, action, username, q):
    stmt = select(AuditLog).where(AuditLog.at >= start, AuditLog.at < end)
    if action:
        if action.endswith(".*"):
            stmt = stmt.where(AuditLog.action.like(action[:-1] + "%"))
        else:
            stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username == username)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(AuditLog.target.ilike(like), AuditLog.path.ilike(like),
                              AuditLog.ip.ilike(like)))
    return stmt


def _audit_row(r: AuditLog) -> dict:
    return {
        "id": r.id, "at": r.at.isoformat() if r.at else None, "username": r.username,
        "action": r.action, "target": r.target, "method": r.method, "path": r.path,
        "status": r.status, "ip": r.ip, "detail": r.detail,
    }


@router.get("/audit")
async def audit_list(
    date_from: Optional[date] = None, date_to: Optional[date] = None,
    action: str = "", username: str = "", q: str = "",
    tz_offset: int = Query(default=480, ge=-840, le=840),
    limit: int = Query(default=200, ge=1, le=1000), offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    start, end = _range(date_from, date_to, tz_offset)
    stmt = _audit_query(start, end, action, username, q)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc())
                             .limit(limit).offset(offset))).scalars().all()
    users = (await db.execute(select(AuditLog.username).distinct()
                              .where(AuditLog.username.is_not(None)))).scalars().all()
    earliest = (await db.execute(select(func.min(AuditLog.at)))).scalar_one()
    conf: sysconf.AuditSettings = await sysconf.load(db, "audit")  # type: ignore[assignment]
    return {
        "total": total, "items": [_audit_row(r) for r in rows],
        "from": start.isoformat(), "to": end.isoformat(),
        "usernames": sorted(u for u in users if u),
        "actions": sorted(set(ACTIONS.values()) | {
            "auth.login", "auth.login_failed", "auth.logout", "audit.purge",
            "backup.scheduled", "health.scheduled"}),
        "earliest": earliest.isoformat() if earliest else None,
        "retention_days": conf.retention_days,
    }


@router.get("/audit/export")
async def audit_export(
    date_from: Optional[date] = None, date_to: Optional[date] = None,
    action: str = "", username: str = "", q: str = "",
    tz_offset: int = Query(default=480, ge=-840, le=840),
    db: AsyncSession = Depends(get_db),
):
    start, end = _range(date_from, date_to, tz_offset)
    stmt = _audit_query(start, end, action, username, q).order_by(AuditLog.at, AuditLog.id)
    rows = (await db.execute(stmt.limit(200000))).scalars().all()
    tz = timezone(timedelta(minutes=tz_offset))
    buf = io.StringIO()
    # BOM: Excel opens a UTF-8 CSV without one as the local code page, and
    # every Chinese character becomes mojibake
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(["time", "user", "action", "target", "method", "path", "status", "ip", "detail"])
    for r in rows:
        w.writerow([
            r.at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if r.at else "",
            r.username or "", r.action, r.target or "", r.method or "", r.path or "",
            r.status if r.status is not None else "", r.ip or "",
            "" if r.detail is None else json.dumps(r.detail, ensure_ascii=False),
        ])
    name = f"mf-topomon-audit-{start.astimezone(tz):%Y%m%d}-{(end - timedelta(seconds=1)).astimezone(tz):%Y%m%d}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
