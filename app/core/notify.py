"""
Telling a human: Microsoft Teams, a generic webhook, or e-mail.

Two halves in two processes:

  * queue_event() is called by whoever has something to say - the alert
    engine in the collector, the health checks and job watcher in the
    application - and only inserts a row, inside the caller's transaction.
  * dispatch_once() runs in the application's maintenance loop
    (app/core/maintenance.py): it turns pending events into one message per
    channel, and sends them with retries.

So a slow or unreachable Teams endpoint delays a message, never a poll
cycle; and a crash between "alert created" and "message sent" loses
nothing, because the event is in the same commit as the alert.

A burst of events (a core switch takes 200 devices with it) becomes one
digest per channel instead of 200 posts: anything above digest_threshold in
one round is summarised.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any, Dict, Iterable, List, Optional

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import sysconf
from app.core.clock import utcnow
from app.models.admin import NotificationDelivery, NotificationEvent

logger = logging.getLogger(__name__)

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
SEVERITY_LABEL = {"info": "資訊", "warning": "警告", "critical": "嚴重"}
TYPE_LABEL = {
    "device_offline": "設備離線",
    "link_down": "鏈路中斷",
    "link_high_utilization": "鏈路使用率過高",
    "cpu_high": "CPU 使用率過高",
    "memory_high": "記憶體使用率過高",
    "link_flapping": "鏈路反覆斷線",
}
JOB_LABEL = {
    "backup": "備份", "restore": "還原", "upgrade": "升級", "tls": "憑證更新",
    "doctor": "完整健檢", "check-update": "檢查更新", "delete-backup": "刪除備份",
}
# Seconds to wait before attempt n+1 (n = attempts so far)
RETRY_DELAYS = [60, 300, 900, 3600, 3 * 3600]
TIMEOUT = 15.0


def queue_event(db: AsyncSession, kind: str, payload: Dict[str, Any],
                severity: Optional[str] = None, alert_id: Optional[int] = None) -> None:
    """Add an event to the caller's transaction. The caller commits."""
    db.add(NotificationEvent(kind=kind, severity=severity, alert_id=alert_id,
                             payload=payload, status="pending", created_at=utcnow()))


# -- Rendering ----------------------------------------------------------------

class _Fill(dict):
    """str.format_map that leaves an unknown {placeholder} as it is."""

    def __missing__(self, key):
        return "{" + key + "}"


