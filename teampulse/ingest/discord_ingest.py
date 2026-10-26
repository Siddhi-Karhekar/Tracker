"""Discord messages → events.

One message format everywhere: the DiscordChatExporter JSON shape. The live bot converts
discord.py Message objects into it (see discord_bot.py), so exports, seed data and live
messages all go through `ingest_message`.

No claims are made here: what a casual Hinglish message *means* (blocker? "done"? new
deadline?) is the Blocker Agent's job (Week 5). We only store the message, its reply
link, and deterministic task links (a "#17" that names an issue or PR).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from teampulse import db
from teampulse.config import Team
from teampulse.ingest import to_utc_z

HASH_RE = re.compile(r"(?<![\w/&])#(\d{1,5})\b")


def ingest_message(conn: sqlite3.Connection, team: Team, msg: dict, channel: str, guild_id: str | None,
                   channel_id: str | None, skip: set[str] | None = None, thread: str | None = None) -> bool:
    """Store one message. Returns False if skipped (opted-out author, bot, or system message)."""
    author = msg.get("author") or {}
    if author.get("isBot"):
        return False
    if msg.get("type") not in (None, "Default", "Reply", "ThreadStarterMessage"):
        return False
    handle = team.from_discord(author.get("id"), author.get("name"))
    if skip and handle in skip:
        return False
    ref = msg.get("reference") or {}
    url = (f"https://discord.com/channels/{guild_id}/{channel_id}/{msg['id']}"
           if guild_id and channel_id else None)
    db.upsert_event(conn, {
        "event_id": f"discord:message:{msg['id']}", "source": "discord", "type": "message",
        "native_id": str(msg["id"]), "url": url, "author": handle,
        "author_raw": f"{author.get('name')} ({author.get('id')})", "timestamp": to_utc_z(msg["timestamp"]),
        "channel": channel, "parent_id": f"discord:message:{ref['messageId']}" if ref.get("messageId") else None,
        "body": msg.get("content") or "",
        "payload": {
            "guild_id": guild_id, "channel_id": channel_id, "thread": thread,
            "edited": to_utc_z(msg.get("timestampEdited")),
            "mentions": [team.from_discord(m.get("id"), m.get("name")) for m in msg.get("mentions") or []],
            "attachments": [a.get("fileName") or a.get("url") for a in msg.get("attachments") or []],
            "hash_refs": [int(x) for x in HASH_RE.findall(msg.get("content") or "")],
        },
    })
    return True


def load_export_file(conn: sqlite3.Connection, team: Team, path: str | Path) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ch = data["channel"]
    skip = db.opted_out(conn)
    n = sum(
        ingest_message(conn, team, m, ch.get("name"), data["guild"]["id"], ch["id"], skip)
        for m in data["messages"]
    )
    conn.commit()
    return n


def load_offline(conn: sqlite3.Connection, team: Team, folder: str | Path) -> dict:
    files = sorted(Path(folder).glob("*.json"))
    return {"messages": sum(load_export_file(conn, team, f) for f in files), "channels": len(files)}


def link_hash_refs(conn: sqlite3.Connection) -> int:
    """Deterministic task links: '#4' → T-4 if it's an issue; '#17' → whatever PR #17 closes."""
    issues = {r[0] for r in conn.execute("SELECT github_issue FROM tasks WHERE github_issue IS NOT NULL")}
    pr_closes = {
        int(r["native_id"]): json.loads(r["payload"]).get("closes", [])
        for r in conn.execute("SELECT native_id, payload FROM events WHERE type='pull_request'")
    }
    updated = 0
    for row in conn.execute("SELECT event_id, payload, task_ids FROM events WHERE source='discord'").fetchall():
        refs = json.loads(row["payload"]).get("hash_refs", [])
        tids = set(json.loads(row["task_ids"] or "[]"))
        before = set(tids)
        for n in refs:
            if n in pr_closes:
                tids |= {f"T-{i}" for i in pr_closes[n]}
            elif n in issues:
                tids.add(f"T-{n}")
        if tids != before:
            conn.execute("UPDATE events SET task_ids=? WHERE event_id=?", (json.dumps(sorted(tids)), row["event_id"]))
            updated += 1
    conn.commit()
    return updated


def purge_member(conn: sqlite3.Connection, handle: str) -> int:
    """Opt-out: delete a member's Discord messages (and any claims read from them)."""
    ids = [r[0] for r in conn.execute("SELECT event_id FROM events WHERE source='discord' AND author=?", (handle,))]
    conn.executemany("DELETE FROM claims WHERE source_ref=?", [(i,) for i in ids])
    conn.execute("DELETE FROM events WHERE source='discord' AND author=?", (handle,))
    conn.commit()
    return len(ids)
