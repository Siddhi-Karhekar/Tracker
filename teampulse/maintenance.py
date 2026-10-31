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
    python -m teampulse.maintenance new-key        # a key for encrypted backups (TEAMPULSE_BACKUP_KEY)

Encryption: set TEAMPULSE_BACKUP_KEY and backups are written as .db.enc (Fernet: AES with an integrity check).
The snapshot is taken and checked in memory, so no unencrypted copy is ever written to disk. Keep the key
somewhere other than the backups; without it they can't be read.
"""
from __future__ import annotations

import argparse
import json
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


def new_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _fernet(key: str | None = None):
    key = key or config.get("TEAMPULSE_BACKUP_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        raise ValueError("TEAMPULSE_BACKUP_KEY isn't a valid key. Make one with: python -m teampulse.maintenance "
                         "new-key") from None


def _plain(data: bytes) -> bytes:
    """A live TeamPulse database is in WAL mode, which only works on disk. Bytes 18-19 of the SQLite header say so;
    setting them back to the classic rollback-journal format lets the copy open in memory. Nothing else changes."""
    data = bytearray(data)
    if data[18:20] == b"\x02\x02":
        data[18:20] = b"\x01\x01"
    return bytes(data)


def _check(conn, label: str) -> dict:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ValueError(f"{label} failed its integrity check: {result}")
    tables = {"events", "users", "audit_log"}
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in sorted(tables)}


def _open_backup(path: str, key: str | None = None) -> sqlite3.Connection:
    """A backup as an in-memory database (decrypted there if it's a .enc file)."""
    if str(path).endswith(".enc"):
        fernet = _fernet(key)
        if fernet is None:
            raise ValueError("This backup is encrypted. Set TEAMPULSE_BACKUP_KEY to the key it was made with.")
        from cryptography.fernet import InvalidToken
        try:
            data = fernet.decrypt(Path(path).read_bytes())
        except InvalidToken:
            raise ValueError("This backup can't be decrypted: it was made with a different key, or it has been "
                             "changed or damaged.") from None
        conn = sqlite3.connect(":memory:")
        conn.deserialize(_plain(data))
        return conn
    disk = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)   # also correct on Windows
    conn = sqlite3.connect(":memory:")
    try:
        disk.backup(conn)
    finally:
        disk.close()
    return conn


def verify(path: str, key: str | None = None) -> dict:
    """Check a backup (plain or encrypted). Raises ValueError if it is damaged or can't be decrypted."""
    conn = _open_backup(path, key)
    try:
        return _check(conn, path)
    finally:
        conn.close()


def backup(db_path: str | None = None, dest: str | None = None, keep: int | None = None) -> Path:
    db_path = db_path or config.DB_PATH
    folder = Path(dest or config.get("TEAMPULSE_BACKUP_DIR", "data/backups"))
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    fernet = _fernet()
    source, snapshot = _raw(db_path), sqlite3.connect(":memory:")
    try:
        source.backup(snapshot)              # consistent snapshot, even while others write
        counts = _check(snapshot, "the snapshot")
        head = snapshot.execute("SELECT value FROM settings WHERE key = 'audit:head'").fetchone()
        if fernet is not None:
            target = folder / f"teampulse-{stamp}.db.enc"
            target.write_bytes(fernet.encrypt(_plain(snapshot.serialize())))   # never written unencrypted
        else:
            target = folder / f"teampulse-{stamp}.db"
            copy = sqlite3.connect(target)
            try:
                snapshot.backup(copy)
            finally:
                copy.close()
    finally:
        snapshot.close()
        source.close()
    verify(str(target))                      # read it back the way a restore would
    keep = keep if keep is not None else config.get_int("TEAMPULSE_BACKUP_KEEP", 14)
    copies = sorted([*folder.glob("teampulse-*.db"), *folder.glob("teampulse-*.db.enc")], key=lambda p: p.name)
    for old in copies[:-keep or None]:
        old.unlink()
    with _raw(db_path) as live:              # for the Security page: when, encrypted or not, and the audit head
        live.execute("INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES ('backup:last', ?, ?)",
                     (json.dumps({"file": target.name, "encrypted": fernet is not None, "verified": True,
                                  "counts": counts, "audit_head": head[0] if head else None}), _iso(0)))
    log.info("Backup written to %s", target)
    return target


def restore(backup_path: str, db_path: str | None = None) -> dict:
    """Replace the live database with a verified backup. Stop the app, bot and pollers first."""
    db_path = db_path or config.DB_PATH
    source = _open_backup(backup_path)       # decrypted in memory if needed
    try:
        counts = _check(source, backup_path)  # never restore a damaged copy
        if Path(db_path).exists():
            backup(db_path, str(Path(db_path).parent / "before-restore"), keep=5)   # undo is one restore away
        target = _raw(db_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
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
    sub.add_parser("new-key", help="print a new key for encrypted backups")
    args = parser.parse_args()
    if args.command == "new-key":
        print(f"TEAMPULSE_BACKUP_KEY={new_key()}\nPut this in .env, and keep a copy somewhere other than the backups.")
        return
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
