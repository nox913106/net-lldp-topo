"""
The back office's health check: what the application itself can see.

`mf-topomon doctor` (run as root, from the host or through the runner) checks
the host - units, nginx, the database server. This checks what the running
application knows and doctor cannot easily tell: whether the collectors are
reporting, whether the data is fresh, whether the backups are recent, whether
the certificate is about to expire and whether notifications are actually
getting out. Each item is ok / warn / fail / info, with a one-line hint.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import runner, sysconf
from app.core.clock import utcnow
from app.core.lease import FLEET_LEASE, MEMBER_PREFIX, read_leases
from app.models.admin import NotificationDelivery, User
from app.models.device import Device

LEVELS = ("ok", "info", "warn", "fail")


def _item(group: str, name: str, level: str, detail: str, hint: str = "") -> Dict[str, str]:
    return {"group": group, "name": name, "level": level, "detail": detail, "hint": hint}


def _aware(v: Optional[datetime]) -> Optional[datetime]:
    if v is not None and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _ago(v: Optional[datetime]) -> str:
    if v is None:
        return "從未"
    s = int((utcnow() - _aware(v)).total_seconds())  # type: ignore[operator]
    if s < 120:
        return f"{s} 秒前"
    if s < 7200:
        return f"{s // 60} 分鐘前"
    if s < 172800:
        return f"{s // 3600} 小時前"
    return f"{s // 86400} 天前"


def unit_state(unit: str) -> Optional[str]:
    if shutil.which("systemctl") is None:
        return None
    try:
        out = subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                             text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


async def run_checks(db: AsyncSession) -> List[Dict[str, str]]:
    conf: sysconf.HealthSettings = await sysconf.load(db, "health")  # type: ignore[assignment]
    env = get_settings()
    items: List[Dict[str, str]] = []

    # -- Core
    try:
        await db.execute(text("SELECT 1"))
        items.append(_item("核心", "資料庫", "ok", "連線正常"))
    except Exception as exc:  # pragma: no cover - the request needed it already
        items.append(_item("核心", "資料庫", "fail", str(exc)[:200], "sudo systemctl status postgresql"))

    leases = await read_leases()
    members = [lease for lease in leases if lease.name.startswith(MEMBER_PREFIX)]
    live = [lease for lease in members if lease.live]
    if not members:
        items.append(_item("核心", "收集器", "fail", "沒有任何收集器回報過",
                           "sudo systemctl status mf-topomon-collector"))
    elif not live:
        newest = max((lease.renewed_at for lease in members if lease.renewed_at), default=None)
        items.append(_item("核心", "收集器", "fail", f"沒有收集器在回報（最後一次 {_ago(newest)}）",
                           "sudo systemctl restart mf-topomon-collector"))
    else:
        items.append(_item("核心", "收集器", "ok", f"{len(live)} 個在回報"))
    fleet = next((lease for lease in leases if lease.name == FLEET_LEASE), None)
    if live and fleet is not None and not fleet.live:
        items.append(_item("核心", "全網工作", "warn", "沒有收集器持有全網工作（拓撲合併與告警檢查暫停）",
                           "sudo mf-topomon logs collector"))

    total = (await db.execute(select(func.count()).select_from(Device))).scalar_one()
    if total == 0:
        items.append(_item("資料", "設備", "info", "還沒有任何設備", "到「設備」頁新增第一台交換機"))
    else:
        newest = (await db.execute(select(func.max(Device.last_seen)))).scalar_one()
        stale_after = timedelta(seconds=max(env.collector_interval, env.collector_fast_interval or 0) * 3)
        if newest is None:
            items.append(_item("資料", "輪詢", "warn", f"{total} 台設備都還沒有被輪詢到"))
        elif utcnow() - _aware(newest) > stale_after:  # type: ignore[operator]
            items.append(_item("資料", "輪詢", "fail", f"最新的輪詢結果是 {_ago(newest)}，資料已經停止更新",
                               "sudo mf-topomon logs collector"))
        else:
            offline = (await db.execute(select(func.count()).select_from(Device)
                                        .where(Device.status == "offline"))).scalar_one()
            items.append(_item("資料", "輪詢", "ok",
                               f"{total} 台設備，最新結果 {_ago(newest)}" + (f"，{offline} 台離線" if offline else "")))

    # -- Accounts
    local_admins = (await db.execute(select(func.count()).select_from(User).where(
        User.role == "admin", User.source == "local", User.is_active.is_(True)))).scalar_one()
    if local_admins == 0:
        items.append(_item("帳號", "本機管理員", "warn",
                           "沒有啟用中的本機管理員帳號：目錄伺服器故障時沒有人登入得了後台",
                           "sudo mf-topomon password"))
    else:
        items.append(_item("帳號", "本機管理員", "ok", f"{local_admins} 個"))
    if env.app_secret_key in ("", "change-this-to-random-string"):
        items.append(_item("帳號", "APP_SECRET_KEY", "warn", "還是範例值", "sudo mf-topomon install"))

    # -- Operations (native install only)
    state = runner.available()
    if not state["available"]:
        items.append(_item("維運", "背景執行器", "info",
                           "這個部署沒有背景執行器（Docker 或開發環境），備份、升級、憑證請用主機指令"))
    else:
        unit = unit_state("mf-topomon-runner.path")
        if unit is not None and unit != "active":
            # `install` rather than `systemctl enable`: after an upgrade done by
            # an older mf-topomon the unit file does not exist yet
            items.append(_item("維運", "背景執行器", "fail", f"mf-topomon-runner.path 是 {unit}，網頁上的備份、升級、憑證都不會執行",
                               "sudo mf-topomon install"))
        else:
            stuck = [j for j in runner.pending()
                     if _aware(datetime.fromisoformat(j["requested_at"])) < utcnow() - timedelta(minutes=10)]  # type: ignore[operator]
            if stuck:
                items.append(_item("維運", "背景執行器", "fail", f"{len(stuck)} 個工作排隊超過 10 分鐘沒有被執行",
                                   "sudo systemctl restart mf-topomon-runner.path"))
            else:
                items.append(_item("維運", "背景執行器", "ok", "待命中"))

        dumps = runner.backups()
        if not dumps:
            items.append(_item("維運", "備份", "warn", "還沒有任何備份", "到「備份還原」按「立即備份」"))
        else:
            newest_b = datetime.fromisoformat(dumps[0]["time"])
            age = utcnow() - _aware(newest_b)  # type: ignore[operator]
            level = "warn" if age > timedelta(hours=conf.backup_max_age_hours) else "ok"
            items.append(_item("維運", "備份", level,
                               f"最近一份 {_ago(newest_b)}，共 {len(dumps)} 份",
                               "檢查備份排程與背景執行器" if level == "warn" else ""))

        tls = runner.status_file("tls.json")
        if tls and tls.get("not_after"):
            try:
                end = datetime.fromisoformat(tls["not_after"])
            except ValueError:
                end = None
            if end is not None:
                days = (_aware(end) - utcnow()).days  # type: ignore[operator]
                if days < 0:
                    items.append(_item("維運", "HTTPS 憑證", "fail", f"已在 {-days} 天前過期",
                                       "到「HTTPS 憑證」上傳新的憑證"))
                elif days <= conf.cert_warn_days:
                    items.append(_item("維運", "HTTPS 憑證", "warn", f"{days} 天後到期",
                                       "到「HTTPS 憑證」上傳新的憑證"))
                else:
                    items.append(_item("維運", "HTTPS 憑證", "ok",
                                       f"{days} 天後到期" + ("（自簽）" if tls.get("self_signed") else "")))

    for label, path in (("資料目錄", env.app_data_dir), ("備份目錄", env.app_backup_dir)):
        if not path:
            continue
        u = runner.disk_usage(Path(path))
        if u is None:
            continue
        level = "fail" if u["percent"] >= 97 else "warn" if u["percent"] >= conf.disk_warn_percent else "ok"
        items.append(_item("維運", f"磁碟（{label}）", level,
                           f"{u['percent']}% 已用，剩 {u['free'] / 1e9:.1f} GB",
                           "清出空間或減少備份保留份數" if level != "ok" else ""))

    # -- Notifications
    nconf: sysconf.NotificationSettings = await sysconf.load(db, "notifications")  # type: ignore[assignment]
    channels = [c for c in nconf.channels if c.enabled]
    if not nconf.enabled or not channels:
        items.append(_item("通知", "告警通知", "info", "沒有啟用的通知通道，告警只會顯示在畫面上",
                           "到「通知」新增 Teams、Webhook 或 Email"))
    else:
        since = utcnow() - timedelta(hours=24)
        failed = (await db.execute(select(func.count()).select_from(NotificationDelivery).where(
            NotificationDelivery.status == "failed", NotificationDelivery.created_at >= since))).scalar_one()
        retrying = (await db.execute(select(func.count()).select_from(NotificationDelivery).where(
            NotificationDelivery.status == "pending", NotificationDelivery.attempts > 0))).scalar_one()
        if failed:
            items.append(_item("通知", "告警通知", "fail", f"過去 24 小時有 {failed} 則送不出去",
                               "到「通知」看發送紀錄的錯誤訊息"))
        elif retrying:
            items.append(_item("通知", "告警通知", "warn", f"{retrying} 則正在重試"))
        else:
            items.append(_item("通知", "告警通知", "ok", f"{len(channels)} 個通道"))
    return items


def summarize(items: List[Dict[str, str]]) -> Dict[str, Any]:
    counts = {lvl: sum(1 for i in items if i["level"] == lvl) for lvl in LEVELS}
    worst = "fail" if counts["fail"] else "warn" if counts["warn"] else "ok"
    return {"counts": counts, "worst": worst}