def _local(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分鐘"
    if seconds < 86400:
        return f"{seconds // 3600} 小時 {seconds % 3600 // 60} 分"
    return f"{seconds // 86400} 天 {seconds % 86400 // 3600} 小時"


def values(event: NotificationEvent, conf: sysconf.NotificationSettings) -> Dict[str, str]:
    p = dict(event.payload or {})
    base = conf.public_url.rstrip("/")
    link = ""
    if base:
        link = f"{base}/alerts" if event.kind.startswith("alert") else f"{base}/admin"
    v = {
        "severity": event.severity or "",
        "severity_label": SEVERITY_LABEL.get(event.severity or "", event.severity or ""),
        "type": p.get("alert_type", ""),
        "type_label": TYPE_LABEL.get(p.get("alert_type", ""), p.get("alert_type", "")),
        "device": p.get("device") or "",
        "ip": p.get("ip") or "",
        "message": p.get("message") or "",
        "time": _local(p.get("time") or (event.created_at.isoformat() if event.created_at else None)),
        "duration": _duration(p.get("duration_seconds")),
        "count": str(p.get("count", "")),
        "items": "\n".join(f"• {i}" for i in p.get("items", [])),
        "job_label": JOB_LABEL.get(p.get("job", ""), p.get("job", "")),
        "result_label": "完成" if p.get("ok") else "失敗",
        "summary": p.get("summary") or "",
        "url": link,
    }
    return {k: str(x) for k, x in v.items()}


def render(event: NotificationEvent, conf: sysconf.NotificationSettings) -> tuple[str, str]:
    tpl = conf.templates.get(event.kind) or sysconf.DEFAULT_TEMPLATES.get(event.kind) or {
        "title": "{message}", "body": ""}
    fill = _Fill(values(event, conf))
    title = tpl.get("title", "").format_map(fill).strip()
    body = tpl.get("body", "").format_map(fill).strip()
    return title, body


def render_digest(events: List[NotificationEvent], conf: sysconf.NotificationSettings) -> tuple[str, str]:
    new = [e for e in events if e.kind == "alert_new"]
    resolved = [e for e in events if e.kind == "alert_resolved"]
    other = [e for e in events if e not in new and e not in resolved]
    crit = sum(1 for e in new if e.severity == "critical")
    parts = []
    if new:
        parts.append(f"{len(new)} 則新告警" + (f"（{crit} 則嚴重）" if crit else ""))
    if resolved:
        parts.append(f"{len(resolved)} 則恢復")
    if other:
        parts.append(f"{len(other)} 則系統訊息")
    title = "[告警彙整] " + "、".join(parts)
    lines = []
    for e in events[:40]:
        t, _ = render(e, conf)
        lines.append(f"• {t}")
    if len(events) > 40:
        lines.append(f"…還有 {len(events) - 40} 則，請到告警頁查看")
    base = conf.public_url.rstrip("/")
    if base:
        lines.append(f"{base}/alerts")
    return title, "\n".join(lines)


def wanted(channel: sysconf.Channel, event: NotificationEvent, conf: sysconf.NotificationSettings) -> bool:
    if not channel.enabled or event.kind not in channel.events:
        return False
    if event.kind.startswith("alert"):
        if SEVERITY_RANK.get(event.severity or "info", 0) < SEVERITY_RANK[channel.min_severity]:
            return False
        if conf.root_cause_only and (event.payload or {}).get("is_root_cause") is False:
            return False
    return True


# -- Sending ------------------------------------------------------------------

def _teams_card(title: str, body: str, severity: Optional[str]) -> dict:
    color = {"critical": "Attention", "warning": "Warning"}.get(severity or "", "Good")
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "msteams": {"width": "Full"},
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder",
                     "size": "Medium", "wrap": True, "color": color},
                    {"type": "TextBlock", "text": body.replace("\n", "\n\n"), "wrap": True},
                ],
            },
        }],
    }


