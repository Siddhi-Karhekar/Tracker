"""Read-only queries the pages need. Pages call these instead of writing SQL, so the pages only lay things out and
every query lives in one place (and can be tested without a browser). Nothing here changes data; changes go through
actions.py and pm.py, which check permissions. Which page may be opened is decided by webapp.require_page.
"""
from __future__ import annotations

import sqlite3

AUDIT_COLUMNS = "at, actor_name, action, target, outcome, detail"


def _one(conn: sqlite3.Connection, sql: str, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ---- first run and freshness --------------------------------------------------------------------------------------

def tasks_refreshed_at(conn) -> str | None:
    """When tasks were last rebuilt from GitHub and Notion (None: never, i.e. a new database)."""
    return _one(conn, "SELECT value FROM sync_state WHERE key = 'tasks:refreshed_at'")


def has_events(conn) -> bool:
    return _one(conn, "SELECT 1 FROM events LIMIT 1") is not None


def has_leader(conn) -> bool:
    return _one(conn, "SELECT 1 FROM users WHERE role = 'leader'") is not None


def conflicts_compared_at(conn) -> str | None:
    return _one(conn, "SELECT MAX(last_seen) FROM discrepancies")


# ---- home ---------------------------------------------------------------------------------------------------------

def open_blockers(conn, days: int = 21) -> list[sqlite3.Row]:
    """Unresolved blockers and help requests from the last `days` days, with the task each one is about."""
    return conn.execute(
        "SELECT s.type, s.summary, e.body, e.author_name, e.created_at, e.url, t.title AS task, t.github_number "
        "FROM signals s JOIN events e ON e.id = s.event_id LEFT JOIN task_links tl ON tl.event_id = s.event_id "
        "LEFT JOIN tasks t ON t.id = tl.task_id WHERE s.type IN ('blocker','help_request') AND s.resolved = 0 "
        "AND e.created_at >= date('now', ?) ORDER BY s.type, e.created_at DESC", (f"-{int(days)} days",)).fetchall()


def daily_activity(conn, days: int = 14) -> list[sqlite3.Row]:
    """Items per day and source over the last `days` days."""
    return conn.execute("SELECT substr(created_at, 1, 10) AS day, source, COUNT(*) AS n FROM events "
                        "WHERE created_at >= date('now', ?) GROUP BY day, source", (f"-{int(days)} days",)).fetchall()


# ---- blockers, tasks --------------------------------------------------------------------------------------------

def linked_source_tasks(conn) -> set[str]:
    """Source tasks (GitHub issues, Notion pages) already linked to a planned task."""
    return {r[0] for r in conn.execute("SELECT source_task FROM pm_source_links")}


def signal_count(conn) -> int:
    return _one(conn, "SELECT COUNT(*) FROM signals") or 0


def event_url(conn, event_id: str) -> str | None:
    return _one(conn, "SELECT url FROM events WHERE id = ?", (event_id,))


def task_items(conn, task_id: str) -> list[sqlite3.Row]:
    """Everything linked to a source task (issue, PRs, commits, chat), newest first."""
    return conn.execute(
        "SELECT tl.role, tl.method, e.source, e.title, e.body, e.url, e.author_name, e.updated_at "
        "FROM task_links tl JOIN events e ON e.id = tl.event_id WHERE tl.task_id = ? ORDER BY e.updated_at DESC",
        (task_id,)).fetchall()


def task_ids_by_primary_event(conn) -> dict[str, str]:
    return {r["primary_event"]: r["id"] for r in conn.execute("SELECT id, primary_event FROM tasks")}


def recent_chat_signals(conn, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.event_id, s.type, s.task_id, s.link_method, e.body, e.author_name, e.created_at "
        "FROM signals s JOIN events e ON e.id = s.event_id WHERE s.type != 'noise' "
        "ORDER BY e.created_at DESC LIMIT ?", (int(limit),)).fetchall()


# ---- labelling ------------------------------------------------------------------------------------------------------

def discord_message_count(conn) -> int:
    return _one(conn, "SELECT COUNT(*) FROM events WHERE source='discord' AND kind='message'") or 0


def labelled_count(conn) -> int:
    return _one(conn, "SELECT COUNT(*) FROM labels l JOIN events e ON e.id = l.event_id") or 0


def next_unlabelled_message(conn) -> sqlite3.Row | None:
    """The next unlabelled Discord message, in a stable shuffled order across channels."""
    return conn.execute(
        "SELECT e.* FROM events e LEFT JOIN labels l ON l.event_id = e.id "
        "WHERE e.source='discord' AND e.kind='message' AND l.event_id IS NULL "
        "ORDER BY substr(e.native_id, -3) || e.native_id LIMIT 1").fetchone()


def messages_before(conn, channel: str, before: str, limit: int = 3) -> list[sqlite3.Row]:
    """The messages just before one in the same channel, oldest first."""
    return conn.execute(
        "SELECT author_name, body FROM events WHERE source='discord' AND channel = ? AND created_at < ? "
        "ORDER BY created_at DESC LIMIT ?", (channel, before, int(limit))).fetchall()[::-1]


def recent_labels(conn, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute("SELECT l.event_id, l.label, e.body FROM labels l JOIN events e ON e.id = l.event_id "
                        "ORDER BY l.labelled_at DESC LIMIT ?", (int(limit),)).fetchall()


def labels_by_event(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT event_id, label FROM labels WHERE label != 'skip'")}


# ---- memory, sources, team ----------------------------------------------------------------------------------------

def forgotten_facts(conn, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute("SELECT fact, created_at FROM memory WHERE active = 0 ORDER BY created_at DESC LIMIT ?",
                        (int(limit),)).fetchall()


def source_stats(conn, source: str) -> tuple[int, str | None]:
    """How many items a source has, and when the newest was updated."""
    count, latest = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM events WHERE source = ?", (source,)).fetchone()
    return count, latest


def cached_answer_count(conn) -> int:
    return _one(conn, "SELECT COUNT(*) FROM llm_cache") or 0


def members_with_review(conn) -> list[sqlite3.Row]:
    """Team members with their Discord opt-in state and contribution report review status."""
    return conn.execute("SELECT m.*, r.status AS review FROM members m LEFT JOIN contribution_reviews r "
                        "ON r.member = m.display_name ORDER BY m.display_name").fetchall()


def member_names(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT display_name FROM members ORDER BY display_name")]


def members_with_accounts(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT member_name FROM users WHERE member_name IS NOT NULL")}


def activity_log(conn, outcome: str | None = None, limit: int = 300, skip_questions: bool = False) -> list[dict]:
    """The newest activity log entries, optionally only one outcome ("denied", "failed")."""
    where, params = [], []
    if outcome:
        where.append("outcome = ?")
        params.append(outcome)
    if skip_questions:
        where.append("action != 'ask'")
    sql = f"SELECT {AUDIT_COLUMNS} FROM audit_log"   # nosec B608  # fixed column list and fixed conditions only
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, int(limit)))]
