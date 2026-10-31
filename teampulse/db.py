"""SQLite storage: events from all sources, sync cursors, members/consent, and tables used in later weeks."""
from __future__ import annotations

import sqlite3
import threading
import weakref
from pathlib import Path

from teampulse import config
from teampulse.models import Event, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,      -- "<source>:<native_id>"
    source       TEXT NOT NULL,         -- github | notion | discord
    kind         TEXT NOT NULL,         -- issue | pr | commit | issue_comment | review | notion_page | message
    native_id    TEXT NOT NULL,
    author       TEXT,
    author_name  TEXT,
    title        TEXT,
    body         TEXT,
    status       TEXT,
    assignees    TEXT,                  -- JSON list
    url          TEXT,
    channel      TEXT,
    parent_id    TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    extra        TEXT,                  -- JSON object
    ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_source_kind ON events(source, kind);
CREATE INDEX IF NOT EXISTS idx_events_author ON events(source, author);
CREATE INDEX IF NOT EXISTS idx_events_updated ON events(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);

-- Where each poller / channel left off.
CREATE TABLE IF NOT EXISTS sync_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);

-- One row per real person, linking their identities across tools. Consent gates Discord capture
-- and inclusion in contribution reports.
CREATE TABLE IF NOT EXISTS members (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT,
    discord_id      TEXT UNIQUE,
    github_login    TEXT UNIQUE,
    notion_user_id  TEXT UNIQUE,
    consent         INTEGER NOT NULL DEFAULT 0,
    consented_at    TEXT,
    opted_out_at    TEXT
);

