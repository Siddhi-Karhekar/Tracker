"""Retention purge, and a backup that can actually be restored."""
from datetime import datetime, timedelta, timezone

import pytest

from teampulse import auth, db, maintenance


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_purge_removes_only_what_is_past_retention(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMPULSE_AUDIT_DAYS", "180")
    monkeypatch.setenv("TEAMPULSE_CACHE_DAYS", "30")
    conn = db.connect(str(tmp_path / "t.db"))
    uid = auth._insert_user(conn, "Neha Sharma", "neha", "leader", None, "violet-river-stone-42", False, None)
    with conn:
        for i, (created, expires, revoked) in enumerate([(ago(40), ago(30), None), (ago(1), ago(-1), None),
                                                         (ago(20), ago(-1), ago(10)), (ago(2), ago(-1), ago(1))]):
            conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", (f"h{i}", uid, created, created, expires, revoked))
        conn.executemany("INSERT INTO audit_log (at, action, outcome) VALUES (?, 'x', 'ok')", [(ago(200),), (ago(5),)])
        conn.executemany("INSERT INTO llm_cache VALUES (?, 'r', 'p', ?)", [("old", ago(31)), ("new", ago(1))])
        conn.executemany("INSERT INTO login_throttle VALUES (?, 0, ?)", [("a", ago(1)), ("b", ago(-0.01))])
    counts = maintenance.purge(conn)
    assert counts == {"sessions": 2, "login_throttle": 1, "audit_log": 1, "llm_cache": 1}
    assert {r[0] for r in conn.execute("SELECT token_hash FROM sessions")} == {"h1", "h3"}   # live + recently ended
    assert [r[0] for r in conn.execute("SELECT key FROM llm_cache")] == ["new"]
    assert [r[0] for r in conn.execute("SELECT key FROM login_throttle")] == ["b"]            # still locked


def test_backup_restores_data_and_old_sessions_cannot_be_reused(tmp_path):
    path = str(tmp_path / "live.db")
    conn = db.connect(path)
    auth._insert_user(conn, "Neha Sharma", "neha", "leader", None, "violet-river-stone-42", False, None)
    token, _ = auth.login(conn, "neha", "violet-river-stone-42")
    copy = maintenance.backup(path, str(tmp_path / "backups"))
    assert maintenance.verify(str(copy))["users"] == 1

    with conn:                                   # disaster: someone deletes the accounts
        conn.execute("DELETE FROM users")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    maintenance.restore(str(copy), path)
    fresh = db.connect(path)
    assert fresh.execute("SELECT username FROM users").fetchone()[0] == "neha"
    assert auth.resolve(fresh, token) is None                       # sessions in the copy are revoked
    assert auth.login(fresh, "neha", "violet-river-stone-42")[1].username == "neha"
    assert list((tmp_path / "before-restore").glob("teampulse-*.db"))  # the replaced copy was kept


def test_damaged_backups_are_refused_and_old_ones_pruned(tmp_path):
    path = str(tmp_path / "live.db")
    db.connect(path)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(Exception):
        maintenance.restore(str(bad), path)
    folder = tmp_path / "b"
    for _ in range(4):
        maintenance.backup(path, str(folder), keep=2)
    assert len(list(folder.glob("teampulse-*.db"))) == 2
