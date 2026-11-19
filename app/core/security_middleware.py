"""
Login, roles, CSRF and the audit trail, for every request, in one place.

Pure ASGI rather than BaseHTTPMiddleware because it has to see the request
body (to record what was changed) without consuming it, and to stream large
responses (a backup download) without buffering them.

The rules, in the order they are applied:

  1. A short list of paths is public: the health probes, the login page and
     the handful of files it needs to render. Everything else needs a
     session - including /docs, which can call every endpoint.
  2. No session: a page is redirected to /login?next=..., an API call gets
     401. The redirect is what makes a bookmark or a link from a Teams alert
     land on the login page instead of on a blank map.
  3. A changing request (anything but GET/HEAD/OPTIONS) must carry
     X-Requested-With. A cross-site form or <img> cannot set a custom header,
     and a cross-site fetch() that sets one needs a CORS preflight this API
     does not answer (app/main.py), so this is the CSRF protection; the
     cookie is also SameSite=Lax.
  4. The role check (app/core/auth.py has the roles).
  5. After the response, a changing request is written to the audit trail
     with its status code - a refused change is as interesting as a done one.
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import quote

from starlette.routing import Match

from app.core import audit
from app.core.auth import COOKIE, Principal, session_principal
from app.db.database import async_session_maker

logger = logging.getLogger(__name__)

PUBLIC_EXACT = {
    "/health", "/health/ready", "/login", "/favicon.ico",
    "/api/v1/auth/login", "/api/v1/auth/info",
    "/static/login.html", "/static/js/login.js", "/static/js/i18n.js",
    "/static/js/theme.js", "/static/js/util.js", "/static/js/i18n-admin.js",
    "/static/css/style.css", "/static/css/theme-tactical.css",
}
PUBLIC_PREFIX = ("/static/vendor/fonts/",)

# Any logged-in account, guests and must-change-password included: what the
# login page itself calls once you are in
SELF_SERVICE = {"/api/v1/auth/me", "/api/v1/auth/logout", "/api/v1/auth/password"}

ADMIN_PREFIX = ("/api/v1/admin", "/admin", "/static/admin.html", "/static/js/admin.js")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

PAGES = {"/", "/devices", "/alerts", "/groups", "/settings", "/admin", "/docs", "/redoc"}

# Requests that are not audited even though they change something: the login
# endpoints write their own, more informative rows (who tried, why refused)
NOT_AUDITED = {"/api/v1/auth/login", "/api/v1/auth/logout"}

_MAX_CAPTURE = 256 * 1024


def _cookie(scope, name: str) -> str:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == name:
                    return v
    return ""


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


def client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else ""


def _is_page(scope) -> bool:
    """
    A browser loading a page - which is sent to the login page - as opposed
    to a program, which gets a 401 it can act on.

    A request that carries credentials of its own (Authorization) is a
    program unless it explicitly asks for HTML. That is what a health probe
    with a deliberately wrong password looks like - the check the previous
    version's `mf-topomon upgrade` runs after installing this one, and which
    must keep reading 401 or it would roll the upgrade back.
    """
    path = scope["path"]
    accepts_html = "text/html" in _header(scope, b"accept")
    if _header(scope, b"authorization") and not accepts_html:
        return False
    if path in PAGES or path.endswith(".html"):
        return True
    return accepts_html and not path.startswith("/api/")


async def _respond(send, status: int, body: dict | None = None, headers: list | None = None):
    payload = json.dumps(body or {}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                            (b"cache-control", b"no-store")] + (headers or [])})
    await send({"type": "http.response.body", "body": payload})


async def _redirect(send, location: str):
    await send({"type": "http.response.start", "status": 303,
                "headers": [(b"location", location.encode("latin-1")),
                            (b"cache-control", b"no-store"),
                            (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


def required_role(method: str, path: str) -> str:
    if path.startswith(ADMIN_PREFIX):
        return "admin"
    if method in SAFE_METHODS:
        return "viewer"
    return "operator"


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope["path"]
        method: str = scope["method"]

        if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX):
            # The login endpoint is public but still a POST: without the
            # header check a cross-site form could log a victim's browser
            # into an account the attacker controls (login CSRF)
            if method not in SAFE_METHODS and not _header(scope, b"x-requested-with"):
                return await _respond(send, 403, {
                    "detail": "缺少 X-Requested-With 標頭（防止跨站請求）", "code": "csrf"})
            return await self.app(scope, receive, send)

        token = _cookie(scope, COOKIE)
        principal: Optional[Principal] = None
        if token:
            try:
                async with async_session_maker() as db:
                    principal = await session_principal(db, token)
            except Exception:
                logger.exception("Session lookup failed")
                return await _respond(send, 503, {"detail": "資料庫暫時無法使用"})

        if principal is None:
            if _is_page(scope) and method in SAFE_METHODS:
                target = path + (("?" + scope["query_string"].decode("latin-1"))
                                 if scope.get("query_string") else "")
                return await _redirect(send, "/login?next=" + quote(target, safe=""))
            return await _respond(send, 401, {"detail": "請先登入", "code": "not_authenticated"})

        scope.setdefault("state", {})["user"] = principal

        if method not in SAFE_METHODS and not _header(scope, b"x-requested-with"):
            return await _respond(send, 403, {
                "detail": "缺少 X-Requested-With 標頭（防止跨站請求）", "code": "csrf"})

        # Before anything else, a guest or an account that must change its
        # password can only do that, or log out
        if path not in SELF_SERVICE and (principal.role == "guest" or principal.must_change_password):
            if _is_page(scope) and method in SAFE_METHODS:
                return await _redirect(send, "/login")
            code = "password_change_required" if principal.must_change_password else "not_granted"
            return await _respond(send, 403, {
                "detail": "請先變更密碼" if code == "password_change_required"
                else "這個帳號還沒有被開通，請洽系統管理員", "code": code})

        need = "guest" if path in SELF_SERVICE else required_role(method, path)
        if not principal.at_least(need):
            if _is_page(scope) and method in SAFE_METHODS:
                return await _redirect(send, "/")
            return await _respond(send, 403, {
                "detail": f"需要「{need}」以上的角色（目前是「{principal.role}」）", "code": "forbidden"})

        audited = (method not in SAFE_METHODS and path not in NOT_AUDITED) or path in audit.AUDITED_GETS
        if not audited:
            return await self.app(scope, receive, send)

        captured = bytearray()

        async def capture_receive():
            message = await receive()
            if message["type"] == "http.request" and len(captured) < _MAX_CAPTURE:
                captured.extend(message.get("body", b"")[: _MAX_CAPTURE - len(captured)])
            return message

        status_holder = {"status": None}

        async def capture_send(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, capture_receive, capture_send)
        finally:
            await self._audit(scope, principal, bytes(captured), status_holder["status"])

    async def _audit(self, scope, principal: Principal, body: bytes, status: Optional[int]):
        method, path = scope["method"], scope["path"]
        template, params = path, {}
        app = scope.get("app")
        if app is not None:
            for route in getattr(app.router, "routes", []):
                match, child = route.matches(scope)
                if match == Match.FULL:
                    template = getattr(route, "path", path)
                    params = child.get("path_params", {})
                    break
        action = audit.ACTIONS.get((method, template), f"{method} {template}")
        detail = audit.body_detail(body, _header(scope, b"content-type"))
        query = scope.get("query_string", b"").decode("latin-1")
        if query:
            detail = {"query": query[:500], **(detail if isinstance(detail, dict) else
                                               {"body": detail} if detail is not None else {})}
        await audit.record(
            action, user_id=principal.id, username=principal.username,
            target=audit.target_of(params, detail), method=method, path=path,
            status=status, ip=client_ip(scope), detail=detail,
        )