-- Week 6: every source turned into comparable statements about a task.
CREATE TABLE IF NOT EXISTS claims (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    attribute   TEXT NOT NULL,          -- status | owner | deadline | blocker
    value       TEXT,
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    author      TEXT,
    observed_at TEXT,
    confidence  REAL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_claims_task ON claims(task_id, attribute);

-- Week 3: which event versions are already embedded in the vector index.
CREATE TABLE IF NOT EXISTS indexed (
    event_id    TEXT PRIMARY KEY,
    updated_at  TEXT,
    chunks      INTEGER NOT NULL
);

-- Week 3: cached LLM responses (saves free-tier quota, makes demos repeatable).
CREATE TABLE IF NOT EXISTS llm_cache (
    key         TEXT PRIMARY KEY,
    response    TEXT NOT NULL,
    provider    TEXT,
    created_at  TEXT NOT NULL
);

-- Week 5: what each chat message is (blocker / decision / progress / help_request / noise).
CREATE TABLE IF NOT EXISTS signals (
    event_id          TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    type              TEXT NOT NULL,
    summary           TEXT,
    task_hint         TEXT,
    task_id           TEXT,            -- linked GitHub issue / Notion task event id
    link_method       TEXT,            -- ref | window_ref | semantic
    link_score        REAL,
    resolved          INTEGER NOT NULL DEFAULT 0,
    classifier        TEXT,            -- llm:<provider> | keyword
    window_id         TEXT,
    event_updated_at  TEXT,
    classified_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(type);

-- Week 5: human labels for evaluating the classifier.
CREATE TABLE IF NOT EXISTS labels (
    event_id     TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    labeller     TEXT,
    labelled_at  TEXT
);

-- Week 6: one row per piece of work, merged across GitHub issues and Notion tasks.
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,     -- task:<repo>#<n>  or  task:notion:<page id>
    title          TEXT,
    primary_event  TEXT,                 -- the issue (or Notion page) that defines the task
    github_number  INTEGER,
    notion_page    TEXT,                 -- event id of the matched Notion task page
    url            TEXT
);

-- Week 6: which events belong to which task, and how we know.
CREATE TABLE IF NOT EXISTS task_links (
    event_id  TEXT PRIMARY KEY,
    task_id   TEXT NOT NULL,
    role      TEXT,                      -- issue | notion_task | pr | commit | comment | review | chat
    method    TEXT,                      -- primary | ref | title | llm | parent | semantic | human
    score     REAL
);
CREATE INDEX IF NOT EXISTS idx_task_links_task ON task_links(task_id);

-- Week 6: human corrections of chat→task links (task_id NULL = "not about any task").
CREATE TABLE IF NOT EXISTS link_feedback (
    event_id     TEXT PRIMARY KEY,
    task_id      TEXT,
    labeller     TEXT,
    labelled_at  TEXT
);

-- Week 6: cached LLM extraction of status/owner/deadline from chat messages.
CREATE TABLE IF NOT EXISTS extractions (
    event_id          TEXT PRIMARY KEY,
    event_updated_at  TEXT,
    task_id           TEXT,
    data              TEXT,
    method            TEXT
);

-- Week 7: detected disagreements between sources, tracked over time.
CREATE TABLE IF NOT EXISTS discrepancies (
    id            TEXT PRIMARY KEY,      -- hash of type + task + the conflicting values
    task_id       TEXT,
    type          TEXT NOT NULL,         -- status | stale_plan | owner | deadline | plan_outdated
    severity      TEXT NOT NULL,         -- high | medium | low
    title         TEXT,
    suggestion    TEXT,
    sides         TEXT,                  -- JSON list of {source, value, event_id, observed_at, ...}
    verified      INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'open',   -- open | dismissed | resolved
    first_seen    TEXT,
    last_seen     TEXT,
    resolved_at   TEXT,
    dismissed_by  TEXT,
    note          TEXT,
    verdict       TEXT                   -- set by people: real | false_alarm
);

-- Week 8: each member reviews their own contribution report before the team can see it.
CREATE TABLE IF NOT EXISTS contribution_reviews (
    member      TEXT PRIMARY KEY,        -- members.display_name
    status      TEXT NOT NULL,           -- approved | disputed
    comment     TEXT,                    -- the member's own context, shown with their report
    updated_at  TEXT
);

-- Accounts and access control. Every account is created by a team leader.
CREATE TABLE IF NOT EXISTS users (
    id               TEXT PRIMARY KEY,          -- random, not guessable
    username         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name     TEXT NOT NULL,
    role             TEXT NOT NULL CHECK (role IN ('leader', 'member')),
    password_hash    TEXT NOT NULL,             -- scrypt with a per-user salt, never the password
    must_change      INTEGER NOT NULL DEFAULT 1, -- set for leader-issued passwords
    active           INTEGER NOT NULL DEFAULT 1,
    member_name      TEXT,                      -- links the account to members.display_name
    created_at       TEXT NOT NULL,
    created_by       TEXT,
    last_login       TEXT
);

-- Server-side sessions. Only a hash of each token is stored.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

-- Failed sign-ins per username (whether or not the account exists, so nothing is revealed).
CREATE TABLE IF NOT EXISTS login_throttle (
    key           TEXT PRIMARY KEY,
    failures      INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT
);

-- Who did what, including refused attempts. Never contains passwords or message text.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor_id    TEXT,
    actor_name  TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    outcome     TEXT NOT NULL,              -- ok | denied | failed
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_id, action, at);   -- per-person rate limits

-- Workspace settings the leader controls (e.g. which pages members can open).
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Week 8: long-term memory, only explicit facts, re-verified against live data.
CREATE TABLE IF NOT EXISTS memory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fact           TEXT NOT NULL,
    source         TEXT,
    created_at     TEXT NOT NULL,
    last_verified  TEXT
);

