"""Housekeeping: delete data past its retention period, and back up / restore the database.

Retention (days; set in .env):
  TEAMPULSE_AUDIT_DAYS   180   activity log entries
  TEAMPULSE_CACHE_DAYS    30   cached AI responses (they can quote chat messages)
  ended sessions and expired sign-in locks are removed after 7 days.

Backups use SQLite's online backup API, so they are consistent even while the app, bot and pollers are
writing. Each copy is integrity-checked; the newest TEAMPULSE_BACKUP_KEEP (14) are kept. The search index
isn't backed up: it is rebuilt from the database automatically.

    python -m teampulse.maintenance purge
    python -m teampulse.maintenance backup [--to data/backups]
    python -m teampulse.maintenance verify data/backups/teampulse-20261028-101500.db
    python -m teampulse.maintenance restore data/backups/teampulse-20261028-101500.db --yes   # app stopped
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teampulse import config, db

log = logging.getLogger("teampulse.maintenance")
ENDED_SESSION_DAYS = 7


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def purge(conn) -> dict:
    """Delete what has passed its retention period. Safe to run any time; returns rows deleted per table."""
    audit_days = config.get_int("TEAMPULSE_AUDIT_DAYS", 180)
    cache_days = config.get_int("TEAMPULSE_CACHE_DAYS", 30)
    now = _iso(0)
    with conn:
        counts = {
            "sessions": conn.execute(
                "DELETE FROM sessions WHERE expires_at < ? OR revoked_at < ?",
                (_iso(ENDED_SESSION_DAYS), _iso(ENDED_SESSION_DAYS))).rowcount,
            "login_throttle": conn.execute(
                "DELETE FROM login_throttle WHERE locked_until IS NOT NULL AND locked_until < ?", (now,)).rowcount,
            "audit_log": conn.execute("DELETE FROM audit_log WHERE at < ?", (_iso(audit_days),)).rowcount,
            "llm_cache": conn.execute("DELETE FROM llm_cache WHERE created_at < ?", (_iso(cache_days),)).rowcount,
        }
    if any(counts.values()):
        log.info("Retention purge: %s", counts)
    return counts


def _raw(path: str) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=db.BUSY_SECONDS)


def verify(path: str) -> dict:
    """Open a database file read-only and check it. Raises ValueError if it is damaged."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)   # also correct on Windows
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"{path} failed its integrity check: {result}")
        tables = {"events", "users", "audit_log"}
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(tables)}
    finally:
        conn.close()


def backup(db_path: str | None = None, dest: str | None = None, keep: int | None = None) -> Path:
    db_path = db_path or config.DB_PATH
    folder = Path(dest or config.get("TEAMPULSE_BACKUP_DIR", "data/backups"))
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = folder / f"teampulse-{stamp}.db"
    source, copy = _raw(db_path), sqlite3.connect(target)
    try:
        source.backup(copy)                  # consistent snapshot, even while others write
    finally:
        copy.close()
        source.close()
    verify(str(target))
    keep = keep if keep is not None else config.get_int("TEAMPULSE_BACKUP_KEEP", 14)
    for old in sorted(folder.glob("teampulse-*.db"))[:-keep or None]:
        old.unlink()
    log.info("Backup written to %s", target)
    return target


def restore(backup_path: str, db_path: str | None = None) -> dict:
    """Replace the live database with a verified backup. Stop the app, bot and pollers first."""
    db_path = db_path or config.DB_PATH
    counts = verify(backup_path)             # never restore a damaged copy
    if Path(db_path).exists():
        backup(db_path, str(Path(db_path).parent / "before-restore"), keep=5)   # undo is one restore away
    source, target = _raw(backup_path), _raw(db_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    with _raw(db_path) as conn:              # sessions in a restored copy must not be reusable
        conn.execute("UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL", (_iso(0),))
    return counts


def daily(conn, state: dict) -> None:
    """For long-running processes (the poll loop): purge, and back up once a day if a folder is set."""
    purge(conn)
    if config.get("TEAMPULSE_BACKUP_DIR") and state.get("last_backup", datetime.min.replace(tzinfo=timezone.utc)) < \
            datetime.now(timezone.utc) - timedelta(hours=24):
        backup()
        state["last_backup"] = datetime.now(timezone.utc)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("purge")
    b = sub.add_parser("backup")
    b.add_argument("--to")
    v = sub.add_parser("verify")
    v.add_argument("path")
    r = sub.add_parser("restore")
    r.add_argument("path")
    r.add_argument("--yes", action="store_true", help="confirm: the app, bot and pollers are stopped")
    args = parser.parse_args()
    if args.command == "purge":
        print(purge(db.connect()))
    elif args.command == "backup":
        print(backup(dest=args.to))
    elif args.command == "verify":
        print(verify(args.path))
    elif not args.yes:
        parser.error("Stop the app, bot and pollers, then run again with --yes.")
    else:
        print("Restored:", restore(args.path))


if __name__ == "__main__":
    main()
