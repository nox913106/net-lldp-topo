"""
Back office: notifications, health, backups, the certificate, upgrades and
system information. Admin only (by path, app/core/security_middleware.py).
"""
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current
from app.core import maintenance, notify, runner, sysconf, sysinfo
from app.core.clock import utcnow
from app.core.passwords import verify_password
from app.db.database import get_db
from app.models.admin import NotificationDelivery, NotificationEvent, User

router = APIRouter()


def _runner_error(exc: runner.RunnerUnavailable) -> HTTPException:
    reason = str(exc)
    text = {
        "not_configured": "這個部署沒有背景執行器（Docker 或開發環境）。請在主機上用 mf-topomon-docker 指令執行。",
        "runner_dir_missing": "背景執行器的狀態目錄不存在。請在主機執行 sudo mf-topomon install。",
    }.get(reason, reason)
    return HTTPException(status_code=409, detail=text)


def _submit(kind: str, request: Request, args: Optional[dict] = None, files=None) -> dict:
    busy = runner.busy()
    if busy and kind not in ("doctor", "check-update"):
        raise HTTPException(status_code=409, detail=f"另一個工作（{busy.get('kind')}）還在進行，請等它完成")
    try:
        job_id = runner.submit(kind, by=current(request).username, args=args, files=files)
    except runner.RunnerUnavailable as exc:
        raise _runner_error(exc)
    return {"job": job_id}


# -- Notifications ----------------------------------------------------------

@router.get("/notifications")
async def get_notifications(db: AsyncSession = Depends(get_db)):
    conf = await sysconf.load(db, "notifications")
    return {"value": sysconf.masked(conf), "updated": await sysconf.updated(db, "notifications"),
            "defaults": sysconf.DEFAULT_TEMPLATES,
            "placeholders": sorted(notify.values(NotificationEvent(kind="alert_new", payload={}),
                                                 conf).keys())}  # type: ignore[arg-type]


@router.put("/notifications")
async def put_notifications(body: sysconf.NotificationSettings, request: Request,
                            db: AsyncSession = Depends(get_db)):
    old: sysconf.NotificationSettings = await sysconf.load(db, "notifications")  # type: ignore[assignment]
    ids = [c.id for c in body.channels]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="通道 id 重複")
    try:
        merged = sysconf.merge_secrets(body, old)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for kind, tpl in merged.templates.items():
        for part in ("title", "body"):
            try:
                tpl.get(part, "").format_map(notify._Fill())
            except (ValueError, IndexError) as exc:
                raise HTTPException(status_code=400, detail=f"範本 {kind}.{part} 格式錯誤：{exc}")
    await sysconf.save(db, "notifications", merged, current(request).username)
    return {"success": True}


class TestRequest(BaseModel):
    channel: sysconf.Channel
    kind: str = "alert_new"


@router.post("/notifications/test")
async def test_notification(body: TestRequest, db: AsyncSession = Depends(get_db)):
    """Send one sample message through the channel *as sent* (unsaved edits included)."""
    conf: sysconf.NotificationSettings = await sysconf.load(db, "notifications")  # type: ignore[assignment]
    channel = body.channel
    saved = {c.id: c for c in conf.channels}.get(channel.id)
    if saved is not None:
        channel = sysconf.merge_secrets(
            sysconf.NotificationSettings(channels=[channel]),
            sysconf.NotificationSettings(channels=[saved])).channels[0]
    elif sysconf.SECRET in channel.smtp_password + channel.secret + channel.url:
        raise HTTPException(status_code=400, detail="請重新輸入密碼或網址")
    sample = NotificationEvent(kind=body.kind if body.kind in sysconf.DEFAULT_TEMPLATES else "alert_new",
                               severity="critical", created_at=utcnow(), payload={
                                   "alert_type": "device_offline", "device": "core-sw-01 (測試)",
                                   "ip": "192.0.2.1", "message": "這是 mf-topomon 的測試訊息，不是真的告警",
                                   "time": utcnow().isoformat(), "duration_seconds": 754,
                                   "count": 2, "items": ["核心／收集器：測試", "維運／備份：測試"],
                                   "job": "backup", "ok": True, "summary": "測試"})
    title, text = notify.render(sample, conf)
    title = "[測試] " + title
    try:
        await notify.send(channel, title, text, "critical", [])
    except Exception as exc:
        db.add(NotificationDelivery(channel_id=channel.id, channel_name=channel.name,
                                    channel_type=channel.type, title=title, body=text,
                                    status="failed", attempts=1, last_error=str(exc)[:1000]))
        await db.commit()
        return {"ok": False, "error": str(exc)[:1000]}
    db.add(NotificationDelivery(channel_id=channel.id, channel_name=channel.name,
                                channel_type=channel.type, title=title, body=text,
                                status="sent", attempts=1, sent_at=utcnow()))
    await db.commit()
    return {"ok": True}


