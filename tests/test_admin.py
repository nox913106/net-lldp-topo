"""
The back office: logins and roles, the audit trail, notifications, the
privileged job runner, the health check and the certificate checks.

Each test states one thing that has to stay true. The login itself, through
the real nginx, is tests/test_proxy_auth.py.
"""
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.main as main_module
from app.core import auth, ldap_auth, maintenance, notify, runner, sysconf
from app.core.audit import ACTIONS
from app.core.passwords import apr1, hash_password, needs_rehash, verify_password
from app.db.database import async_session_maker, init_db
from app.models.admin import AuditLog, NotificationDelivery, NotificationEvent, User
from tests.conftest import authed_client

ROOT = Path(__file__).resolve().parent.parent
H = {"X-Requested-With": "tests"}

# Every test starts from an empty schema: lockouts, saved settings and queued
# notifications from one test must not decide another
pytestmark = pytest.mark.usefixtures("clean_db")


def run(coro):
    return asyncio.run(coro)


async def anon():
    await init_db()
    return AsyncClient(transport=ASGITransport(app=main_module.app), base_url="http://test", headers=H)


# -- passwords -----------------------------------------------------------------


def test_scrypt_round_trips_and_says_when_to_rehash():
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batter", stored)
    assert not needs_rehash(stored)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_the_old_proxy_passwords_still_verify():
    """An upgrade imports htpasswd as it is: the $apr1$ hash openssl made."""
    for password in ("legacy-pass-123", "é長密碼 with spaces and more than sixteen"):
        made = subprocess.run(["openssl", "passwd", "-apr1", password],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert apr1(password, made.split("$")[2]) == made
        assert verify_password(password, made)
        assert not verify_password(password + "x", made)
        assert needs_rehash(made), "an imported hash is replaced on the first login"


def test_an_imported_password_is_rehashed_on_first_login():
    async def check():
        async with async_session_maker() as db:
            hashed = apr1("legacy-pass-123", "Ab3dEf9h")
            await auth.create_local_user(db, "legacy", hashed, "admin")
        async with await anon() as c:
            r = await c.post("/api/v1/auth/login", json={"username": "legacy", "password": "legacy-pass-123"})
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.username == "legacy"))).scalar_one()
            return r.status_code, user.password_hash
    status, stored = run(check())
    assert status == 200
    assert stored.startswith("scrypt$")


# -- who may do what -----------------------------------------------------------


def test_only_the_login_page_and_the_probes_are_public():
    async def check():
        async with await anon() as c:
            return {p: (await c.get(p, headers={"Accept": "text/html"} if not (
                        p.startswith("/api/") or p.startswith("/static/")) else {})).status_code
                    for p in ("/", "/devices", "/admin", "/docs", "/api/v1/devices",
                              "/api/v1/admin/users", "/static/js/app.js",
                              "/login", "/health", "/health/ready", "/api/v1/auth/info",
                              "/static/js/login.js", "/static/css/theme-tactical.css")}
    codes = run(check())
    for page in ("/", "/devices", "/admin", "/docs"):
        assert codes[page] == 303, page
    for api in ("/api/v1/devices", "/api/v1/admin/users", "/static/js/app.js"):
        assert codes[api] == 401, api
    for public in ("/login", "/health", "/health/ready", "/api/v1/auth/info",
                   "/static/js/login.js", "/static/css/theme-tactical.css"):
        assert codes[public] == 200, public


@pytest.mark.parametrize("role,read,change,admin", [
    ("viewer", 200, 403, 403),
    ("operator", 200, 201, 403),
    ("admin", 200, 201, 200),
])
def test_each_role_can_do_exactly_its_share(role, read, change, admin):
    async def check():
        async with authed_client(main_module.app, role) as c:
            return ((await c.get("/api/v1/groups")).status_code,
                    (await c.post("/api/v1/groups", json={"name": f"g-{role}"})).status_code,
                    (await c.get("/api/v1/admin/users")).status_code)
    assert run(check()) == (read, change, admin)


