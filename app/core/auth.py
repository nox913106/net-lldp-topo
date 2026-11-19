"""
Who is making this request, and may they.

Four roles, each including the one below it:

    admin     everything, including the back office (/api/v1/admin, accounts)
    operator  changes the network: devices, groups, alert profiles, SNMP
              settings, acknowledging alerts, discovery and the SNMP tools
    viewer    reads everything an operator can, changes nothing
    guest     logged in but not yet granted anything - a directory account
              on its first login. Sees only "ask an administrator".

The check is by HTTP method and path (app/core/security_middleware.py), not
by decorating each endpoint: a new endpoint is protected the day it is
written, and "which endpoints can a viewer call" is answered by one table
instead of by reading every router.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ldap_auth, sysconf
from app.core.clock import utcnow
from app.core.passwords import (
    hash_password, needs_rehash, new_token, token_hash, verify_password,
)
from app.models.admin import User, UserSession

logger = logging.getLogger(__name__)

ROLES = ("guest", "viewer", "operator", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

COOKIE = "mft_session"


@dataclass
class Principal:
    """The logged-in user, as the rest of the request sees it."""
    id: int
    username: str
    display_name: str
    role: str
    source: str
    session_id: int
    must_change_password: bool

    def at_least(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[role]

    @property
    def label(self) -> str:
        return self.display_name or self.username


def _aware(value):
    # SQLite hands back naive datetimes even through UTCDateTime in some
    # paths; treat them as UTC like everything else this project writes.
    from datetime import timezone
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def session_principal(db: AsyncSession, token: str) -> Optional[Principal]:
    """The user behind a session cookie, refreshing its idle timer."""
    if not token:
        return None
    row = (await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash(token))
    )).first()
    if row is None:
        return None
    sess, user = row
    now = utcnow()
    conf: sysconf.AuthSettings = await sysconf.load(db, "auth")  # type: ignore[assignment]
    last = _aware(sess.last_seen_at) or _aware(sess.created_at) or now
    if (not user.is_active
            or _aware(sess.expires_at) <= now
            or now - last > timedelta(minutes=conf.idle_minutes)):
        await db.delete(sess)
        await db.commit()
        return None
    # Written at most once a minute: a page that polls every few seconds
    # should not turn every poll into a write.
    if now - last > timedelta(seconds=60):
        sess.last_seen_at = now
        await db.commit()
    return Principal(
        id=user.id, username=user.username, display_name=user.display_name or "",
        role=user.role, source=user.source, session_id=sess.id,
        must_change_password=bool(user.must_change_password),
    )


async def start_session(db: AsyncSession, user: User, ip: str, agent: str) -> str:
    conf: sysconf.AuthSettings = await sysconf.load(db, "auth")  # type: ignore[assignment]
    token = new_token()
    now = utcnow()
    db.add(UserSession(
        token_hash=token_hash(token), user_id=user.id, created_at=now,
        last_seen_at=now, expires_at=now + timedelta(hours=conf.absolute_hours),
        ip=ip[:64], user_agent=(agent or "")[:300],
    ))
    user.last_login_at = now
    user.failed_logins = 0
    user.locked_until = None
    await db.commit()
    return token


async def end_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))
    await db.commit()


async def end_all_sessions(db: AsyncSession, user_id: int, keep_session_id: Optional[int] = None) -> int:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_session_id is not None:
        stmt = stmt.where(UserSession.id != keep_session_id)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def purge_expired_sessions(db: AsyncSession) -> int:
    result = await db.execute(delete(UserSession).where(UserSession.expires_at < utcnow()))
    await db.commit()
    return result.rowcount or 0


class LoginRefused(Exception):
    """Why a login was refused - the reason is audited, the user sees `public`."""

    def __init__(self, reason: str, public: str = "帳號或密碼錯誤"):
        super().__init__(reason)
        self.reason = reason
        self.public = public


async def _register_failure(db: AsyncSession, user: User) -> None:
    conf: sysconf.AuthSettings = await sysconf.load(db, "auth")  # type: ignore[assignment]
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= conf.lockout_failures:
        user.locked_until = utcnow() + timedelta(minutes=conf.lockout_minutes)
        user.failed_logins = 0
    await db.commit()


async def login(db: AsyncSession, raw_username: str, password: str) -> User:
    """
    The account these credentials belong to, or LoginRefused.

    A local account is checked first even when the directory is configured:
    that is what makes the emergency account work on the day the domain
    controllers do not. A name that is not a local account goes to the
    directory, and an unknown name or a wrong password both read "wrong
    username or password", so the page does not tell a stranger which
    accounts exist.
    """
    username = ldap_auth.clean_username(raw_username)
    if not username or not password:
        raise LoginRefused("empty")
    user = (await db.execute(select(User).where(func.lower(User.username) == username))).scalar_one_or_none()
    now = utcnow()

    if user is not None and _aware(user.locked_until) and _aware(user.locked_until) > now:
        minutes = int((_aware(user.locked_until) - now).total_seconds() // 60) + 1
        raise LoginRefused("locked", f"登入失敗太多次，請 {minutes} 分鐘後再試")
    if user is not None and not user.is_active:
        raise LoginRefused("disabled", "這個帳號已停用，請洽系統管理員")

    if user is not None and user.source == "local":
        if not verify_password(password, user.password_hash):
            await _register_failure(db, user)
            raise LoginRefused("bad_password")
        if needs_rehash(user.password_hash):
            # The imported $apr1$ hash, or a cost raised since
            user.password_hash = hash_password(password)
            await db.commit()
        return user

    conf: sysconf.LdapSettings = await sysconf.load(db, "ldap")  # type: ignore[assignment]
    if not ldap_auth.configured(conf):
        raise LoginRefused("unknown_user")
    try:
        found = await ldap_auth.authenticate(conf, username, password)
    except ldap_auth.LdapUnavailable as exc:
        raise LoginRefused(f"ldap_unavailable: {exc}", "目錄伺服器連不上，請稍後再試，或用本機帳號登入")
    if found is None:
        if user is not None:
            await _register_failure(db, user)
        raise LoginRefused("ldap_refused")

    if user is None:
        user = User(username=username, source="ldap", role=conf.default_role,
                    display_name=found.display_name, email=found.email, is_active=True)
        db.add(user)
        await db.flush()
    else:
        user.display_name = found.display_name or user.display_name
        user.email = found.email or user.email
    await db.commit()
    return user


async def create_local_user(
    db: AsyncSession, username: str, password: str, role: str = "admin",
    display_name: str = "", must_change: bool = False,
) -> Tuple[User, bool]:
    """Create or reset a local account. Returns (user, created)."""
    username = ldap_auth.clean_username(username)
    user = (await db.execute(select(User).where(func.lower(User.username) == username))).scalar_one_or_none()
    created = user is None
    if created:
        user = User(username=username, source="local")
        db.add(user)
    assert user is not None
    user.source = "local"
    user.role = role
    user.display_name = display_name or user.display_name or username
    user.password_hash = password if password.startswith("$apr1$") else hash_password(password)
    user.password_changed_at = utcnow()
    user.must_change_password = must_change
    user.is_active = True
    user.failed_logins = 0
    user.locked_until = None
    await db.commit()
    return user, created


async def count_active_admins(db: AsyncSession, local_only: bool = False) -> int:
    stmt = select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
    if local_only:
        stmt = stmt.where(User.source == "local")
    return int((await db.execute(stmt)).scalar_one())
