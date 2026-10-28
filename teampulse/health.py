"""Connection checks: is each token valid, and can it see what TeamPulse needs?

    python -m teampulse.health

Read-only calls only. Each failed check says what to fix, in plain words.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from teampulse import config

GITHUB = "https://api.github.com"
NOTION = "https://api.notion.com/v1"
DISCORD = "https://discord.com/api/v10"
MESSAGE_CONTENT_INTENT = (1 << 18) | (1 << 19)   # verified / small-bot flag for the Message Content intent


@dataclass
class Check:
    source: str
    name: str
    ok: bool | None          # None = not set up (not an error)
    detail: str
    fix: str = ""


def _get(session, url, **kwargs):
    try:
        return session.get(url, timeout=15, **kwargs)
    except requests.RequestException as exc:
        return exc


def _post(session, url, **kwargs):
    try:
        return session.post(url, timeout=15, **kwargs)
    except requests.RequestException as exc:
        return exc


def _offline(source, name, exc) -> Check:
    return Check(source, name, False, f"Couldn't reach the service ({type(exc).__name__}).",
                 "Check your internet connection or proxy, then try again.")


def check_github(session=requests, token: str | None = None, repos: list[str] | None = None) -> list[Check]:
    token = token if token is not None else config.get("GITHUB_TOKEN")
    repos = repos if repos is not None else config.get_list("GITHUB_REPOS")
    if not token:
        return [Check("github", "Token", None, "Not set.", "Create a fine-grained token (README, GitHub) and set GITHUB_TOKEN.")]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    out = []
    r = _get(session, f"{GITHUB}/user", headers=headers)
    if isinstance(r, Exception):
        return [_offline("github", "Token", r)]
    if r.status_code == 401:
        return [Check("github", "Token", False, "GitHub rejected the token.", "It may be expired or mistyped: create a new one.")]
    out.append(Check("github", "Token", r.status_code == 200, f"Signed in as {r.json().get('login', '?')}."
                     if r.status_code == 200 else f"Unexpected reply ({r.status_code})."))
    if not repos:
        out.append(Check("github", "Repositories", None, "None listed.", "Set GITHUB_REPOS=owner/repo."))
    for repo in repos:
        r = _get(session, f"{GITHUB}/repos/{repo}", headers=headers)
        if isinstance(r, Exception):
            out.append(_offline("github", repo, r))
        elif r.status_code == 200:
            issues = _get(session, f"{GITHUB}/repos/{repo}/issues", headers=headers, params={"per_page": 1})
            if not isinstance(issues, Exception) and issues.status_code == 200:
                out.append(Check("github", repo, True, "Can read code, issues and pull requests."))
            else:
                out.append(Check("github", repo, False, "Can see the repo but not its issues.",
                                 "Give the token read access to Issues and Pull requests."))
        else:
            out.append(Check("github", repo, False, "The token can't see this repository.",
                             "Check the spelling (owner/repo) and give the fine-grained token access to it."))
    return out


def check_notion(session=requests, token: str | None = None, databases: list[str] | None = None,
                 pages: list[str] | None = None) -> list[Check]:
    token = token if token is not None else config.get("NOTION_TOKEN")
    databases = databases if databases is not None else config.get_list("NOTION_DATABASE_IDS")
    pages = pages if pages is not None else config.get_list("NOTION_PAGE_IDS")
    if not token:
        return [Check("notion", "Integration", None, "Not set.", "Create an internal integration (README, Notion) and set NOTION_TOKEN.")]
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}
    r = _get(session, f"{NOTION}/users/me", headers=headers)
    if isinstance(r, Exception):
        return [_offline("notion", "Integration", r)]
    if r.status_code != 200:
        return [Check("notion", "Integration", False, "Notion rejected the integration secret.",
                      "Copy the secret again from notion.so/profile/integrations.")]
    out = [Check("notion", "Integration", True, f"Connected as {r.json().get('name') or 'your integration'}.")]
    if not databases and not pages:
        out.append(Check("notion", "Pages", None, "No task database or plan page listed.",
                         "Set NOTION_DATABASE_IDS (and NOTION_PAGE_IDS for plan pages)."))
    for db_id in databases:
        r = _post(session, f"{NOTION}/databases/{db_id}/query", headers=headers, json={"page_size": 1})
        if isinstance(r, Exception):
            out.append(_offline("notion", f"Database {db_id[:8]}", r))
        elif r.status_code == 200:
            out.append(Check("notion", f"Database {db_id[:8]}", True, "Can read the task database."))
        elif r.status_code == 404:
            out.append(Check("notion", f"Database {db_id[:8]}", False, "The integration can't see this database.",
                             "Open the database in Notion, then ••• → Connections → add your integration."))
        else:
            message = (r.json() or {}).get("message", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            out.append(Check("notion", f"Database {db_id[:8]}", False, f"Notion refused the query ({r.status_code}). {message}",
                             "If this database has several data sources, give TeamPulse one plain task database."))
    for page_id in pages:
        r = _get(session, f"{NOTION}/pages/{page_id}", headers=headers)
        ok = not isinstance(r, Exception) and r.status_code == 200
        out.append(Check("notion", f"Page {page_id[:8]}", ok, "Can read the plan page." if ok else "Can't read this page.",
                         "" if ok else "Share the page with your integration (••• → Connections)."))
    return out


def check_discord(session=requests, token: str | None = None, guild: str | None = None) -> list[Check]:
    token = token if token is not None else config.get("DISCORD_BOT_TOKEN")
    guild = guild if guild is not None else config.get("DISCORD_GUILD_ID")
    if not token:
        return [Check("discord", "Bot", None, "Not set.", "Create a bot (README, Discord) and set DISCORD_BOT_TOKEN.")]
    headers = {"Authorization": f"Bot {token}"}
    r = _get(session, f"{DISCORD}/users/@me", headers=headers)
    if isinstance(r, Exception):
        return [_offline("discord", "Bot", r)]
    if r.status_code != 200:
        return [Check("discord", "Bot", False, "Discord rejected the bot token.",
                      "Reset the token in the Developer Portal (Bot tab) and paste the new one.")]
    out = [Check("discord", "Bot", True, f"Logged in as {r.json().get('username', 'your bot')}.")]
    app = _get(session, f"{DISCORD}/applications/@me", headers=headers)
    if not isinstance(app, Exception) and app.status_code == 200:
        has_intent = bool((app.json().get("flags") or 0) & MESSAGE_CONTENT_INTENT)
        out.append(Check("discord", "Message Content intent", has_intent,
                         "Switched on." if has_intent else "Switched off, so the bot would receive empty messages.",
                         "" if has_intent else "Developer Portal → Bot → turn on Message Content Intent → Save."))
    if not guild:
        out.append(Check("discord", "Team server", None, "No server ID set.", "Set DISCORD_GUILD_ID (right-click the server → Copy Server ID)."))
    else:
        g = _get(session, f"{DISCORD}/guilds/{guild}", headers=headers)
        ok = not isinstance(g, Exception) and g.status_code == 200
        out.append(Check("discord", "Team server", ok, f"Bot is in “{g.json().get('name')}”." if ok else
                         "The bot isn't in this server, or the ID is wrong.",
                         "" if ok else "Invite the bot with the OAuth2 URL from the README, and check DISCORD_GUILD_ID."))
    return out


def check_all(session=requests) -> list[Check]:
    return check_github(session) + check_notion(session) + check_discord(session)


def process_status(conn) -> dict:
    """Is each background process alive? 'ok', 'stale' (no heartbeat for 3 intervals) or 'never' (not started).

    Only processes whose source is configured are judged; the others are 'not used'."""
    from datetime import datetime, timezone

    from teampulse import db
    limits = {"poller": 3 * config.get_int("POLL_MINUTES", 5) * 60, "discord": 3 * 300}
    needed = {"poller": bool(config.get("GITHUB_TOKEN") or config.get("NOTION_TOKEN")),
              "discord": bool(config.get("DISCORD_BOT_TOKEN"))}
    out = {}
    for process, key in db.HEARTBEAT.items():
        last = db.get_state(conn, key)
        if not needed[process]:
            out[process] = {"state": "not used", "last": last}
            continue
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() if last else None
        out[process] = {"state": "never" if age is None else "ok" if age <= limits[process] else "stale",
                        "last": last}
    return out


def main():
    import sys
    if sys.argv[1:] == ["status"]:   # for uptime monitors / cron: exit code 1 if a needed process is down
        import json

        from teampulse import db
        status = process_status(db.connect())
        print(json.dumps(status, indent=2))
        sys.exit(0 if all(v["state"] in ("ok", "not used") for v in status.values()) else 1)
    for c in check_all():
        mark = {True: "OK  ", False: "FIX ", None: "--  "}[c.ok]
        print(f"{mark} {c.source:8} {c.name:28} {c.detail}" + (f"\n{'':44}→ {c.fix}" if c.fix and c.ok is not True else ""))


if __name__ == "__main__":
    main()