def test_a_guest_and_a_forced_password_change_can_only_do_that():
    async def check():
        async with authed_client(main_module.app, "guest") as c:
            guest = ((await c.get("/api/v1/devices")).json(), (await c.get("/api/v1/auth/me")).status_code)
        async with async_session_maker() as db:
            user, _ = await auth.create_local_user(db, "fresh", "First-pass-1234", "admin", must_change=True)
        async with await anon() as c:
            await c.post("/api/v1/auth/login", json={"username": "fresh", "password": "First-pass-1234"})
            blocked = (await c.get("/api/v1/devices")).json()
            changed = await c.post("/api/v1/auth/password", json={
                "current_password": "First-pass-1234", "new_password": "Second-pass-5678"})
            after = (await c.get("/api/v1/devices")).status_code
        return guest, blocked, changed.status_code, after
    (guest_body, me), blocked, changed, after = run(check())
    assert guest_body["code"] == "not_granted"
    assert me == 200, "a guest can still see who it is (the login page uses it)"
    assert blocked["code"] == "password_change_required"
    assert changed == 200
    assert after == 200


def test_the_last_administrator_cannot_be_removed():
    async def check():
        async with authed_client(main_module.app, "admin") as c:
            users = (await c.get("/api/v1/admin/users")).json()
            for u in users:
                if u["username"] != c.username and u["role"] == "admin":
                    await c.delete(f"/api/v1/admin/users/{u['id']}")
            me = next(u for u in (await c.get("/api/v1/admin/users")).json() if u["username"] == c.username)
            other = (await c.post("/api/v1/admin/users", json={
                "username": "second", "role": "viewer", "password": "Viewer-pass-123"})).json()
            self_demote = await c.put(f"/api/v1/admin/users/{me['id']}", json={"role": "viewer"})
            self_delete = await c.delete(f"/api/v1/admin/users/{me['id']}")
            return self_demote.status_code, self_delete.status_code, other["role"]
    assert run(check()) == (400, 400, "viewer")


def test_failed_logins_lock_the_account_for_a_while():
    async def check():
        async with async_session_maker() as db:
            await auth.create_local_user(db, "target", "Target-pass-123", "viewer")
        async with await anon() as c:
            for _ in range(5):
                await c.post("/api/v1/auth/login", json={"username": "target", "password": "nope"})
            locked = await c.post("/api/v1/auth/login", json={"username": "target", "password": "Target-pass-123"})
        return locked.status_code, locked.json()["detail"]
    status, detail = run(check())
    assert status == 401
    assert "分鐘後再試" in detail, "even the right password waits out the lock"


def test_a_directory_account_starts_as_a_guest_and_gets_its_role_here(monkeypatch):
    seen = {}

    async def fake(conf, username, password):
        seen["args"] = (username, password)
        return ldap_auth.DirectoryUser(username=username, display_name="Alice Wang", email="alice@corp.local")
    monkeypatch.setattr(ldap_auth, "authenticate", fake)

    async def check():
        async with async_session_maker() as db:
            await sysconf.save(db, "ldap", sysconf.LdapSettings(
                enabled=True, urls=["ldaps://dc01.corp.local:636"], domain="corp.local",
                base_dn="DC=corp,DC=local"), "test")
        async with await anon() as c:
            r = await c.post("/api/v1/auth/login", json={"username": "CORP\\Alice", "password": "her-ad-password"})
            role = r.json()["role"]
            blocked = (await c.get("/api/v1/devices")).status_code
        async with async_session_maker() as db:
            user = (await db.execute(select(User).where(User.username == "alice"))).scalar_one()
            user.role = "operator"
            await db.commit()
            return role, blocked, user.source, user.password_hash
    role, blocked, source, stored = run(check())
    assert seen["args"] == ("alice", "her-ad-password")
    assert (role, blocked, source) == ("guest", 403, "ldap")
    assert stored is None, "a directory password is never stored"


def test_an_empty_password_never_reaches_the_directory():
    """An empty password is an anonymous bind, which many directories accept."""
    conf = sysconf.LdapSettings(enabled=True, urls=["ldap://127.0.0.1:1"], domain="x", base_dn="DC=x")
    assert ldap_auth._authenticate(conf, "anyone", "") is None


def test_the_acknowledging_user_is_who_is_logged_in_not_what_the_form_says():
    async def check():
        from app.models.alert import Alert
        from app.models.device import Device
        async with async_session_maker() as db:
            d = Device(hostname="ack-sw", ip_address="10.1.1.1", status="offline")
            db.add(d)
            await db.flush()
            a = Alert(device_id=d.id, alert_type="device_offline", severity="critical", message="x")
            db.add(a)
            await db.commit()
            alert_id = a.id
        async with authed_client(main_module.app, "operator") as c:
            await c.post(f"/api/v1/alerts/{alert_id}/acknowledge", json={"acknowledged_by": "somebody else"})
            name = c.username
        async with async_session_maker() as db:
            return (await db.get(Alert, alert_id)).acknowledged_by, name
    by, name = run(check())
    assert by == name


