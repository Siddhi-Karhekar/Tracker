"""Ingestion pipeline (plain code, not an agent): GitHub, Notion, Discord → SQLite events + structured claims."""
from __future__ import annotations

from datetime import datetime, timezone


def to_utc_z(value: str | datetime | None) -> str | None:
    """Any ISO-8601 string or datetime → 'YYYY-MM-DDTHH:MM:SSZ' in UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
