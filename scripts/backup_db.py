"""Nightly Postgres backup — pg_dump to backups/ with 14-day retention.

Called by the bot's daily backup loop (bot_core/tasks.py) so no extra
scheduling infrastructure is needed. Can also run manually:
    python scripts/backup_db.py

Parses DATABASE_URL from the environment/.env. Backs up the whole database
in custom format (compressed, pg_restore-compatible).
"""

import os
import subprocess
import sys
from datetime import datetime
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(BASE, "backups")
RETENTION_DAYS = 14

PG_DUMP_CANDIDATES = [
    r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe",
    r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe",
    r"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe",
]


def _load_database_url():
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    env_path = os.path.join(BASE, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip()
    return None


def _find_pg_dump():
    for candidate in PG_DUMP_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    found = subprocess.run(["where", "pg_dump"], capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip().splitlines()[0]
    return None


def run_backup():
    url = _load_database_url()
    if not url:
        return {"ok": False, "error": "DATABASE_URL not set"}
    pg_dump = _find_pg_dump()
    if not pg_dump:
        return {"ok": False, "error": "pg_dump.exe not found"}

    parsed = urlparse(url)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(BACKUP_DIR, f"skillsync_{stamp}.dump")

    cmd = [
        pg_dump,
        "--format=custom",
        "--no-password",
        f"--host={parsed.hostname or 'localhost'}",
        f"--port={parsed.port or 5432}",
        f"--username={parsed.username or 'skillsync'}",
        f"--file={out_path}",
        (parsed.path or "/skillsync").lstrip("/"),
    ]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()[:500]}
    size = os.path.getsize(out_path)
    return {"ok": True, "file": out_path, "size_bytes": size}


def prune_old_backups():
    """Delete backups older than RETENTION_DAYS."""
    cutoff = datetime.now().timestamp() - RETENTION_DAYS * 86400
    removed = 0
    for name in os.listdir(BACKUP_DIR):
        if not name.startswith("skillsync_") or not name.endswith(".dump"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        if os.path.getmtime(path) < cutoff:
            os.remove(path)
            removed += 1
    return removed


if __name__ == "__main__":
    result = run_backup()
    if result.get("ok"):
        removed = prune_old_backups()
        print(
            f"[Backup] OK {result['file']} ({result['size_bytes']} bytes, "
            f"{removed} old backups pruned)"
        )
    else:
        print(f"[Backup] FAILED: {result.get('error')}")
        sys.exit(1)
