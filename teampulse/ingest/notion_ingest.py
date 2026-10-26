"""Notion task database → events + claims.

Expected properties (rename with env vars if your board differs):
  title (any name)                   task name
  NOTION_PROP_STATUS   = "Status"    status or select: Not started / In progress / Done / Dropped
  NOTION_PROP_ASSIGNEE = "Assignee"  people
  NOTION_PROP_DUE      = "Due"       date
  NOTION_PROP_ISSUE    = "GitHub Issue"  number (links the page to issue #n → task T-n)

Claims carry the page's last_edited_time: Notion doesn't say which property changed,
which is exactly what lets the Discrepancy Agent spot a stale plan.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from teampulse import db
from teampulse.config import Team, env
from teampulse.ingest import to_utc_z

STATUS_MAP = {
    "not started": "todo", "to do": "todo", "todo": "todo", "backlog": "todo",
    "in progress": "in_progress", "doing": "in_progress", "in review": "in_progress",
    "blocked": "in_progress",
    "done": "done", "complete": "done", "completed": "done",
    "dropped": "dropped", "cancelled": "dropped", "canceled": "dropped", "won't do": "dropped",
}


def prop_names() -> dict:
    return {
        "status": env("NOTION_PROP_STATUS", "Status"),
        "assignee": env("NOTION_PROP_ASSIGNEE", "Assignee"),
        "due": env("NOTION_PROP_DUE", "Due"),
        "issue": env("NOTION_PROP_ISSUE", "GitHub Issue"),
    }


def _title(props: dict) -> str:
    for p in props.values():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p["title"]).strip()
    return "(untitled)"


def _status(prop: dict | None) -> str | None:
    if not prop:
        return None
    val = prop.get("status") or prop.get("select")
    return val.get("name") if val else None


def _assignees(team: Team, prop: dict | None) -> list[str]:
    """People property → member handles. A select/multi-select of handles also works (seeded demo boards)."""
    if not prop:
        return []
    if prop.get("type") == "people":
        return [team.from_notion(p["id"]) for p in prop.get("people", [])]
    if prop.get("type") == "select" and prop.get("select"):
        return [prop["select"]["name"].lower()]
    if prop.get("type") == "multi_select":
        return [o["name"].lower() for o in prop.get("multi_select", [])]
    return []


def task_id_for(page: dict, issue_number: int | None) -> str:
    return f"T-{issue_number}" if issue_number else f"N-{page['id'][:8]}"


def ingest_page(conn: sqlite3.Connection, team: Team, page: dict) -> str | None:
    if page.get("archived") or page.get("in_trash"):
        return None
    names = prop_names()
    props = page["properties"]
    title = _title(props)
    issue_prop = props.get(names["issue"]) or {}
    issue_number = issue_prop.get("number")
    if issue_number is None and issue_prop.get("type") == "rich_text":  # "#12" typed as text
        txt = "".join(t.get("plain_text", "") for t in issue_prop["rich_text"]).lstrip("#").strip()
        issue_number = int(txt) if txt.isdigit() else None
    tid = task_id_for(page, issue_number)
    eid = f"notion:notion_page:{page['id']}"
    edited_at = to_utc_z(page["last_edited_time"])
    editor = team.from_notion((page.get("last_edited_by") or {}).get("id"))

    status_raw = _status(props.get(names["status"]))
    status = STATUS_MAP.get((status_raw or "").lower(), (status_raw or "").lower() or None)
    assignees = _assignees(team, props.get(names["assignee"]))
    due = ((props.get(names["due"]) or {}).get("date") or {}).get("start")

    db.upsert_task(conn, tid, title, github_issue=issue_number, notion_page_id=page["id"])
    db.upsert_event(conn, {
        "event_id": eid, "source": "notion", "type": "notion_page", "native_id": page["id"], "url": page.get("url"),
        "author": editor, "author_raw": (page.get("last_edited_by") or {}).get("id"), "timestamp": edited_at,
        "title": title,
        "body": f"{title} | status: {status_raw} | assignee: {', '.join(a or '?' for a in assignees)} | due: {due}",
        "task_ids": [tid],
        "payload": {"status_raw": status_raw, "status": status, "assignees": assignees, "due": due,
                    "github_issue": issue_number, "created_time": to_utc_z(page.get("created_time")),
                    "last_edited_by": editor},
    })
    base = {"task_id": tid, "source": "notion", "source_ref": eid, "author": editor, "timestamp": edited_at,
            "extractor": "rule:notion"}
    if status:
        db.add_claim(conn, {**base, "attribute": "status", "value": status})
    for a in assignees:
        db.add_claim(conn, {**base, "attribute": "owner", "value": a})
    if due:
        db.add_claim(conn, {**base, "attribute": "deadline", "value": due[:10]})
    return tid


def load_offline(conn: sqlite3.Connection, team: Team, path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    pages = data["results"] if isinstance(data, dict) else data
    for p in pages:
        ingest_page(conn, team, p)
    conn.commit()
    return {"pages": len(pages)}


def _data_source_id(client, database_id: str) -> str | None:
    """API 2025-09-03: a database holds one or more data sources; tasks live in the first."""
    if not hasattr(client, "data_sources"):
        return None
    dbinfo = client.databases.retrieve(database_id=database_id)
    sources = dbinfo.get("data_sources") or []
    return sources[0]["id"] if sources else None


def poll(conn: sqlite3.Connection, team: Team, token: str, database_id: str,
         snapshot_dir: Path | None = None) -> dict:
    from notion_client import Client

    client = Client(auth=token)
    ds_id = _data_source_id(client, database_id)
    scope = ds_id or database_id
    cursor = db.get_cursor(conn, "notion", scope)

    body: dict = {"sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}], "page_size": 100}
    if cursor:
        body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cursor}}

    pages, start = [], None
    while True:
        kwargs = {**body, **({"start_cursor": start} if start else {})}
        if ds_id:
            res = client.data_sources.query(data_source_id=ds_id, **kwargs)
        else:  # older notion-client / API versions
            res = client.databases.query(database_id=database_id, **kwargs)
        pages += res["results"]
        if not res.get("has_more"):
            break
        start = res["next_cursor"]

    newest = cursor
    for p in pages:
        ingest_page(conn, team, p)
        t = to_utc_z(p["last_edited_time"])
        newest = max(newest or t, t)
    if newest:
        db.set_cursor(conn, "notion", scope, newest)
    conn.commit()
    if snapshot_dir:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / "tasks.json").write_text(
            json.dumps({"data_source_id": ds_id, "database_id": database_id, "results": pages}, indent=1),
            encoding="utf-8")
    return {"pages": len(pages)}