@router.get("/notifications/deliveries")
async def deliveries(limit: int = 100, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(NotificationDelivery).order_by(NotificationDelivery.id.desc())
                             .limit(min(limit, 500)))).scalars().all()
    pending = (await db.execute(select(NotificationEvent).where(NotificationEvent.status == "pending"))).scalars().all()
    return {
        "pending_events": len(pending),
        "items": [{
            "id": d.id, "created_at": d.created_at.isoformat() if d.created_at else None,
            "channel": d.channel_name, "type": d.channel_type, "title": d.title,
            "status": d.status, "attempts": d.attempts, "error": d.last_error,
            "events": len(d.event_ids or []),
            "sent_at": d.sent_at.isoformat() if d.sent_at else None,
            "next_attempt_at": d.next_attempt_at.isoformat() if d.next_attempt_at and d.status == "pending" else None,
        } for d in rows]}


@router.post("/notifications/deliveries/{delivery_id}/retry")
async def retry_delivery(delivery_id: int, db: AsyncSession = Depends(get_db)):
    d = await db.get(NotificationDelivery, delivery_id)
    if d is None:
        raise HTTPException(status_code=404, detail="找不到這筆發送紀錄")
    if d.status == "sent":
        raise HTTPException(status_code=400, detail="這則已經送出了")
    d.status = "pending"
    d.attempts = 0
    d.next_attempt_at = utcnow()
    await db.commit()
    return {"success": True}


# -- Health -----------------------------------------------------------------

@router.get("/health")
async def get_health(db: AsyncSession = Depends(get_db)):
    state = await maintenance.get_state(db, "health")
    conf = await sysconf.load(db, "health")
    doctor = runner.jobs(limit=1, kind="doctor")
    return {"last": state or None, "settings": conf.model_dump(), "doctor": doctor[0] if doctor else None,
            "runner": runner.available()}


@router.post("/health/run")
async def health_run(db: AsyncSession = Depends(get_db)):
    return await maintenance.run_health(db, "manual")


@router.put("/settings/health")
async def put_health(body: sysconf.HealthSettings, request: Request, db: AsyncSession = Depends(get_db)):
    await sysconf.save(db, "health", body, current(request).username)
    return {"success": True}


@router.post("/doctor")
async def doctor(request: Request):
    return _submit("doctor", request)


# -- Jobs -------------------------------------------------------------------

@router.get("/jobs")
async def list_jobs(kind: Optional[str] = None, limit: int = 30):
    return {"runner": runner.available(), "items": [
        {k: v for k, v in j.items() if k != "log"} for j in runner.jobs(limit=min(limit, 200), kind=kind)]}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    j = runner.job(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="找不到這個工作")
    return j


# -- Backups ----------------------------------------------------------------

@router.get("/backups")
async def list_backups(db: AsyncSession = Depends(get_db)):
    conf: sysconf.BackupSettings = await sysconf.load(db, "backup")  # type: ignore[assignment]
    items = runner.backups()
    now = utcnow()
    return {
        "runner": runner.available(), "settings": conf.model_dump(),
        "next_run": maintenance.next_scheduled(conf, now).isoformat() if conf.enabled else None,
        "items": items, "total_size": sum(b["size"] for b in items),
        "disk": runner.disk_usage(runner.backup_dir()),
        "busy": runner.busy(), "dir": str(runner.backup_dir() or ""),
    }


