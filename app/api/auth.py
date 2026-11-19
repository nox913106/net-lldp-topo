"""
Logging in and out, and the account's own password.

The session is an HttpOnly cookie holding a random token; the database keeps
only its hash (app/core/auth.py). Secure when the request came over HTTPS -
which, behind the shipped nginx, is every request that is not the HTTP->HTTPS
redirect - and SameSite=Lax, so following a link from an alert e-mail keeps
you logged in while a cross-site POST does not carry it.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, auth, ldap_auth, sysconf
from app.core.passwords import hash_password, password_problem, verify_password
from app.core.security_middleware import client_ip
from app.core.clock import utcnow
from app.db.database import get_db
from app.models.admin import User

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


def current(request: Request) -> auth.Principal:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    return user


def _me(user: User) -> dict:
    return {
        "id": user.id, "username": user.username,
        "display_name": user.display_name or user.username,
        "email": user.email, "role": user.role, "source": user.source,
        "must_change_password": bool(user.must_change_password),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


@router.get("/info")
async def login_info(db: AsyncSession = Depends(get_db)):
    """What the login page needs before anyone is logged in. Nothing secret."""
    ldap: sysconf.LdapSettings = await sysconf.load(db, "ldap")  # type: ignore[assignment]
    from sqlalchemy import func, select
    accounts = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    return {
        "ldap_enabled": ldap_auth.configured(ldap),
        "login_hint": ldap.login_hint or (ldap.domain if ldap_auth.configured(ldap) else ""),
        # No account at all: an install whose first administrator has not
        # been created yet. Says so instead of "wrong password" forever.
        "needs_setup": accounts == 0,
    }


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response,
                db: AsyncSession = Depends(get_db)):
    ip = client_ip(request.scope)
    agent = request.headers.get("user-agent", "")
    try:
        user = await auth.login(db, body.username, body.password)
    except auth.LoginRefused as refused:
        await audit.record("auth.login_failed", username=ldap_auth.clean_username(body.username),
                           method="POST", path="/api/v1/auth/login", status=401, ip=ip,
                           detail={"reason": refused.reason})
        raise HTTPException(status_code=401, detail=refused.public)
    token = await auth.start_session(db, user, ip, agent)
    response.set_cookie(
        auth.COOKIE, token, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    await audit.record("auth.login", user_id=user.id, username=user.username,
                       method="POST", path="/api/v1/auth/login", status=200, ip=ip,
                       detail={"source": user.source, "role": user.role})
    return _me(user)


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    principal = current(request)
    token = request.cookies.get(auth.COOKIE, "")
    await auth.end_session(db, token)
    response.delete_cookie(auth.COOKIE, path="/")
    await audit.record("auth.logout", user_id=principal.id, username=principal.username,
                       method="POST", path="/api/v1/auth/logout", status=200,
                       ip=client_ip(request.scope))
    return {"success": True}


@router.get("/me")
async def me(request: Request, db: AsyncSession = Depends(get_db)):
    principal = current(request)
    user = await db.get(User, principal.id)
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    return _me(user)


@router.post("/password")
async def change_password(body: PasswordChange, request: Request,
                          db: AsyncSession = Depends(get_db)):
    principal = current(request)
    user = await db.get(User, principal.id)
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    if user.source != "local":
        raise HTTPException(status_code=400, detail="目錄帳號的密碼請在 AD 變更")
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="目前的密碼不對")
    problem = password_problem(body.new_password, user.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="新密碼不能跟目前的一樣")
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    user.must_change_password = False
    await db.commit()
    # Everywhere else this account is logged in was logged in with the old
    # password - which may be exactly why it is being changed
    ended = await auth.end_all_sessions(db, user.id, keep_session_id=principal.session_id)
    return {"success": True, "other_sessions_ended": ended}
