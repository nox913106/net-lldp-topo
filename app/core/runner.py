"""
The application's side of the privileged job runner.

The application asks; root does. A request is a small JSON file written to
<APP_DATA_DIR>/requests, which a systemd path unit notices and hands to
`mf-topomon run-requests` (scripts/mf-topomon.sh). That command validates
every field again - it trusts nothing the service account wrote - runs the
job, and records the outcome in APP_RUNNER_DIR, which root owns and the
service account can only read. So the account that can ask cannot forge an
answer, and the worst a compromised web process can do through this channel
is ask for one of the seven things on the list below.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_settings

KINDS = ("backup", "delete-backup", "restore", "upgrade", "check-update", "tls", "doctor")
BACKUP_NAME = re.compile(r"^mf-topomon-\d{8}-\d{6}\.sql\.gz$")
JOB_ID = re.compile(r"^\d{8}-\d{6}-[a-f0-9]{8}$")


class RunnerUnavailable(Exception):
    pass


def runner_dir() -> Optional[Path]:
    d = get_settings().app_runner_dir
    return Path(d) if d else None


def request_dir() -> Optional[Path]:
    d = get_settings().app_data_dir
    return Path(d) / "requests" if d else None


def backup_dir() -> Optional[Path]:
    d = get_settings().app_backup_dir
    return Path(d) if d else None


def available() -> Dict[str, Any]:
    """Whether jobs can be run here, and if not, why."""
    rd, qd = runner_dir(), request_dir()
    if rd is None or qd is None:
        return {"available": False, "reason": "not_configured"}
    if not rd.is_dir():
        return {"available": False, "reason": "runner_dir_missing"}
    beat = _read_json(rd / "runner.json")
    return {"available": True, "reason": None, "runner": beat}


def _read_json(path: Path) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def submit(kind: str, by: str, args: Optional[Dict[str, Any]] = None,
           files: Optional[Dict[str, bytes]] = None) -> str:
    """Queue a job. Returns its id. Raises RunnerUnavailable."""
    if kind not in KINDS:
        raise ValueError(kind)
    state = available()
    if not state["available"]:
        raise RunnerUnavailable(state["reason"])
    qd = request_dir()
    assert qd is not None
    qd.mkdir(mode=0o750, exist_ok=True)
    job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    if files:
        staged = qd / f"{job_id}.files"
        staged.mkdir(mode=0o700)
        for name, data in files.items():
            with open(staged / name, "wb") as fh:
                fh.write(data)
    body = {"id": job_id, "kind": kind, "args": args or {}, "by": by,
            "requested_at": datetime.now(timezone.utc).isoformat()}
    # Written under another name and renamed: the path unit fires on the
    # .json appearing, and must never see half a file
    tmp = qd / f".{job_id}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    os.replace(tmp, qd / f"{job_id}.json")
    return job_id


def pending() -> List[dict]:
    qd = request_dir()
    out = []
    if qd is None or not qd.is_dir():
        return out
    for p in sorted(qd.glob("*.json")):
        data = _read_json(p)
        if data:
            out.append({**data, "state": "queued"})
    return out


def job(job_id: str) -> Optional[dict]:
    if not JOB_ID.match(job_id or ""):
        return None
    rd = runner_dir()
    if rd is None:
        return None
    data = _read_json(rd / "jobs" / f"{job_id}.json")
    if data is None:
        for p in pending():
            if p.get("id") == job_id:
                return p
        return None
    log = rd / "jobs" / f"{job_id}.log"
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        data["log"] = text[-20000:]
    except OSError:
        data["log"] = ""
    return data


def jobs(limit: int = 50, kind: Optional[str] = None) -> List[dict]:
    rd = runner_dir()
    out = pending()
    if rd is not None and (rd / "jobs").is_dir():
        files = sorted((rd / "jobs").glob("*.json"), reverse=True)
        for p in files[: limit * 3]:
            data = _read_json(p)
            if data:
                out.append(data)
    if kind:
        out = [j for j in out if j.get("kind") == kind]
    out.sort(key=lambda j: j.get("id", ""), reverse=True)
    return out[:limit]


def busy() -> Optional[dict]:
    """A job that is queued or running, if any."""
    for j in jobs(limit=20):
        if j.get("state") in ("queued", "running"):
            return j
    return None


def status_file(name: str) -> Optional[dict]:
    """tls.json / version.json / runner.json, written by root."""
    rd = runner_dir()
    if rd is None or name not in ("tls.json", "version.json", "runner.json", "update.json"):
        return None
    return _read_json(rd / name)


def backups() -> List[dict]:
    bd = backup_dir()
    out: List[dict] = []
    if bd is None:
        return out
    try:
        entries = list(os.scandir(bd))
    except OSError:
        return out
    meta = (_read_json(runner_dir() / "backups.json") if runner_dir() else None) or {}
    for e in entries:
        if not BACKUP_NAME.match(e.name):
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        stamp = e.name[len("mf-topomon-"):-len(".sql.gz")]
        try:
            when = datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone().isoformat()
        except ValueError:
            when = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        info = meta.get(e.name) or {}
        out.append({
            "name": e.name, "size": st.st_size, "time": when,
            "readable": os.access(e.path, os.R_OK),
            "config": os.path.exists(os.path.join(bd, e.name[:-len(".sql.gz")] + "-config.tar.gz")),
            "trigger": info.get("trigger"), "by": info.get("by"),
        })
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def backup_path(name: str) -> Optional[Path]:
    bd = backup_dir()
    if bd is None or not BACKUP_NAME.match(name or ""):
        return None
    p = bd / name
    return p if p.is_file() else None


def disk_usage(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        u = shutil.disk_usage(probe)
    except OSError:
        return None
    return {"path": str(path), "total": u.total, "free": u.free, "used": u.used,
            "percent": round(u.used * 100 / u.total, 1) if u.total else 0}


def wait_for(job_id: str, seconds: float = 5.0) -> Optional[dict]:
    """Poll briefly for a job to leave the queue - used by tests and the UI."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        j = job(job_id)
        if j and j.get("state") not in ("queued",):
            return j
        time.sleep(0.2)
    return job(job_id)
