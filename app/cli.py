"""
Host-side commands that need the application's database.

Run by scripts/mf-topomon.sh as the service account:

    python -m app.cli --env-file /etc/mf-topomon/mf-topomon.env <command> ...

  ensure-admin <user>          create the first administrator if there is no
                               active one; prints the generated password
  set-password <user> [pw]     create or reset a local administrator
                               (generated password when none is given)
  import-htpasswd <file|->     bring the proxy's old basic-auth account over
                               with its existing password ($apr1$ hash)
  collector-status             the collector report doctor needs, as JSON,
                               straight from the database (the HTTP API now
                               requires a login)
  users                        list accounts

The password is never put on a command line by the shell script (it would
show in `ps`): a generated one is printed here, one given by the operator is
read from stdin when the argument is "-".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import string
import sys


def _load_env(path: str) -> None:
    """KEY=VALUE lines, the same file systemd reads. Never executed as shell."""
    if not path:
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def _generate() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(20))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw


async def _main(args) -> int:
    from sqlalchemy import func, select

    from app.core import auth
    from app.core.passwords import password_problem
    from app.db.database import async_session_maker, init_db
    from app.models.admin import User

    await init_db()
    async with async_session_maker() as db:
        if args.command == "ensure-admin":
            if await auth.count_active_admins(db) > 0:
                print("exists")
                return 0
            password = _generate()
            await auth.create_local_user(db, args.username, password, "admin",
                                         display_name="Administrator", must_change=True)
            print(f"created {auth.ldap_auth.clean_username(args.username)} {password}")
            return 0

        if args.command == "set-password":
            password = args.password
            if password == "-":
                password = sys.stdin.readline().rstrip("\n")
            generated = not password
            if generated:
                password = _generate()
            else:
                problem = password_problem(password, args.username)
                if problem:
                    print(problem, file=sys.stderr)
                    return 2
            user, created = await auth.create_local_user(
                db, args.username, password, "admin", must_change=generated)
            await auth.end_all_sessions(db, user.id)
            print(f"{'created' if created else 'reset'} {user.username} {password if generated else '(given)'}")
            return 0

        if args.command == "import-htpasswd":
            try:
                if args.file == "-":
                    # Handed over by root on stdin: the file itself is not
                    # readable by the service account (/dev/stdin would
                    # reopen it by path and be refused the same way)
                    lines = [line.strip() for line in sys.stdin if ":" in line]
                else:
                    with open(args.file, encoding="utf-8") as fh:
                        lines = [line.strip() for line in fh if ":" in line]
            except OSError as exc:
                print(f"cannot read {args.file}: {exc}", file=sys.stderr)
                return 2
            imported = 0
            for line in lines:
                name, _, hashed = line.partition(":")
                if not hashed.startswith("$apr1$"):
                    continue
                exists = (await db.execute(select(User).where(
                    func.lower(User.username) == name.lower()))).scalar_one_or_none()
                if exists is not None:
                    continue
                await auth.create_local_user(db, name, hashed, "admin", display_name=name)
                imported += 1
            print(f"imported {imported}")
            return 0

        if args.command == "users":
            for u in (await db.execute(select(User).order_by(User.username))).scalars():
                print(f"{u.username}\t{u.source}\t{u.role}\t{'active' if u.is_active else 'disabled'}")
            return 0

        if args.command == "collector-status":
            from app.api.collector import collector_status
            status = await collector_status(db)
            print(json.dumps(status.model_dump(mode="json") if hasattr(status, "model_dump") else status,
                             default=str))
            return 0
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("--env-file", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ensure-admin")
    p.add_argument("username", nargs="?", default="admin")
    p = sub.add_parser("set-password")
    p.add_argument("username", nargs="?", default="admin")
    p.add_argument("password", nargs="?", default="")
    p = sub.add_parser("import-htpasswd")
    p.add_argument("file")
    sub.add_parser("users")
    sub.add_parser("collector-status")
    args = parser.parse_args(argv)
    _load_env(args.env_file)
    # Quiet: the shell script parses what is printed
    import logging
    logging.disable(logging.WARNING)
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