-- TeamPulse 2.0: project management. Tasks here are planned by the team in TeamPulse itself; the `tasks`
-- table above is rebuilt from GitHub/Notion and is never edited by hand. Members are display names from
-- `members` (the same names user accounts link to through users.member_name).
CREATE TABLE IF NOT EXISTS pm_projects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    start_date   TEXT,
    end_date     TEXT,
    archived     INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_milestones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES pm_projects(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    due_date    TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pm_tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES pm_projects(id) ON DELETE CASCADE,
    parent_id          INTEGER REFERENCES pm_tasks(id) ON DELETE CASCADE,   -- set for subtasks
    milestone_id       INTEGER REFERENCES pm_milestones(id) ON DELETE SET NULL,
    title              TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    priority           TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    status             TEXT NOT NULL DEFAULT 'todo'
                       CHECK (status IN ('backlog', 'todo', 'in_progress', 'review', 'blocked', 'done')),
    reported_progress  INTEGER NOT NULL DEFAULT 0 CHECK (reported_progress BETWEEN 0 AND 100),
    estimate_hours     REAL,
    blocked_reason     TEXT,
    start_date         TEXT,
    due_date           TEXT,
    completed_at       TEXT,
    archived           INTEGER NOT NULL DEFAULT 0,
    created_by         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1        -- optimistic locking: two people editing at once
);
CREATE INDEX IF NOT EXISTS idx_pm_tasks_project ON pm_tasks(project_id, parent_id, status);
CREATE INDEX IF NOT EXISTS idx_pm_tasks_due ON pm_tasks(due_date);
CREATE TABLE IF NOT EXISTS pm_assignees (
    task_id  INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    member   TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (task_id, member)
);
CREATE INDEX IF NOT EXISTS idx_pm_assignees_member ON pm_assignees(member);
CREATE TABLE IF NOT EXISTS pm_dependencies (
    task_id     INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    depends_on  INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id != depends_on)
);
CREATE TABLE IF NOT EXISTS pm_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    author_id   TEXT,
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'comment',   -- comment | blocker | review
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_comments_task ON pm_comments(task_id, created_at);
CREATE TABLE IF NOT EXISTS pm_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    at         TEXT NOT NULL,
    actor_id   TEXT,
    actor      TEXT,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_history_task ON pm_history(task_id, at);
CREATE INDEX IF NOT EXISTS idx_pm_history_at ON pm_history(at);
-- Requests a member makes and the leader decides (Phase 1: deadline changes).
CREATE TABLE IF NOT EXISTS pm_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'deadline',
    requested_by  TEXT NOT NULL,          -- member name
    proposed      TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by    TEXT,
    decided_at    TEXT,
    created_at    TEXT NOT NULL
);
-- In-app notifications. recipient is a member name, or '@leaders' for every team leader.
CREATE TABLE IF NOT EXISTS pm_notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient   TEXT NOT NULL COLLATE NOCASE,
    task_id     INTEGER REFERENCES pm_tasks(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    read_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_notifications ON pm_notifications(recipient, read_at);
-- Progress updates with evidence. kind 'reported' is the assignee's own number; 'verified' is the leader's,
-- and only a leader can write one. Nothing automatic (and no AI) ever writes a verified update.
CREATE TABLE IF NOT EXISTS pm_progress_updates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES pm_tasks(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('reported', 'verified')),
    author_id     TEXT,
    author        TEXT NOT NULL,
    progress      INTEGER NOT NULL CHECK (progress BETWEEN 0 AND 100),
    note          TEXT NOT NULL DEFAULT '',
    evidence_url  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pm_progress_task ON pm_progress_updates(task_id, id);
-- A planned task linked to the GitHub issue / Notion page it corresponds to (an id in `tasks`, which is rebuilt
-- from the sources). The link only brings evidence and disagreements in; the sources never change a planned task.
CREATE TABLE IF NOT EXISTS pm_source_links (
    task_id       INTEGER PRIMARY KEY REFERENCES pm_tasks(id) ON DELETE CASCADE,
    source_task   TEXT NOT NULL UNIQUE,
    source_title  TEXT,                  -- kept so the link still makes sense if the source disappears
    linked_by     TEXT,
    linked_at     TEXT NOT NULL
);
-- Extra capabilities a leader grants to one member (e.g. co-lead planning). Only permissions in auth.GRANTABLE
-- count; anything else in this table is ignored, so a tampered row can't make someone an admin.
CREATE TABLE IF NOT EXISTS user_grants (
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission  TEXT NOT NULL,
    granted_by  TEXT,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, permission)
);

-- Personal access tokens for an AI assistant (MCP). Like sessions, only a hash is stored. scope 'read' can only
-- look; 'write' can also update the person's own tasks. They never give more than the person's own role allows.
CREATE TABLE IF NOT EXISTS api_tokens (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    scope       TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    last_used   TEXT,
    revoked_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);