# -- the audit trail -------------------------------------------------------------


def test_every_route_that_changes_something_has_an_audit_action():
    """A new endpoint is audited regardless; this keeps it readable."""
    missing = []
    for route in main_module.app.routes:
        methods = getattr(route, "methods", set()) or set()
        for method in methods - {"GET", "HEAD", "OPTIONS"}:
            if route.path in ("/api/v1/auth/login", "/api/v1/auth/logout"):
                continue
            if (method, route.path) not in ACTIONS:
                missing.append(f"{method} {route.path}")
    assert not missing, missing


def test_a_change_is_recorded_with_who_and_without_the_secret():
    async def check():
        async with authed_client(main_module.app, "operator") as c:
            await c.post("/api/v1/devices", json={"hostname": "audited-sw", "ip_address": "10.2.2.2",
                                                  "snmp_community": "very-secret"})
            await c.post("/api/v1/groups", json={"name": "x"}, headers={"X-Requested-With": ""})
            name = c.username
        async with async_session_maker() as db:
            rows = (await db.execute(select(AuditLog).where(AuditLog.username == name)
                                     .order_by(AuditLog.id))).scalars().all()
            return name, [(r.action, r.status, r.target, json.dumps(r.detail)) for r in rows]
    name, rows = run(check())
    created = [r for r in rows if r[0] == "device.create"]
    assert created, rows
    action, status, target, detail = created[0]
    assert status in (200, 201)
    assert "audited-sw" in target
    assert "very-secret" not in detail
    assert '"snmp_community": "***"' in detail


def test_a_failed_login_is_recorded_with_why():
    async def check():
        async with await anon() as c:
            await c.post("/api/v1/auth/login", json={"username": "nobody-here", "password": "x"})
        async with async_session_maker() as db:
            row = (await db.execute(select(AuditLog).where(AuditLog.action == "auth.login_failed",
                                                           AuditLog.username == "nobody-here"))).scalar_one()
            return row.detail
    assert run(check())["reason"] == "unknown_user"


def test_the_audit_export_is_a_csv_excel_can_open():
    async def check():
        async with authed_client(main_module.app, "admin") as c:
            r = await c.get("/api/v1/admin/audit/export")
            return r.status_code, r.headers["content-type"], r.text
    status, ctype, text = run(check())
    assert status == 200 and "text/csv" in ctype
    assert text.startswith("﻿time,user,action")


# -- notifications ---------------------------------------------------------------


