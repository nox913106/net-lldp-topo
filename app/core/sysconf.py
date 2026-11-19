"""
Back-office settings, one JSON document per section in system_settings.

Each section is a pydantic model, so a value that was saved by an older
version (a missing key) reads back with the current default, and a value an
administrator types is validated before it is stored rather than when it is
next used. Saving replaces the whole section - the web forms always send the
whole section - and records who did it.

Secrets (the SMTP password, the LDAP bind password) are stored as they are:
the database already holds every device's SNMP community in clear, and the
backups that carry it are readable by root and the service account only. What
the API does instead is never hand a secret back to the browser: read() for
the UI masks them (``masked()``), and a form that leaves a secret field empty
keeps the stored one (``merge_secrets()``).
"""
from __future__ import annotations

import re
from typing import Dict, List, Literal, Optional, Type, TypeVar

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.models.admin import SystemSetting

SECRET = "********"


class AuthSettings(BaseModel):
    # Minutes without a request before a session ends, and the most a session
    # can last however busy it is. A wall display that refreshes every 30 s
    # never goes idle, so the absolute limit is what eventually logs it out.
    idle_minutes: int = Field(default=480, ge=5, le=10080)
    absolute_hours: int = Field(default=168, ge=1, le=2160)
    lockout_failures: int = Field(default=5, ge=3, le=50)
    lockout_minutes: int = Field(default=15, ge=1, le=1440)


class LdapSettings(BaseModel):
    enabled: bool = False
    # One per line; the next one is tried when one cannot be reached
    urls: List[str] = []
    # user@domain is what is bound with (AD's userPrincipalName form)
    domain: str = ""
    base_dn: str = ""
    # Allowed groups (DN). Empty = anyone in the directory. Nested groups count.
    groups: List[str] = []
    # Role given to a directory account on its first login. "guest" means an
    # administrator has to grant one before it sees anything.
    default_role: Literal["guest", "viewer", "operator"] = "guest"
    timeout: int = Field(default=5, ge=1, le=60)
    ca_pem: str = ""
    # Shown next to the username on the login page
    login_hint: str = ""

    @field_validator("urls", "groups", mode="before")
    @classmethod
    def _lines(cls, value):
        if isinstance(value, str):
            value = value.splitlines()
        return [v.strip() for v in value or [] if v and v.strip()]

    @field_validator("urls")
    @classmethod
    def _ldap_urls(cls, value: List[str]) -> List[str]:
        for url in value:
            if not re.match(r"^ldaps?://[^/\s]+$", url, re.I):
                raise ValueError(f"{url}：格式應為 ldaps://主機:636 或 ldap://主機:389")
        return value


class Channel(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9]{1,32}$")
    name: str = Field(min_length=1, max_length=100)
    type: Literal["teams", "webhook", "email"]
    enabled: bool = True
    # Which events go to this channel
    events: List[Literal["alert_new", "alert_resolved", "health_failed", "job_finished"]] = [
        "alert_new", "alert_resolved", "health_failed"]
    # Alerts below this severity are not sent here (health and job events
    # always are)
    min_severity: Literal["info", "warning", "critical"] = "warning"
    # teams / webhook
    url: str = ""
    # webhook: an HMAC-SHA256 of the body is sent in X-Mf-Topomon-Signature
    secret: str = ""
    # email
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["none", "starttls", "ssl"] = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_to: List[str] = []

    @field_validator("mail_to", mode="before")
    @classmethod
    def _addresses(cls, value):
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        return [v.strip() for v in value or [] if v and v.strip()]

    @field_validator("url")
    @classmethod
    def _http(cls, value: str) -> str:
        if value and not re.match(r"^https?://", value):
            raise ValueError("網址要以 https:// 或 http:// 開頭")
        return value.strip()


DEFAULT_TEMPLATES = {
    "alert_new": {
        "title": "[{severity_label}] {device}：{type_label}",
        "body": "{message}\n時間：{time}\n{url}",
    },
    "alert_resolved": {
        "title": "[恢復] {device}：{type_label}",
        "body": "{message}\n持續：{duration}\n{url}",
    },
    "health_failed": {
        "title": "[系統] 健康檢查有 {count} 項異常",
        "body": "{items}\n{url}",
    },
    "job_finished": {
        "title": "[系統] {job_label}{result_label}",
        "body": "{summary}\n{url}",
    },
}