-- Phase 5: what the daily check noticed. Suggestions only: nothing here ever changes a task. One row per situation
-- (key), reopened if it comes back after being resolved; dismissed rows stay quiet until the situation ends.
CREATE TABLE IF NOT EXISTS pm_alerts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT NOT NULL UNIQUE,
    kind           TEXT NOT NULL,
    project_id     INTEGER REFERENCES pm_projects(id) ON DELETE CASCADE,
    task_id        INTEGER REFERENCES pm_tasks(id) ON DELETE CASCADE,
    member         TEXT,
    severity       TEXT NOT NULL CHECK (severity IN ('high', 'medium', 'low')),
    text           TEXT NOT NULL,          -- the facts
    suggestion     TEXT NOT NULL,          -- what a person might do about it
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'dismissed', 'resolved')),
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    resolved_at    TEXT,
    snoozed_until  TEXT,
    decided_by     TEXT,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_alerts_status ON pm_alerts(status, project_id);

-- Phase 4: what the AI proposed, kept until a person accepts or discards it. Nothing in here is a task until a
-- leader confirms it; then the created task ids are recorded.
CREATE TABLE IF NOT EXISTS pm_ai_drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL CHECK (kind IN ('plan', 'summary')),
    project_id   INTEGER REFERENCES pm_projects(id) ON DELETE CASCADE,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    input        TEXT NOT NULL DEFAULT '',
    output       TEXT NOT NULL,            -- JSON: proposed items (plan) or {text, facts} (summary)
    method       TEXT NOT NULL,            -- the AI provider, or 'rules' when no AI answered
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'discarded')),
    decided_by   TEXT,
    decided_at   TEXT,
    created_ids  TEXT
);
"""

EVENT_COLUMNS = [
    "id", "source", "kind", "native_id", "author", "author_name", "title", "body", "status",
    "assignees", "url", "channel", "parent_id", "created_at", "updated_at", "extra", "ingested_at",
]


BUSY_SECONDS = 15   # wait this long for another writer (app, bot, poller) instead of failing at once


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_SECONDS, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class ThreadConnection:
    """One SQLite connection per thread, behind one shared object.

    The app keeps a single agent (and so a single connection object) for everyone, but Streamlit runs each
    visitor's page on its own thread. A plain shared connection mixes their transactions: one person's
    rollback can undo another's write, and a commit can save someone else's half-finished change
    (tests/test_concurrency.py). Here every thread transparently gets its own connection, so each `with conn:`
    is a real, separate transaction. A thread's connection is closed when the thread ends.
    """

    def __init__(self, path: str):
        self._path = path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = _open(self._path)
            # Close it when the thread is cleaned up (Streamlit uses a new thread for each rerun), rather than
            # leaving it to the garbage collector.
            weakref.finalize(threading.current_thread(), conn.close)
        return conn

    def __getattr__(self, name):
        return getattr(self._conn(), name)

    def __enter__(self):
        return self._conn().__enter__()

    def __exit__(self, *exc):
        return self._conn().__exit__(*exc)


def wipe(path: str) -> None:
    """Empty a database file in place (drop every table). Unlike deleting the file, this works while other
    connections have it open, which Windows doesn't allow; those connections simply see the new contents."""
    conn = sqlite3.connect(path, timeout=BUSY_SECONDS)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                             "AND name NOT LIKE 'sqlite_%'")]
        with conn:
            for table in tables:
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
    finally:
        conn.close()


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if path == ":memory:":   # a private in-memory database (tests, evaluation): one connection is the database
        conn = _open(path)
        conn.executescript(SCHEMA)
        _migrate(conn)
        return conn
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    setup = _open(path)
    setup.execute("PRAGMA journal_mode=WAL")   # readers never block the writer; the bot and pollers write too
    setup.executescript(SCHEMA)
    _migrate(setup)
    setup.close()
    return ThreadConnection(path)   # type: ignore[return-value]  (behaves like sqlite3.Connection)


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS won't add them to old databases.
MIGRATIONS = [
    ("signals", "link_checked", "INTEGER NOT NULL DEFAULT 0"),   # Week 6: chat link confirmed by LLM/human
    ("claims", "method", "TEXT"),                                # Week 6: how the claim was extracted
    ("memory", "kind", "TEXT"),                                  # Week 8: deadline | priority | preference | fact
    ("memory", "subject", "TEXT"),
    ("memory", "fact_date", "TEXT"),
    ("memory", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("memory", "created_by", "TEXT"),
    ("pm_tasks", "weight", "INTEGER NOT NULL DEFAULT 1"),        # 2.0: size used for completion %
    ("pm_tasks", "verified_progress", "INTEGER"),                 # 2.0: set only by a leader
    ("pm_tasks", "verified_by", "TEXT"),
    ("pm_tasks", "verified_at", "TEXT"),
    ("pm_notifications", "dedupe", "TEXT"),                      # 2.0: one reminder per task per day
    ("sessions", "reauth_at", "TEXT"),                           # security: password re-confirmed in this session
    ("audit_log", "prev_hash", "TEXT"),                          # security: tamper-evident chain
    ("audit_log", "hash", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    # Indexes on migrated columns can only be created once the columns exist.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pm_notifications_dedupe ON pm_notifications(dedupe) "
                 "WHERE dedupe IS NOT NULL")
    conn.commit()


def upsert_event(conn: sqlite3.Connection, event: Event) -> None:
    row = event.to_row()
    cols = ", ".join(EVENT_COLUMNS)
    marks = ", ".join(f":{c}" for c in EVENT_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in EVENT_COLUMNS if c != "id")
    conn.execute(f"INSERT INTO events ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}", row)


def upsert_events(conn: sqlite3.Connection, events) -> int:
    count = 0
    with conn:
        for event in events:
            upsert_event(conn, event)
            count += 1
    return count


# Tables that hold copies or by-products of an event without a foreign key (signals and claims cascade).
DERIVED_TABLES = ("extractions", "labels", "task_links", "link_feedback")


def _delete_with_copies(conn: sqlite3.Connection, ids: list[str]) -> int:
    """Delete events and everything derived from them, in the caller's transaction. Returns events deleted.

    The search index drops them on its next sync (EventIndex.sync removes vectors whose event is gone)."""
    if not ids:
        return 0
    removed = 0
    for i in range(0, len(ids), 500):        # stay under SQLite's limit on query parameters
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for table in DERIVED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE event_id IN ({marks})", chunk)
        removed += conn.execute(f"DELETE FROM events WHERE id IN ({marks})", chunk).rowcount
        for event_id in chunk:               # conflicts that quote the message; rebuilt on the next comparison
            conn.execute("DELETE FROM discrepancies WHERE instr(sides, ?) > 0", (f'"{event_id}"',))
    return removed


def delete_event(conn: sqlite3.Connection, event_id: str) -> None:
    with conn:
        _delete_with_copies(conn, [event_id])


HEARTBEAT = {"poller": "heartbeat:poller", "discord": "heartbeat:discord"}


def beat(conn: sqlite3.Connection, process: str) -> None:
    """Long-running processes call this regularly so the app can tell "connected" from "stopped"."""
    set_state(conn, HEARTBEAT[process], utc_now())


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utc_now()),
        )