@router.put("/settings/backup")
async def put_backup(body: sysconf.BackupSettings, request: Request, db: AsyncSession = Depends(get_db)):
    await sysconf.save(db, "backup", body, current(request).username)
    # A changed schedule starts counting from now, not from a moment that has
    # already passed under the new time
    await maintenance.set_state(db, "backup", {"last_requested": utcnow().isoformat()})
    return {"success": True}


@router.post("/backups")
async def backup_now(request: Request, db: AsyncSession = Depends(get_db)):
    conf: sysconf.BackupSettings = await sysconf.load(db, "backup")  # type: ignore[assignment]
    return _submit("backup", request, {"keep": conf.keep, "trigger": "manual"})


class Confirm(BaseModel):
    password: str = Field(default="", max_length=1024)
    confirm: str = ""


async def _reauth(request: Request, db: AsyncSession, password: str) -> None:
    """Destructive or data-exporting actions ask for the password again."""
    me = current(request)
    user = await db.get(User, me.id)
    if user is None:
        raise HTTPException(status_code=401, detail="請先登入")
    if user.source == "local":
        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=403, detail="密碼不對")
        return
    from app.core import ldap_auth
    conf = await sysconf.load(db, "ldap")
    try:
        ok = await ldap_auth.authenticate(conf, user.username, password)  # type: ignore[arg-type]
    except ldap_auth.LdapUnavailable:
        raise HTTPException(status_code=503, detail="目錄伺服器連不上，無法確認身分")
    if ok is None:
        raise HTTPException(status_code=403, detail="密碼不對")


def _backup_or_404(name: str) -> Path:
    p = runner.backup_path(name)
    if p is None:
        raise HTTPException(status_code=404, detail="找不到這份備份")
    return p


@router.post("/backups/{name}/download")
async def download_backup(name: str, body: Confirm, request: Request, db: AsyncSession = Depends(get_db)):
    p = _backup_or_404(name)
    await _reauth(request, db, body.password)
    return FileResponse(p, media_type="application/gzip", filename=name)


@router.delete("/backups/{name}")
async def delete_backup(name: str, request: Request):
    _backup_or_404(name)
    return _submit("delete-backup", request, {"name": name})


@router.post("/backups/{name}/restore")
async def restore_backup(name: str, body: Confirm, request: Request, db: AsyncSession = Depends(get_db)):
    _backup_or_404(name)
    if body.confirm != "RESTORE":
        raise HTTPException(status_code=400, detail="請輸入 RESTORE 確認")
    await _reauth(request, db, body.password)
    return _submit("restore", request, {"name": name})


# -- HTTPS certificate ------------------------------------------------------

def _openssl(*args: str, data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], input=data, capture_output=True, timeout=15)


