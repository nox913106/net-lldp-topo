#!/usr/bin/env python3
"""
The careful parts of `mf-topomon run-requests`, which runs as root.

Root reads files that the service account wrote, in a directory the service
account owns. Everything here assumes that account is hostile:

  * the request directory is opened once with O_NOFOLLOW|O_DIRECTORY and every
    file in it is opened relative to that descriptor with O_NOFOLLOW, so a
    symlink planted in place of the directory or of a file is never followed
    (root reading /etc/shadow "as a request", or deleting files elsewhere);
  * a request must be a regular file, owned by the service account, small,
    and valid JSON of exactly the expected shape; anything else is deleted
    (so the path unit does not fire on it forever) and reported;
  * every argument is checked against a fixed pattern here, and again by the
    shell before it is used.

Standard library only, run with `python3 -I`: no site-packages, no current
directory on sys.path, nothing from the environment.

  take <request-dir> <uid> <stage-dir>    print the oldest request as JSON
  write-json <path>                        stdin JSON -> path, atomically
  merge-json <path>                        merge stdin JSON into path
  tls-info <cert.pem>                      certificate facts as JSON
  git-info <checkout> [<since-ref>]        version facts as JSON
"""
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone

KINDS = {"backup", "delete-backup", "restore", "upgrade", "check-update", "tls", "doctor"}
REQ_NAME = re.compile(r"^(\d{8}-\d{6}-[a-f0-9]{8})\.json$")
BACKUP_NAME = re.compile(r"^mf-topomon-\d{8}-\d{6}\.sql\.gz$")
BY = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")
MAX_REQUEST = 64 * 1024
MAX_FILE = 512 * 1024
TLS_FILES = ("cert.pem", "key.pem")