async def _post(url: str, payload: dict, headers: Optional[dict] = None, raw: Optional[bytes] = None):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(url, content=raw if raw is not None else json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json", **(headers or {})})
    if r.status_code >= 300:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    # Teams' classic incoming webhook answers 200 with a body of "1" on
    # success and 200 with an error text on some failures
    text = (r.text or "").strip()
    if text and text not in ("1",) and "error" in text.lower()[:200]:
        raise RuntimeError(text[:300])


def _send_mail(ch: sysconf.Channel, title: str, body: str) -> None:
    if not ch.smtp_host or not ch.mail_to or not ch.mail_from:
        raise RuntimeError("SMTP 主機、寄件者、收件者都要填")
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = ch.mail_from
    msg["To"] = ", ".join(ch.mail_to)
    msg.set_content(body)
    context = ssl.create_default_context()
    if ch.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(ch.smtp_host, ch.smtp_port, timeout=TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(ch.smtp_host, ch.smtp_port, timeout=TIMEOUT)
    try:
        server.ehlo()
        if ch.smtp_security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if ch.smtp_user:
            server.login(ch.smtp_user, ch.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send(ch: sysconf.Channel, title: str, body: str, severity: Optional[str] = None,
               events: Optional[List[dict]] = None) -> None:
    """Deliver one message. Raises with a readable reason on failure."""
    if ch.type == "teams":
        if not ch.url:
            raise RuntimeError("沒有填 Webhook 網址")
        await _post(ch.url, _teams_card(title, body, severity))
    elif ch.type == "webhook":
        if not ch.url:
            raise RuntimeError("沒有填 Webhook 網址")
        payload = {"source": "mf-topomon", "title": title, "text": body,
                   "severity": severity, "events": events or [],
                   "sent_at": utcnow().isoformat()}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = {}
        if ch.secret:
            headers["X-Mf-Topomon-Signature"] = "sha256=" + hmac.new(
                ch.secret.encode(), raw, hashlib.sha256).hexdigest()
        await _post(ch.url, payload, headers=headers, raw=raw)
    elif ch.type == "email":
        await asyncio.to_thread(_send_mail, ch, title, body)
    else:  # pragma: no cover - the model forbids it
        raise RuntimeError(f"不支援的通道類型 {ch.type}")


# -- The dispatcher -----------------------------------------------------------

def _event_summary(e: NotificationEvent) -> dict:
    p = e.payload or {}
    return {"id": e.id, "kind": e.kind, "severity": e.severity, "alert_id": e.alert_id,
            "device": p.get("device"), "ip": p.get("ip"), "type": p.get("alert_type"),
            "message": p.get("message"), "time": p.get("time")}


async def fan_out(db: AsyncSession, conf: sysconf.NotificationSettings) -> int:
    """Pending events -> one delivery per channel (a digest when there are many)."""
    events = (await db.execute(
        select(NotificationEvent).where(NotificationEvent.status == "pending")
        .order_by(NotificationEvent.id).limit(1000))).scalars().all()
    if not events:
        return 0
    now = utcnow()
    created = 0
    if conf.enabled:
        for ch in conf.channels:
            mine = [e for e in events if wanted(ch, e, conf)]
            if not mine:
                continue
            if len(mine) > conf.digest_threshold:
                groups: Iterable[List[NotificationEvent]] = [mine]
            else:
                groups = [[e] for e in mine]
            for group in groups:
                if len(group) == 1:
                    title, body = render(group[0], conf)
                else:
                    title, body = render_digest(group, conf)
                db.add(NotificationDelivery(
                    created_at=now, channel_id=ch.id, channel_name=ch.name, channel_type=ch.type,
                    event_ids=[e.id for e in group], title=title, body=body,
                    status="pending", attempts=0, next_attempt_at=now))
                created += 1
    ids = [e.id for e in events]
    await db.execute(update(NotificationEvent).where(NotificationEvent.id.in_(ids))
                     .values(status="processed", processed_at=now))
    await db.commit()
    return created


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def deliver_due(db: AsyncSession, conf: sysconf.NotificationSettings) -> Dict[str, int]:
    now = utcnow()
    due = (await db.execute(
        select(NotificationDelivery).where(NotificationDelivery.status == "pending")
        .order_by(NotificationDelivery.id).limit(200))).scalars().all()
    channels = {c.id: c for c in conf.channels}
    result = {"sent": 0, "failed": 0, "retry": 0}
    for d in due:
        if d.next_attempt_at is not None and _aware(d.next_attempt_at) > now:
            continue
        ch = channels.get(d.channel_id)
        if ch is None:
            d.status = "failed"
            d.last_error = "通道已被刪除"
            result["failed"] += 1
            continue
        events = []
        if d.event_ids:
            rows = (await db.execute(select(NotificationEvent).where(
                NotificationEvent.id.in_(d.event_ids)))).scalars().all()
            events = [_event_summary(e) for e in rows]
        severity = max((e.get("severity") or "info" for e in events),
                       key=lambda s: SEVERITY_RANK.get(s, 0), default=None)
        d.attempts = (d.attempts or 0) + 1
        try:
            await send(ch, d.title or "", d.body or "", severity, events)
        except Exception as exc:
            d.last_error = str(exc)[:1000] or exc.__class__.__name__
            if d.attempts > len(RETRY_DELAYS):
                d.status = "failed"
                result["failed"] += 1
            else:
                d.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[d.attempts - 1])
                result["retry"] += 1
            logger.warning("Notification %s to %s failed (attempt %s): %s",
                           d.id, ch.name, d.attempts, d.last_error)
        else:
            d.status = "sent"
            d.sent_at = utcnow()
            d.last_error = None
            result["sent"] += 1
        await db.commit()
    return result


async def dispatch_once(db: AsyncSession) -> Dict[str, int]:
    conf: sysconf.NotificationSettings = await sysconf.load(db, "notifications")  # type: ignore[assignment]
    made = await fan_out(db, conf)
    result = await deliver_due(db, conf)
    result["queued"] = made
    return result


async def purge_old(db: AsyncSession, days: int = 30) -> None:
    cutoff = utcnow() - timedelta(days=days)
    await db.execute(delete(NotificationEvent).where(NotificationEvent.created_at < cutoff,
                                                     NotificationEvent.status != "pending"))
    await db.execute(delete(NotificationDelivery).where(NotificationDelivery.created_at < cutoff,
                                                        NotificationDelivery.status != "pending"))
    await db.commit()
