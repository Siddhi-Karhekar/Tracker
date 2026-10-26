"""Settings from .env and the team roster from team.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value if value not in ("", None) else default


@dataclass
class Member:
    handle: str
    display_name: str
    github_login: str | None = None
    notion_user_id: str | None = None
    discord_id: str | None = None
    discord_name: str | None = None
    opted_in: bool = True


@dataclass
class Team:
    members: list[Member] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_github = {m.github_login.lower(): m for m in self.members if m.github_login}
        self._by_notion = {m.notion_user_id: m for m in self.members if m.notion_user_id}
        self._by_discord_id = {m.discord_id: m for m in self.members if m.discord_id}
        self._by_discord_name = {m.discord_name.lower(): m for m in self.members if m.discord_name}

    def from_github(self, login: str | None) -> str | None:
        if not login:
            return None
        m = self._by_github.get(login.lower())
        return m.handle if m else login

    def from_notion(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        m = self._by_notion.get(user_id)
        return m.handle if m else user_id

    def from_discord(self, user_id: str | None, name: str | None = None) -> str | None:
        m = self._by_discord_id.get(str(user_id)) if user_id else None
        if not m and name:
            m = self._by_discord_name.get(name.lower())
        return m.handle if m else (name or (str(user_id) if user_id else None))

    def get(self, handle: str) -> Member | None:
        return next((m for m in self.members if m.handle == handle), None)


def load_team(path: str | Path | None = None) -> Team:
    path = Path(path or env("TEAMPULSE_TEAM", str(ROOT / "team.toml")))
    with open(path, "rb") as f:
        data = tomllib.load(f)
    members = []
    for handle, m in data.get("members", {}).items():
        members.append(
            Member(
                handle=handle,
                display_name=m.get("display_name", handle),
                github_login=m.get("github"),
                notion_user_id=m.get("notion_user_id"),
                discord_id=str(m["discord_id"]) if m.get("discord_id") else None,
                discord_name=m.get("discord_name"),
                opted_in=m.get("opted_in", True),
            )
        )
    return Team(members)


def db_path() -> Path:
    p = Path(env("TEAMPULSE_DB", str(ROOT / "data" / "teampulse.db")))
    return p if p.is_absolute() else ROOT / p