class _Hook(BaseHTTPRequestHandler):
    received: list = []
    fail = False

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((dict(self.headers), body))
        self.send_response(500 if type(self).fail else 200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def hook():
    _Hook.received = []
    _Hook.fail = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()


def _channel(url, **kw):
    return sysconf.Channel(id="hook1", name="Hook", type="webhook", url=url, secret="s3cret",
                           min_severity="warning", **kw)


def test_an_alert_becomes_a_signed_webhook_message(hook):
    async def check():
        from app.core.alert_engine import AlertEngine, AlertEvent, AlertSeverity, AlertType
        from app.models.device import Device
        async with async_session_maker() as db:
            await sysconf.save(db, "notifications", sysconf.NotificationSettings(channels=[_channel(hook)]), "t")
            d = Device(hostname="core-sw-01", ip_address="10.0.0.1", status="offline")
            db.add(d)
            await db.commit()
            engine = AlertEngine(db)
            await engine.process_events([AlertEvent(
                alert_type=AlertType.DEVICE_OFFLINE, severity=AlertSeverity.CRITICAL,
                device_id=d.id, message="core-sw-01 is offline")])
        async with async_session_maker() as db:
            result = await notify.dispatch_once(db)
        return result
    result = run(check())
    assert result["sent"] == 1
    headers, body = _Hook.received[0]
    payload = json.loads(body)
    assert "core-sw-01" in payload["title"]
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-Mf-Topomon-Signature"] == expected


def test_a_burst_of_alerts_is_one_digest(hook):
    async def check():
        async with async_session_maker() as db:
            await sysconf.save(db, "notifications", sysconf.NotificationSettings(
                channels=[_channel(hook)], digest_threshold=3), "t")
            for i in range(12):
                notify.queue_event(db, "alert_new", {"alert_type": "device_offline", "device": f"sw-{i}",
                                                     "message": f"sw-{i} offline"}, severity="critical")
            await db.commit()
            return await notify.dispatch_once(db)
    result = run(check())
    assert result["sent"] == 1
    payload = json.loads(_Hook.received[0][1])
    assert "12" in payload["title"]
    assert len(payload["events"]) == 12


def test_a_failed_message_is_retried_later_not_dropped(hook):
    _Hook.fail = True

    async def check():
        async with async_session_maker() as db:
            await sysconf.save(db, "notifications", sysconf.NotificationSettings(channels=[_channel(hook)]), "t")
            notify.queue_event(db, "alert_new", {"device": "x", "alert_type": "link_down"}, severity="warning")
            await db.commit()
            first = await notify.dispatch_once(db)
            row = (await db.execute(select(NotificationDelivery))).scalars().all()[-1]
            return first, row.status, row.attempts, row.next_attempt_at
    first, status, attempts, next_at = run(check())
    assert first["retry"] == 1
    assert status == "pending" and attempts == 1
    assert next_at is not None


def test_below_the_channels_severity_nothing_is_sent(hook):
    async def check():
        async with async_session_maker() as db:
            await sysconf.save(db, "notifications", sysconf.NotificationSettings(
                channels=[_channel(hook, events=["alert_new"])]), "t")
            notify.queue_event(db, "alert_new", {"device": "x"}, severity="info")
            notify.queue_event(db, "health_failed", {"count": 1}, severity="critical")
            await db.commit()
            return await notify.dispatch_once(db)
    assert run(check())["queued"] == 0


def test_secrets_are_never_sent_back_to_the_browser_and_survive_a_save():
    async def check():
        async with authed_client(main_module.app, "admin") as c:
            conf = sysconf.NotificationSettings(channels=[sysconf.Channel(
                id="t1", name="Teams", type="teams", url="https://example.webhook.office.com/abcdef0123456789/xyz",
            ), sysconf.Channel(id="m1", name="Mail", type="email", smtp_host="smtp", smtp_password="pw-123",
                               mail_from="a@b", mail_to=["c@d"])])
            assert (await c.put("/api/v1/admin/notifications", json=conf.model_dump())).status_code == 200
            shown = (await c.get("/api/v1/admin/notifications")).json()["value"]
            # Save what the browser was shown: the masked values must not overwrite the real ones
            assert (await c.put("/api/v1/admin/notifications", json=shown)).status_code == 200
        async with async_session_maker() as db:
            stored = await sysconf.load(db, "notifications")
        return shown, stored
    shown, stored = run(check())
    assert "pw-123" not in json.dumps(shown)
    assert "abcdef0123456789" not in json.dumps(shown)
    assert stored.channels[0].url.endswith("abcdef0123456789/xyz")
    assert stored.channels[1].smtp_password == "pw-123"


# -- the runner ----------------------------------------------------------------------


def _helper(*args, **kw):
    return subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "runner_helper.py"), *args],
                          capture_output=True, text=True, **kw)


def test_the_runner_takes_a_valid_request_and_deletes_it(tmp_path):
    req = tmp_path / "requests"
    req.mkdir()
    stage = tmp_path / "stage"
    stage.mkdir()
    (req / "20261119-120000-0123abcd.json").write_text(json.dumps({
        "id": "20261119-120000-0123abcd", "kind": "backup", "by": "alice",
        "args": {"keep": 7, "trigger": "manual", "extra": "ignored"}}))
    out = _helper("take", str(req), str(os.getuid()), str(stage))
    data = json.loads(out.stdout)
    assert data["kind"] == "backup" and data["args"] == {"keep": 7, "trigger": "manual"}
    assert not list(req.iterdir())


@pytest.mark.parametrize("body,why", [
    ({"id": "20261119-120000-0123abcd", "kind": "rm -rf /", "args": {}}, "unknown kind"),
    ({"id": "20261119-120000-0123abcd", "kind": "restore", "args": {"name": "../../etc/shadow"}}, "bad backup name"),
    ({"id": "20261119-120000-0123abcd", "kind": "backup", "args": {"keep": "7; reboot"}}, "keep"),
    ({"id": "20261119-999999-ffffffff", "kind": "doctor", "args": {}}, "does not match"),
])
def test_the_runner_refuses_anything_off_the_list(tmp_path, body, why):
    req = tmp_path / "requests"
    req.mkdir()
    (req / "20261119-120000-0123abcd.json").write_text(json.dumps(body))
    out = json.loads(_helper("take", str(req), str(os.getuid()), str(tmp_path)).stdout)
    assert why in out["error"]
    assert not list(req.iterdir()), "a refused request is removed, or the path unit fires forever"