class NotificationSettings(BaseModel):
    enabled: bool = True
    channels: List[Channel] = []
    templates: Dict[str, Dict[str, str]] = Field(default_factory=lambda: {
        k: dict(v) for k, v in DEFAULT_TEMPLATES.items()})
    # More events than this in one dispatch round become one digest message,
    # so a core switch going down and taking 200 devices with it is one Teams
    # post, not 200.
    digest_threshold: int = Field(default=5, ge=1, le=100)
    # The address of this UI, used for the link in a message. Empty = no link.
    public_url: str = ""
    # Alerts that are not the root cause of an outage (details.is_root_cause
    # false, set by the topology impact analysis) are left out.
    root_cause_only: bool = False


class BackupSettings(BaseModel):
    enabled: bool = True
    frequency: Literal["daily", "weekly"] = "daily"
    weekday: int = Field(default=0, ge=0, le=6)  # 0 = Monday
    time: str = Field(default="02:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    keep: int = Field(default=14, ge=1, le=365)


class HealthSettings(BaseModel):
    enabled: bool = True
    interval_hours: int = Field(default=6, ge=1, le=168)
    # Notify only when something is wrong (the default), or every time
    notify_ok: bool = False
    # Warn this many days before the HTTPS certificate expires
    cert_warn_days: int = Field(default=30, ge=1, le=365)
    # Warn when a filesystem the system writes to is fuller than this
    disk_warn_percent: int = Field(default=85, ge=50, le=99)
    # Warn when the newest backup is older than this
    backup_max_age_hours: int = Field(default=48, ge=1, le=2160)


class AuditSettings(BaseModel):
    retention_days: int = Field(default=365, ge=30, le=3650)


SECTIONS: Dict[str, Type[BaseModel]] = {
    "auth": AuthSettings,
    "ldap": LdapSettings,
    "notifications": NotificationSettings,
    "backup": BackupSettings,
    "health": HealthSettings,
    "audit": AuditSettings,
}

T = TypeVar("T", bound=BaseModel)


async def load(db: AsyncSession, section: str) -> BaseModel:
    model = SECTIONS[section]
    row = await db.get(SystemSetting, section)
    if row is None or not isinstance(row.value, dict):
        return model()
    try:
        return model.model_validate(row.value)
    except ValueError:
        # A value stored by a version with different rules. Fall back to the
        # defaults rather than make the whole back office unusable; the next
        # save writes a valid one.
        return model()


async def save(db: AsyncSession, section: str, value: BaseModel, by: str | None) -> None:
    row = await db.get(SystemSetting, section)
    data = value.model_dump(mode="json")
    if row is None:
        db.add(SystemSetting(key=section, value=data, updated_by=by, updated_at=utcnow()))
    else:
        row.value = data
        row.updated_by = by
        row.updated_at = utcnow()
    await db.commit()


async def updated(db: AsyncSession, section: str) -> Optional[dict]:
    row = await db.get(SystemSetting, section)
    if row is None:
        return None
    return {"at": row.updated_at.isoformat() if row.updated_at else None, "by": row.updated_by}


_SECRET_FIELDS = {"smtp_password", "secret"}


def masked(value: BaseModel) -> dict:
    """The section for the browser: secrets replaced by a placeholder."""
    data = value.model_dump(mode="json")
    if "channels" in data:
        for ch in data["channels"]:
            for f in _SECRET_FIELDS:
                if ch.get(f):
                    ch[f] = SECRET
            # A Teams or Slack-style URL *is* the credential: anybody who has
            # it can post to the channel. Show enough to recognise it.
            if ch.get("url") and ch["type"] == "teams":
                ch["url"] = _mask_url(ch["url"])
    return data


def _mask_url(url: str) -> str:
    m = re.match(r"^(https?://[^/]+/)(.*)$", url)
    if not m or len(m.group(2)) < 12:
        return url
    return m.group(1) + m.group(2)[:6] + SECRET + m.group(2)[-4:]


def merge_secrets(new: NotificationSettings, old: NotificationSettings) -> NotificationSettings:
    """A secret sent back as the placeholder (or a masked URL) keeps its stored value."""
    previous = {c.id: c for c in old.channels}
    for ch in new.channels:
        before = previous.get(ch.id)
        if before is None:
            if SECRET in (ch.smtp_password + ch.secret + ch.url):
                raise ValueError(f"通道「{ch.name}」的密碼或網址還是遮蔽的值，請重新輸入")
            continue
        if ch.smtp_password == SECRET:
            ch.smtp_password = before.smtp_password
        if ch.secret == SECRET:
            ch.secret = before.secret
        if SECRET in ch.url:
            ch.url = before.url
    return new