# ---- members & consent -------------------------------------------------------------------

def consented_discord_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT discord_id FROM members WHERE consent = 1 AND discord_id IS NOT NULL")
    return {r["discord_id"] for r in rows}


def set_discord_consent(conn: sqlite3.Connection, discord_id: str, name: str, consent: bool) -> None:
    now = utc_now()
    with conn:
        conn.execute(
            "INSERT INTO members (discord_id, display_name) VALUES (?, ?) ON CONFLICT(discord_id) DO NOTHING",
            (discord_id, name),
        )
        if consent:
            conn.execute(
                "UPDATE members SET consent = 1, consented_at = ?, opted_out_at = NULL, "
                "display_name = COALESCE(display_name, ?) WHERE discord_id = ?",
                (now, name, discord_id),
            )
        else:
            conn.execute(
                "UPDATE members SET consent = 0, opted_out_at = ? WHERE discord_id = ?", (now, discord_id)
            )


def delete_discord_messages_of(conn: sqlite3.Connection, discord_id: str) -> int:
    """Opt-out: delete the person's messages and every copy derived from them.

    Cached AI responses are keyed by a hash of the prompt, so the ones that quoted this person can't be picked
    out; the whole cache is cleared instead (it only costs re-asking the AI)."""
    with conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM events WHERE source = 'discord' AND author = ?",
                                          (discord_id,))]
        removed = _delete_with_copies(conn, ids)
        conn.execute("DELETE FROM llm_cache")
    return removed


def count_events(conn: sqlite3.Connection, source: str, author: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM events WHERE source = ? AND author = ?", (source, author)
    ).fetchone()[0]
