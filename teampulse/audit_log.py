"""The activity log: every sign-in, change and refusal, chained by hash so tampering shows."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from teampulse.identity import Actor, iso, now


# ---- audit -------------------------------------------------------------------------------------------

def audit(conn: sqlite3.Connection, actor: Actor | None, action: str, target: str = "", outcome: str = "ok",
          detail: str = "") -> None:
    with conn:
        conn.execute("INSERT INTO audit_log (at, actor_id, actor_name, action, target, outcome, detail) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (iso(now()), actor.id if actor else None, actor.display_name if actor else None, action,
                      str(target or "")[:200], outcome, str(detail or "")[:300]))
    seal_audit(conn)


# ---- tamper-evident activity log ---------------------------------------------------------------------------
# Every entry carries the hash of the one before it, so changing or deleting an entry breaks the chain from there
# on, and verify_audit() says where. Entries are sealed in id order under SQLite's write lock, so the app, the poller
# and the bot writing at once can't fork the chain. The newest hash is also kept separately, which catches entries
# removed from the end. (Someone with full write access to the database file could rebuild the whole chain; keep
# backups, whose verification records the head hash, to compare against.)

AUDIT_FIELDS = ("at", "actor_id", "actor_name", "action", "target", "outcome", "detail")


def _digest(prev: str, row) -> str:
    payload = json.dumps([prev] + [row[f] for f in AUDIT_FIELDS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def seal_audit(conn) -> int:
    if conn.in_transaction:      # inside someone else's transaction: the next audit() call seals it
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Continue from the recorded head, not from whatever row is last: if entries were cut off the end, the next
        # entry then points at a hash that no longer exists, and the gap stays visible.
        head = conn.execute("SELECT value FROM settings WHERE key = 'audit:head'").fetchone()
        last = conn.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        prev = json.loads(head[0])["hash"] if head else (last[0] if last else "genesis")
        rows = conn.execute(f"SELECT id, {', '.join(AUDIT_FIELDS)} FROM audit_log WHERE hash IS NULL ORDER BY id"  # nosec B608  # fixed column list AUDIT_FIELDS
                            ).fetchall()
        for r in rows:
            h = _digest(prev, r)
            conn.execute("UPDATE audit_log SET prev_hash = ?, hash = ? WHERE id = ?", (prev, h, r["id"]))
            prev = h
        if rows:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('audit:head', ?)",
                         (json.dumps({"id": rows[-1]["id"], "hash": prev}),))
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise


def verify_audit(conn) -> dict:
    """Recompute the chain. The first entry still kept (older ones may be removed by retention) anchors it."""
    seal_audit(conn)
    rows = conn.execute(f"SELECT id, prev_hash, hash, {', '.join(AUDIT_FIELDS)} FROM audit_log ORDER BY id").fetchall()  # nosec B608  # fixed column list AUDIT_FIELDS
    if not rows:
        return {"ok": True, "checked": 0}
    prev = rows[0]["prev_hash"]
    for r in rows:
        if r["prev_hash"] != prev or _digest(prev, r) != r["hash"]:
            return {"ok": False, "checked": len(rows), "first_bad_id": r["id"], "at": r["at"],
                    "reason": "an entry was changed or removed here"}
        prev = r["hash"]
    head = conn.execute("SELECT value FROM settings WHERE key = 'audit:head'").fetchone()
    if head:
        h = json.loads(head[0])
        if h["id"] > rows[-1]["id"] or (h["id"] == rows[-1]["id"] and h["hash"] != prev):
            return {"ok": False, "checked": len(rows), "first_bad_id": rows[-1]["id"], "at": rows[-1]["at"],
                    "reason": "the newest entries were removed"}
    return {"ok": True, "checked": len(rows), "since": rows[0]["at"], "head": prev}