def test_the_runner_never_follows_a_symlink(tmp_path):
    req = tmp_path / "requests"
    req.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"id": "20261119-120000-0123abcd", "kind": "doctor"}')
    os.symlink(secret, req / "20261119-120000-0123abcd.json")
    out = json.loads(_helper("take", str(req), str(os.getuid()), str(tmp_path)).stdout)
    assert "error" in out
    assert secret.exists(), "the link is removed, not what it points to"


def test_the_runner_ignores_files_another_account_wrote(tmp_path):
    req = tmp_path / "requests"
    req.mkdir()
    (req / "20261119-120000-0123abcd.json").write_text('{"id": "20261119-120000-0123abcd", "kind": "doctor"}')
    out = _helper("take", str(req), str(os.getuid() + 1), str(tmp_path))
    assert "not owned" in out.stdout


def test_the_application_side_queues_and_reads_jobs(tmp_path, monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "app_data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "app_runner_dir", str(tmp_path / "runner"))
    (tmp_path / "data").mkdir()
    (tmp_path / "runner" / "jobs").mkdir(parents=True)
    job_id = runner.submit("doctor", by="alice")
    assert runner.busy()["id"] == job_id
    (tmp_path / "runner" / "jobs" / f"{job_id}.json").write_text(json.dumps({"id": job_id, "kind": "doctor", "state": "ok"}))
    (tmp_path / "runner" / "jobs" / f"{job_id}.log").write_text("all good")
    (tmp_path / "data" / "requests" / f"{job_id}.json").unlink()
    assert runner.job(job_id)["log"] == "all good"
    assert runner.busy() is None


def test_without_a_runner_the_back_office_says_so():
    async def check():
        async with authed_client(main_module.app, "admin") as c:
            return await c.post("/api/v1/admin/backups")
    r = run(check())
    assert r.status_code == 409
    assert "mf-topomon-docker" in r.json()["detail"]


# -- schedules, health, system information ------------------------------------------------


def test_the_backup_schedule_finds_the_moment_it_last_should_have_run():
    daily = sysconf.BackupSettings(frequency="daily", time="02:30")
    now = datetime(2026, 11, 19, 1, 0).astimezone()
    last = maintenance.last_scheduled(daily, now)
    assert (last.day, last.hour, last.minute) == (18, 2, 30)
    weekly = sysconf.BackupSettings(frequency="weekly", weekday=0, time="03:00")  # Monday
    last = maintenance.last_scheduled(weekly, datetime(2026, 11, 19, 12, 0).astimezone())  # a Thursday
    assert last.weekday() == 0 and last.day == 16


def test_the_health_check_reports_and_queues_a_notification_once():
    async def check():
        async with async_session_maker() as db:
            first = await maintenance.run_health(db, "manual")
            await maintenance.run_health(db, "manual")
            events = (await db.execute(select(NotificationEvent).where(
                NotificationEvent.kind == "health_failed"))).scalars().all()
            return first, len(events)
    result, queued = run(check())
    names = {i["name"] for i in result["items"]}
    assert {"資料庫", "收集器", "本機管理員"} <= names
    assert result["counts"]["fail"] >= 1, "no collector is running in the tests"
    assert queued == 1, "the same problems are not announced again on the next run"


def test_system_information_reads_the_host():
    async def check():
        async with authed_client(main_module.app, "admin") as c:
            return (await c.get("/api/v1/admin/sysinfo")).json()
    info = run(check())
    assert info["hardware"]["cpu_count"] >= 1
    assert any(p["name"].lower() == "fastapi" and p["direct"] for p in info["packages"])
    assert info["package_mismatches"] == 0 or all("required" in p for p in info["packages"])


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_a_certificate_and_a_key_that_are_not_a_pair_are_refused(tmp_path):
    from fastapi import HTTPException

    from app.api.admin_ops import inspect_certificate

    def make(name):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
                        "-subj", f"/CN={name}", "-keyout", str(tmp_path / f"{name}.key"),
                        "-out", str(tmp_path / f"{name}.crt")], check=True, capture_output=True)
        return (tmp_path / f"{name}.crt").read_bytes(), (tmp_path / f"{name}.key").read_bytes()
    cert_a, key_a = make("a")
    _, key_b = make("b")
    info = inspect_certificate(cert_a, key_a)
    assert "CN" in info["subject"] and info["not_after"]
    with pytest.raises(HTTPException) as refused:
        inspect_certificate(cert_a, key_b)
    assert "不是一對" in refused.value.detail
