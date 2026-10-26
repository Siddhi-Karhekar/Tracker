"""Live-path code that can be checked without network access."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import discord

from teampulse import db
from teampulse.config import ROOT, load_team
from teampulse.ingest import discord_ingest, notion_ingest
from teampulse.ingest.discord_bot import to_export_dict

DEMO = ROOT / "data" / "demo"


def fake_message(**kw):
    author = SimpleNamespace(id=1290000000000000101, name="aarav_m", bot=False, display_name="Aarav M")
    base = dict(id=1600000000000000000, type=discord.MessageType.reply,
                created_at=datetime(2026, 10, 26, 5, 0, tzinfo=timezone.utc), edited_at=None,
                content="login abhi bhi crash ho raha hai #1", author=author, mentions=[], attachments=[],
                reference=SimpleNamespace(message_id=42), webhook_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_live_message_uses_same_ingest_path():
    team = load_team(DEMO / "team.toml")
    conn = db.connect(":memory:")
    d = to_export_dict(fake_message())
    assert d["type"] == "Reply" and d["reference"] == {"messageId": "42"}
    assert discord_ingest.ingest_message(conn, team, d, "frontend", "g", "c")
    row = conn.execute("SELECT * FROM events").fetchone()
    assert row["author"] == "aarav"
    assert row["timestamp"] == "2026-10-26T05:00:00Z"
    assert row["parent_id"] == "discord:message:42"
    assert json.loads(row["payload"])["hash_refs"] == [1]


def test_bot_messages_skipped():
    team = load_team(DEMO / "team.toml")
    conn = db.connect(":memory:")
    bot = SimpleNamespace(id=9, name="some-bot", bot=True, display_name="bot")
    assert not discord_ingest.ingest_message(conn, team, to_export_dict(fake_message(author=bot)), "general", "g", "c")


def test_notion_select_assignee_and_rich_text_issue():
    team = load_team(DEMO / "team.toml")
    conn = db.connect(":memory:")
    page = {
        "id": "abcdef12-0000-0000-0000-000000000000", "last_edited_time": "2026-10-26T05:00:00.000Z",
        "last_edited_by": {"id": "x"}, "properties": {
            "Name": {"type": "title", "title": [{"plain_text": "Load testing"}]},
            "Status": {"type": "select", "select": {"name": "In Progress"}},
            "Assignee": {"type": "select", "select": {"name": "rohan"}},
            "Due": {"type": "date", "date": {"start": "2026-10-28"}},
            "GitHub Issue": {"type": "rich_text", "rich_text": [{"plain_text": "#11"}]},
        }}
    assert notion_ingest.ingest_page(conn, team, page) == "T-11"
    claims = {(r["attribute"], r["value"]) for r in conn.execute("SELECT attribute, value FROM claims")}
    assert claims == {("status", "in_progress"), ("owner", "rohan"), ("deadline", "2026-10-28")}
