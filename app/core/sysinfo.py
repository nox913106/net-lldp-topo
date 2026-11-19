"""
System information for the back office: read live from the host every time.

Everything here is readable by the unprivileged service account (/proc,
/etc/os-release, `systemctl show`, the package metadata of its own
environment). Something that is not shows as unavailable rather than failing
the page.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import runner

ROOT = Path(__file__).resolve().parent.parent.parent
UNITS = ("mf-topomon-app.service", "mf-topomon-collector.service",
         "mf-topomon-runner.path", "nginx.service", "postgresql.service")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _os_release() -> str:
    data = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    return data.get("PRETTY_NAME") or platform.platform()


def _meminfo() -> Dict[str, int]:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        m = re.match(r"^(\w+):\s+(\d+) kB", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine()


def _uptime() -> Optional[float]:
    try:
        return float(_read("/proc/uptime").split()[0])
    except (IndexError, ValueError):
        return None


def _units() -> List[Dict[str, Any]]:
    if shutil.which("systemctl") is None:
        return []
    out = []
    for unit in UNITS:
        try:
            res = subprocess.run(
                ["systemctl", "show", unit, "--no-pager", "-p",
                 "ActiveState,SubState,UnitFileState,MainPID,ActiveEnterTimestamp,MemoryCurrent,NRestarts"],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        props = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
        mem = props.get("MemoryCurrent", "")
        out.append({
            "unit": unit,
            "state": f"{props.get('ActiveState', '?')} ({props.get('SubState', '?')})",
            "ok": props.get("ActiveState") == "active",
            "enabled": props.get("UnitFileState", ""),
            "pid": props.get("MainPID") if props.get("MainPID") not in (None, "0") else "",
            "since": props.get("ActiveEnterTimestamp", ""),
            "memory": int(mem) if mem.isdigit() else None,
            "restarts": props.get("NRestarts", ""),
        })
    return out


def _requirements() -> Dict[str, str]:
    pins = {}
    for line in _read(str(ROOT / "requirements.txt")).splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\\;]+)", line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    direct = set()
    for line in _read(str(ROOT / "requirements.in")).splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line.strip())
        if m and not line.strip().startswith("#"):
            direct.add(m.group(1).lower().replace("_", "-"))
    return {"pins": pins, "direct": direct}  # type: ignore[return-value]


def _packages() -> List[Dict[str, Any]]:
    req = _requirements()
    pins, direct = req["pins"], req["direct"]
    out = []
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        key = name.lower().replace("_", "-")
        if key in ("pip", "setuptools", "wheel"):
            continue
        version = dist.version
        out.append({
            "name": name, "version": version, "direct": key in direct,
            "required": pins.get(key), "mismatch": key in pins and pins[key] != version,
            "license": (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "")[:60],
            "summary": (dist.metadata.get("Summary") or "")[:120],
        })
    out.sort(key=lambda p: (not p["direct"], p["name"].lower()))
    return out


async def collect(db: AsyncSession) -> Dict[str, Any]:
    started = time.monotonic()
    env = get_settings()
    mem = _meminfo()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)

    database: Dict[str, Any] = {"url_kind": env.database_url.split(":", 1)[0]}
    try:
        if db.bind.dialect.name == "postgresql":
            database["version"] = (await db.execute(text("SHOW server_version"))).scalar_one()
            database["size"] = (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
        else:
            database["version"] = "SQLite " + (await db.execute(text("select sqlite_version()"))).scalar_one()
    except Exception as exc:  # pragma: no cover
        database["error"] = str(exc)[:200]
    counts = {}
    from app.models.admin import AuditLog, User
    from app.models.alert import Alert
    from app.models.device import Device
    from app.models.link import MergedLink, RawLink
    for label, model in (("devices", Device), ("raw_links", RawLink), ("merged_links", MergedLink),
                         ("alerts", Alert), ("users", User), ("audit_log", AuditLog)):
        try:
            counts[label] = (await db.execute(select(func.count()).select_from(model))).scalar_one()
        except Exception:  # pragma: no cover
            counts[label] = None
    database["rows"] = counts

    disks = []
    for label, path in (("程式", str(ROOT)), ("資料", env.app_data_dir), ("備份", env.app_backup_dir)):
        if path:
            u = runner.disk_usage(Path(path))
            if u:
                disks.append({"label": label, **u})

    pkgs = _packages()
    version = runner.status_file("version.json")
    info = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "hardware": {
            "hostname": socket.gethostname(),
            "cpu": _cpu_model(), "cpu_count": os.cpu_count(),
            "load": [round(x, 2) for x in load],
            "memory_total": mem.get("MemTotal"), "memory_available": mem.get("MemAvailable"),
            "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree"),
        },
        "os": {
            "name": _os_release(), "kernel": platform.release(), "arch": platform.machine(),
            "uptime_seconds": _uptime(), "timezone": time.strftime("%Z %z"),
        },
        "runtime": {
            "python": sys.version.split()[0], "executable": sys.executable,
            "app_dir": str(ROOT), "data_dir": env.app_data_dir or "(程式目錄)",
            "backup_dir": env.app_backup_dir or "(未設定)",
            "runner": runner.available(), "pid": os.getpid(),
            "user": _whoami(),
        },
        "config": {
            "collector_interval": env.collector_interval,
            "collector_fast_interval": env.collector_fast_interval,
            "collector_concurrent": env.collector_concurrent,
            "collector_shards": env.collector_shards,
            "collector_trap_enabled": env.collector_trap_enabled,
            "snmp_timeout": env.snmp_timeout, "snmp_retries": env.snmp_retries,
            "alert_offline_confirm_cycles": env.alert_offline_confirm_cycles,
            "log_export_enabled": env.log_export_enabled,
        },
        "version": version,
        "database": database,
        "disks": disks,
        "services": _units(),
        "packages": pkgs,
        "package_mismatches": sum(1 for p in pkgs if p["mismatch"]),
    }
    info["elapsed"] = round(time.monotonic() - started, 2)
    return info


def _whoami() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):  # pragma: no cover
        return str(os.getuid())
