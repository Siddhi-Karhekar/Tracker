"""Notion poller: task databases and standalone plan pages, including page text and to-do checkboxes.

Uses the REST API directly with a pinned Notion-Version so behaviour doesn't change under you.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from teampulse import config, db
from teampulse.models import Event, issue_refs, to_iso

log = logging.getLogger("teampulse.notion")
API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_BLOCKS = 300


# ---- pure mappers --------------------------------------------------------------------------

def rich_text(items) -> str:
    return "".join(part.get("plain_text", "") for part in items or [])


def property_value(prop: dict):
    """Turn any Notion property into plain Python (str / list / None)."""
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return rich_text(value)
    if kind in ("select", "status"):
        return (value or {}).get("name")
    if kind == "multi_select":
        return [option["name"] for option in value or []]
    if kind == "people":
        return [person.get("name") or person["id"] for person in value or []]
    if kind == "date":
        return (value or {}).get("start")
    if kind == "checkbox":
        return bool(value)
    if kind in ("number", "url", "email", "phone_number", "created_time", "last_edited_time"):
        return value
    if kind in ("created_by", "last_edited_by"):
        return (value or {}).get("name") or (value or {}).get("id")
    if kind == "relation":
        return [rel["id"] for rel in value or []]
    if kind == "formula":
        return (value or {}).get((value or {}).get("type"))
    if kind == "unique_id":
        prefix = (value or {}).get("prefix")
        number = (value or {}).get("number")
        return f"{prefix}-{number}" if prefix else number
    return None


def _pick(properties: dict, wanted_name: str | None, types: tuple[str, ...]):
    if wanted_name and wanted_name in properties:
        return property_value(properties[wanted_name])
    for prop in properties.values():
        if prop.get("type") in types:
            return property_value(prop)
    return None


def blocks_to_text(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        kind = block.get("type")
        data = block.get(kind) or {}
        text = rich_text(data.get("rich_text"))
        if kind == "to_do":
            text = f"[{'x' if data.get('checked') else ' '}] {text}"
        elif kind in ("heading_1", "heading_2", "heading_3"):
            text = f"{'#' * int(kind[-1])} {text}"
        elif kind in ("bulleted_list_item", "numbered_list_item"):
            text = f"- {text}"
        elif kind == "child_page":
            text = f"[page] {data.get('title', '')}"
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


def page_to_event(page: dict, body: str, channel: str) -> Event:
    props = page.get("properties") or {}
    title = _pick(props, None, ("title",)) or "(untitled)"
    status = _pick(props, config.get("NOTION_PROP_STATUS"), ("status",))
    assignees = _pick(props, config.get("NOTION_PROP_ASSIGNEE"), ("people",)) or []
    due = _pick(props, config.get("NOTION_PROP_DUE"), ("date",))
    editor = page.get("last_edited_by") or {}
    creator = page.get("created_by") or {}
    all_props = {name: property_value(prop) for name, prop in props.items()}
    return Event(
        source="notion",
        kind="notion_page",
        native_id=page["id"],
        author=editor.get("id"),
        author_name=editor.get("name"),
        title=title,
        body=body,
        status="archived" if page.get("archived") or page.get("in_trash") else status,
        assignees=assignees if isinstance(assignees, list) else [assignees],
        url=page.get("url"),
        channel=channel,
        created_at=to_iso(page.get("created_time")),
        updated_at=to_iso(page.get("last_edited_time")),
        extra={
            "due": due,
            "created_by": creator.get("id"),
            "last_edited_by": editor.get("id"),
            "properties": all_props,
            "issue_refs": issue_refs(title, body),
        },
    )


# ---- fetching ----------------------------------------------------------------------------

class NotionClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self.session.request(method, f"{API}{path}", timeout=30, **kwargs)
        if response.status_code == 404:
            raise RuntimeError(
                f"Notion returned 404 for {path}. Share that page/database with your integration "
                "(••• menu → Connections)."
            )
        response.raise_for_status()
        return response.json()

    def query_database(self, database_id: str, edited_after: str | None):
        body: dict = {"page_size": 100, "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]}
        if edited_after:
            body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": edited_after}}
        while True:
            data = self._request("POST", f"/databases/{database_id}/query", json=body)
            yield from data["results"]
            if not data.get("has_more"):
                break
            body["start_cursor"] = data["next_cursor"]

    def database_title(self, database_id: str) -> str:
        data = self._request("GET", f"/databases/{database_id}")
        return rich_text(data.get("title")) or database_id

    def page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def page_text(self, page_id: str) -> str:
        blocks, cursor = [], None
        while len(blocks) < MAX_BLOCKS:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = self._request("GET", f"/blocks/{page_id}/children", params=params)
            blocks.extend(data["results"])
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        return blocks_to_text(blocks)


def poll_database(conn, client: NotionClient, database_id: str, backfill_days: int) -> int:
    key = f"notion:edited_after:{database_id}"
    cursor = db.get_state(conn, key)
    if cursor is None:
        cursor = to_iso(datetime.now(timezone.utc) - timedelta(days=backfill_days))
    name = client.database_title(database_id)
    events, newest = [], cursor
    for page in client.query_database(database_id, cursor):
        # Notion rounds edit times to the minute, so the boundary page may be re-read; skip unchanged ones.
        edited = to_iso(page["last_edited_time"])
        known = conn.execute("SELECT updated_at FROM events WHERE id = ?", (f"notion:{page['id']}",)).fetchone()
        if known and known["updated_at"] == edited:
            continue
        events.append(page_to_event(page, client.page_text(page["id"]), name))
        newest = max(newest, edited)
    count = db.upsert_events(conn, events)
    db.set_state(conn, key, newest)
    log.info("Notion database '%s': %d page(s) updated", name, count)
    return count


def poll_page(conn, client: NotionClient, page_id: str) -> int:
    page = client.page(page_id)
    existing = conn.execute(
        "SELECT updated_at FROM events WHERE id = ?", (f"notion:{page['id']}",)
    ).fetchone()
    if existing and existing["updated_at"] == to_iso(page["last_edited_time"]):
        return 0
    db.upsert_events(conn, [page_to_event(page, client.page_text(page["id"]), "plan pages")])
    log.info("Notion page '%s' updated", _pick(page.get("properties") or {}, None, ("title",)))
    return 1


def poll_all(conn) -> int:
    databases = config.get_list("NOTION_DATABASE_IDS")
    pages = config.get_list("NOTION_PAGE_IDS")
    if not databases and not pages:
        log.info("No Notion IDs set; skipping Notion")
        return 0
    client = NotionClient(config.require("NOTION_TOKEN"))
    days = config.get_int("NOTION_BACKFILL_DAYS", 90)
    total = sum(poll_database(conn, client, d, days) for d in databases)
    total += sum(poll_page(conn, client, p) for p in pages)
    return total
