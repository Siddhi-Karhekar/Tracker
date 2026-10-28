"""The single normalized record every source is converted into."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

ISSUE_REF = re.compile(r"(?<![\w/])#(\d+)\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(value) -> str | None:
    """Normalize datetimes / ISO strings to UTC ISO-8601."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def issue_refs(*texts: str | None) -> list[int]:
    """Find GitHub-style #123 references, used later for linking tasks."""
    found: set[int] = set()
    for text in texts:
        if text:
            found.update(int(n) for n in ISSUE_REF.findall(text))
    return sorted(found)


@dataclass
class Event:
    source: str            # github | notion | discord
    kind: str              # issue | pr | commit | issue_comment | review | notion_page | message
    native_id: str         # id unique within the source
    author: str | None = None        # stable id: github login, notion user id, discord user id
    author_name: str | None = None
    title: str | None = None
    body: str | None = None
    status: str | None = None
    assignees: list[str] = field(default_factory=list)
    url: str | None = None
    channel: str | None = None       # repo, notion database, or discord channel
    parent_id: str | None = None     # event id this replies to / belongs to
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"

    def to_row(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "native_id": self.native_id,
            "author": self.author,
            "author_name": self.author_name,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "assignees": json.dumps(self.assignees),
            "url": self.url,
            "channel": self.channel,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": json.dumps(self.extra, ensure_ascii=False),
            "ingested_at": utc_now(),
        }
