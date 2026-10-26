"""SQLite access: connect, initialise schema, and the small write helpers ingestors share."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from teampulse.config import Team, db_path

SCHEMA = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path else db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    return conn


def sync_members(conn: sqlite3.Connection, team: Team) -> None:
    for m in team.members:
        conn.execute(
            """INSERT INTO members (handle, display_name, github_login, notion_user_id, discord_id, discord_name, opted_in)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(handle) DO UPDATE SET display_name=excluded.display_name,
                 github_login=excluded.github_login, notion_user_id=excluded.notion_user_id,
                 discord_id=excluded.discord_id, discord_name=excluded.discord_name""",
            (m.handle, m.display_name, m.github_login, m.notion_user_id, m.discord_id, m.discord_name, int(m.opted_in)),
        )
    conn.commit()


def opted_out(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT handle FROM optouts UNION SELECT handle FROM members WHERE opted_in = 0"
    ).fetchall()
    return {r[0] for r in rows}


def upsert_event(conn: sqlite3.Connection, ev: dict) -> None:
    conn.execute(
        """INSERT INTO events (event_id, source, type, native_id, url, author, author_raw, timestamp, channel,
                               parent_id, title, body, task_ids, payload, deleted, ingested_at)
           VALUES (:event_id, :source, :type, :native_id, :url, :author, :author_raw, :timestamp, :channel,
                   :parent_id, :title, :body, :task_ids, :payload, 0, :ingested_at)
           ON CONFLICT(event_id) DO UPDATE SET url=excluded.url, author=excluded.author,
             author_raw=excluded.author_raw, timestamp=excluded.timestamp, channel=excluded.channel,
             parent_id=excluded.parent_id, title=excluded.title, body=excluded.body,
             task_ids=excluded.task_ids, payload=excluded.payload, ingested_at=excluded.ingested_at""",
        {
            "url": None, "author": None, "author_raw": None, "channel": None, "parent_id": None,
            "title": None, "body": None,
            **ev,
            "task_ids": json.dumps(ev.get("task_ids") or []),
            "payload": json.dumps(ev.get("payload") or {}, ensure_ascii=False),
            "ingested_at": now_iso(),
        },
    )


def upsert_task(conn: sqlite3.Connection, task_id: str, title: str, github_issue=None, notion_page_id=None) -> None:
    conn.execute(
        """INSERT INTO tasks (task_id, title, github_issue, notion_page_id, updated_at) VALUES (?,?,?,?,?)
           ON CONFLICT(task_id) DO UPDATE SET title=excluded.title,
             github_issue=COALESCE(excluded.github_issue, tasks.github_issue),
             notion_page_id=COALESCE(excluded.notion_page_id, tasks.notion_page_id),
             updated_at=excluded.updated_at""",
        (task_id, title, github_issue, notion_page_id, now_iso()),
    )


def add_claim(conn: sqlite3.Connection, claim: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO claims (task_id, attribute, value, source, source_ref, author, timestamp, confidence, extractor)
           VALUES (:task_id, :attribute, :value, :source, :source_ref, :author, :timestamp, :confidence, :extractor)""",
        {"author": None, "confidence": 1.0, **claim},
    )


def get_cursor(conn: sqlite3.Connection, source: str, scope: str) -> str | None:
    row = conn.execute("SELECT cursor FROM sync_state WHERE source=? AND scope=?", (source, scope)).fetchone()
    return row[0] if row else None


def set_cursor(conn: sqlite3.Connection, source: str, scope: str, cursor: str) -> None:
    conn.execute(
        """INSERT INTO sync_state (source, scope, cursor, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(source, scope) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at""",
        (source, scope, cursor, now_iso()),
    )