def inspect_certificate(cert: bytes, key: bytes, chain: bytes = b"") -> dict:
    """What the runner will check again as root - done here first so the form can say why."""
    if b"-----BEGIN" not in cert or b"CERTIFICATE" not in cert:
        raise HTTPException(status_code=400, detail="憑證要是 PEM 格式（-----BEGIN CERTIFICATE-----）")
    if b"PRIVATE KEY" not in key:
        raise HTTPException(status_code=400, detail="私鑰要是 PEM 格式（-----BEGIN PRIVATE KEY-----）")
    if b"ENCRYPTED" in key:
        raise HTTPException(status_code=400, detail="私鑰有密碼保護，nginx 無法自動載入。請先解除密碼：openssl pkey -in key.pem -out key-plain.pem")
    info = _openssl("x509", "-noout", "-subject", "-issuer", "-enddate", "-startdate",
                    "-ext", "subjectAltName", "-fingerprint", "-sha256", data=cert)
    if info.returncode != 0:
        raise HTTPException(status_code=400, detail="讀不出這張憑證：" + info.stderr.decode(errors="replace")[:300])
    cert_pub = _openssl("x509", "-noout", "-pubkey", data=cert).stdout
    key_pub = _openssl("pkey", "-pubout", data=key)
    if key_pub.returncode != 0:
        raise HTTPException(status_code=400, detail="讀不出這把私鑰：" + key_pub.stderr.decode(errors="replace")[:300])
    if cert_pub.strip() != key_pub.stdout.strip():
        raise HTTPException(status_code=400, detail="私鑰與憑證不是一對（公鑰不同）")
    text = info.stdout.decode(errors="replace")
    end = re.search(r"notAfter=(.+)", text)
    start = re.search(r"notBefore=(.+)", text)
    parse = lambda s: datetime.strptime(s.strip(), "%b %d %H:%M:%S %Y %Z")  # noqa: E731
    not_after = parse(end.group(1)) if end else None
    not_before = parse(start.group(1)) if start else None
    now = datetime.utcnow()
    if not_after and not_after < now:
        raise HTTPException(status_code=400, detail=f"這張憑證已經在 {not_after:%Y-%m-%d} 過期")
    if not_before and not_before > now + timedelta(minutes=5):
        raise HTTPException(status_code=400, detail=f"這張憑證要到 {not_before:%Y-%m-%d} 才生效")
    if chain and b"CERTIFICATE" not in chain:
        raise HTTPException(status_code=400, detail="中繼憑證要是 PEM 格式")
    san = re.search(r"X509v3 Subject Alternative Name:\s*\n\s*(.+)", text)
    return {
        "subject": (re.search(r"subject=(.+)", text) or [None, ""])[1].strip(),
        "issuer": (re.search(r"issuer=(.+)", text) or [None, ""])[1].strip(),
        "not_before": not_before.isoformat() + "Z" if not_before else None,
        "not_after": not_after.isoformat() + "Z" if not_after else None,
        "san": san.group(1).strip() if san else "",
        "fingerprint": (re.search(r"Fingerprint=(.+)", text) or [None, ""])[1].strip(),
    }


@router.get("/tls")
async def get_tls():
    return {"current": runner.status_file("tls.json"), "runner": runner.available(),
            "jobs": [{k: v for k, v in j.items() if k != "log"} for j in runner.jobs(limit=5, kind="tls")]}


class TlsUpload(BaseModel):
    # PEM is text, so the browser reads the files and sends them as JSON:
    # no multipart parser to install for the one upload this API has
    cert: str = Field(min_length=1, max_length=256 * 1024)
    key: str = Field(min_length=1, max_length=256 * 1024)
    chain: str = Field(default="", max_length=256 * 1024)


@router.post("/tls")
async def upload_tls(body: TlsUpload, request: Request):
    cert_b, key_b, chain_b = (body.cert.encode(), body.key.encode(), body.chain.encode())
    info = inspect_certificate(cert_b, key_b, chain_b)
    full = cert_b.rstrip(b"\n") + b"\n" + (chain_b.rstrip(b"\n") + b"\n" if chain_b else b"")
    result = _submit("tls", request, files={"cert.pem": full, "key.pem": key_b})
    return {**result, "certificate": info}


# -- Versions and upgrade ---------------------------------------------------

@router.get("/version")
async def get_version():
    return {"current": runner.status_file("version.json"), "update": runner.status_file("update.json"),
            "runner": runner.available(),
            "jobs": [{k: v for k, v in j.items() if k != "log"}
                     for j in runner.jobs(limit=20) if j.get("kind") in ("upgrade", "check-update")]}


@router.post("/upgrade/check")
async def upgrade_check(request: Request):
    return _submit("check-update", request)


@router.post("/upgrade")
async def upgrade(body: Confirm, request: Request, db: AsyncSession = Depends(get_db)):
    if body.confirm != "UPGRADE":
        raise HTTPException(status_code=400, detail="請輸入 UPGRADE 確認")
    return _submit("upgrade", request)


# -- System information ------------------------------------------------------

@router.get("/sysinfo")
async def get_sysinfo(db: AsyncSession = Depends(get_db)):
    return await sysinfo.collect(db)