def _read_owned(dir_fd: int, name: str, uid: int, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{name}: not a regular file")
        if st.st_uid != uid:
            raise ValueError(f"{name}: not owned by the service account")
        if st.st_size > limit:
            raise ValueError(f"{name}: too large")
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise ValueError(f"{name}: too large")
        return data
    finally:
        os.close(fd)


def _validate(req: dict, job_id: str) -> dict:
    if not isinstance(req, dict):
        raise ValueError("request is not an object")
    if req.get("id") != job_id:
        raise ValueError("id does not match the file name")
    kind = req.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    by = str(req.get("by") or "web")
    if not BY.match(by):
        by = "web"
    raw = req.get("args") or {}
    if not isinstance(raw, dict):
        raise ValueError("args is not an object")
    args = {}
    if kind == "backup":
        keep = raw.get("keep", 14)
        if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= 365:
            raise ValueError("keep must be 1-365")
        trigger = raw.get("trigger", "manual")
        if trigger not in ("manual", "scheduled"):
            raise ValueError("bad trigger")
        args = {"keep": keep, "trigger": trigger}
    elif kind in ("delete-backup", "restore"):
        name = raw.get("name")
        if not isinstance(name, str) or not BACKUP_NAME.match(name):
            raise ValueError("bad backup name")
        args = {"name": name}
    return {"id": job_id, "kind": kind, "by": by, "args": args,
            "requested_at": str(req.get("requested_at") or "")[:40]}


def _remove_files_dir(dir_fd: int, name: str) -> None:
    try:
        sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        try:
            os.unlink(name, dir_fd=dir_fd)  # a symlink or a file in its place
        except OSError:
            pass
        return
    try:
        for entry in os.listdir(sub):
            try:
                os.unlink(entry, dir_fd=sub)
            except OSError:
                pass
    finally:
        os.close(sub)
    try:
        os.rmdir(name, dir_fd=dir_fd)
    except OSError:
        pass


def take(req_dir: str, uid: int, stage_dir: str) -> int:
    try:
        dfd = os.open(req_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return 0
    try:
        st = os.fstat(dfd)
        if st.st_uid != uid:
            print(json.dumps({"error": "request directory is not owned by the service account"}))
            return 1
        names = sorted(os.listdir(dfd))
        for name in names:
            if not name.endswith(".json"):
                continue
            m = REQ_NAME.match(name)
            try:
                if not m:
                    raise ValueError(f"{name}: unexpected file name")
                raw = _read_owned(dfd, name, uid, MAX_REQUEST)
                req = _validate(json.loads(raw.decode("utf-8")), m.group(1))
                if req["kind"] == "tls":
                    files_dir = f"{req['id']}.files"
                    sub = os.open(files_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dfd)
                    try:
                        for fname in TLS_FILES:
                            data = _read_owned(sub, fname, uid, MAX_FILE)
                            out = os.open(os.path.join(stage_dir, fname),
                                          os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                            with os.fdopen(out, "wb") as fh:
                                fh.write(data)
                    finally:
                        os.close(sub)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                _discard(dfd, name, m.group(1) if m else None)
                print(json.dumps({"error": str(exc), "file": name,
                                  "id": m.group(1) if m else None}))
                return 0
            _discard(dfd, name, req["id"])
            print(json.dumps(req))
            return 0
        return 0
    finally:
        os.close(dfd)


def _discard(dfd: int, name: str, job_id) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass
    if job_id:
        _remove_files_dir(dfd, f"{job_id}.files")


def write_json(path: str, data: dict) -> None:
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def merge_json(path: str, data: dict) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            current = json.load(fh)
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    current.update(data)
    write_json(path, current)


def _openssl(*args: str) -> str:
    return subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=15).stdout


def tls_info(cert: str) -> dict:
    text = _openssl("x509", "-in", cert, "-noout", "-subject", "-issuer", "-startdate",
                    "-enddate", "-fingerprint", "-sha256", "-ext", "subjectAltName")

    def field(pattern):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""

    def when(value):
        try:
            return datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc).isoformat()
        except ValueError:
            return None

    subject, issuer = field(r"subject=(.+)"), field(r"issuer=(.+)")
    return {
        "subject": subject, "issuer": issuer,
        "not_before": when(field(r"notBefore=(.+)")), "not_after": when(field(r"notAfter=(.+)")),
        "fingerprint": field(r"Fingerprint=(.+)"),
        "san": field(r"Subject Alternative Name:\s*\n\s*(.+)"),
        "self_signed": bool(subject) and subject == issuer,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _git(checkout: str, *args: str) -> str:
    return subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True,
                          timeout=60).stdout.strip()


def _log(checkout: str, rev_range: str, limit: int = 30) -> list:
    out = []
    raw = _git(checkout, "log", f"-{limit}", "--format=%H%x1f%cI%x1f%s", rev_range)
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            out.append({"sha": parts[0][:10], "date": parts[1], "subject": parts[2][:200]})
    return out


def git_info(checkout: str, upstream: str = "") -> dict:
    head = _git(checkout, "rev-parse", "HEAD")
    data = {
        "commit": head[:10], "branch": _git(checkout, "rev-parse", "--abbrev-ref", "HEAD"),
        "date": _git(checkout, "log", "-1", "--format=%cI"),
        "subject": _git(checkout, "log", "-1", "--format=%s"),
        "remote": _git(checkout, "remote", "get-url", "origin"),
        "recent": _log(checkout, "HEAD", 15),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if upstream:
        data["upstream"] = _git(checkout, "rev-parse", upstream)[:10]
        data["behind"] = _log(checkout, f"HEAD..{upstream}", 100)
        data["checked_at"] = data["updated_at"]
    return data


def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "take" and len(argv) == 5:
        return take(argv[2], int(argv[3]), argv[4])
    if cmd == "write-json" and len(argv) == 3:
        write_json(argv[2], json.load(sys.stdin))
        return 0
    if cmd == "merge-json" and len(argv) == 3:
        merge_json(argv[2], json.load(sys.stdin))
        return 0
    if cmd == "tls-info" and len(argv) == 3:
        print(json.dumps(tls_info(argv[2]), ensure_ascii=False))
        return 0
    if cmd == "git-info" and len(argv) in (3, 4):
        print(json.dumps(git_info(argv[2], argv[3] if len(argv) == 4 else ""), ensure_ascii=False))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
