"""GitHub poller: issues, pull requests, commits, comments and reviews via the free REST API."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from teampulse import config, db
from teampulse.models import Event, issue_refs, to_iso

log = logging.getLogger("teampulse.github")
API = "https://api.github.com"
OVERLAP = timedelta(minutes=2)   # re-read a little to avoid missing items at the boundary


# ---- pure mappers (unit-tested with sample JSON) -------------------------------------------

def issue_to_event(item: dict, repo: str) -> Event:
    pr = item.get("pull_request")
    is_pr = pr is not None
    status = item.get("state")
    if is_pr and pr.get("merged_at"):
        status = "merged"
    elif item.get("state") == "closed" and item.get("state_reason") == "not_planned":
        status = "closed_not_planned"
    milestone = item.get("milestone") or {}
    return Event(
        source="github",
        kind="pr" if is_pr else "issue",
        native_id=f"{repo}#{item['number']}",
        author=(item.get("user") or {}).get("login"),
        author_name=(item.get("user") or {}).get("login"),
        title=item.get("title"),
        body=item.get("body") or "",
        status=status,
        assignees=[a["login"] for a in item.get("assignees") or []],
        url=item.get("html_url"),
        channel=repo,
        created_at=to_iso(item.get("created_at")),
        updated_at=to_iso(item.get("updated_at")),
        extra={
            "number": item["number"],
            "labels": [label["name"] for label in item.get("labels") or []],
            "closed_at": to_iso(item.get("closed_at")),
            "merged_at": to_iso(pr.get("merged_at")) if is_pr else None,
            "milestone": milestone.get("title"),
            "milestone_due": to_iso(milestone.get("due_on")),
            "issue_refs": issue_refs(item.get("title"), item.get("body")),
        },
    )


def commit_to_event(item: dict, repo: str) -> Event:
    message = (item.get("commit") or {}).get("message") or ""
    title, _, rest = message.partition("\n")
    git_author = (item.get("commit") or {}).get("author") or {}
    login = (item.get("author") or {}).get("login")
    return Event(
        source="github",
        kind="commit",
        native_id=f"commit:{item['sha']}",
        author=login or git_author.get("email"),
        author_name=login or git_author.get("name"),
        title=title,
        body=rest.strip(),
        url=item.get("html_url"),
        channel=repo,
        created_at=to_iso(git_author.get("date")),
        updated_at=to_iso(git_author.get("date")),
        extra={"sha": item["sha"], "issue_refs": issue_refs(message)},
    )


def comment_to_event(item: dict, repo: str) -> Event:
    number = int(re.search(r"/issues/(\d+)$", item["issue_url"]).group(1))
    return Event(
        source="github",
        kind="issue_comment",
        native_id=f"comment:{item['id']}",
        author=(item.get("user") or {}).get("login"),
        author_name=(item.get("user") or {}).get("login"),
        body=item.get("body") or "",
        url=item.get("html_url"),
        channel=repo,
        parent_id=f"github:{repo}#{number}",
        created_at=to_iso(item.get("created_at")),
        updated_at=to_iso(item.get("updated_at")),
        extra={"number": number, "issue_refs": issue_refs(item.get("body"))},
    )


def review_to_event(item: dict, repo: str, pr_number: int) -> Event:
    return Event(
        source="github",
        kind="review",
        native_id=f"review:{item['id']}",
        author=(item.get("user") or {}).get("login"),
        author_name=(item.get("user") or {}).get("login"),
        body=item.get("body") or "",
        status=(item.get("state") or "").lower(),     # approved | changes_requested | commented
        url=item.get("html_url"),
        channel=repo,
        parent_id=f"github:{repo}#{pr_number}",
        created_at=to_iso(item.get("submitted_at")),
        updated_at=to_iso(item.get("submitted_at")),
        extra={"number": pr_number},
    )


# ---- fetching ----------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def paginate(self, path: str, params: dict | None = None):
        url = f"{API}{path}"
        params = {"per_page": 100, **(params or {})}
        while url:
            response = self.session.get(url, params=params, timeout=30)
            if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
                raise RuntimeError("GitHub rate limit reached; try again later.")
            response.raise_for_status()
            yield from response.json()
            url = response.links.get("next", {}).get("url")
            params = None   # the "next" link already carries the query


def poll_repo(conn, client: GitHubClient, repo: str, backfill_days: int) -> int:
    key = f"github:since:{repo}"
    started = datetime.now(timezone.utc)
    stored = db.get_state(conn, key)
    since_dt = (datetime.fromisoformat(stored) - OVERLAP) if stored else started - timedelta(days=backfill_days)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    events: list[Event] = []
    updated_prs: list[int] = []
    for item in client.paginate(f"/repos/{repo}/issues", {"state": "all", "since": since}):
        event = issue_to_event(item, repo)
        events.append(event)
        if event.kind == "pr":
            updated_prs.append(item["number"])
    for item in client.paginate(f"/repos/{repo}/commits", {"since": since}):
        events.append(commit_to_event(item, repo))
    for item in client.paginate(f"/repos/{repo}/issues/comments", {"since": since}):
        events.append(comment_to_event(item, repo))
    for number in updated_prs:
        for item in client.paginate(f"/repos/{repo}/pulls/{number}/reviews"):
            if item.get("submitted_at"):
                events.append(review_to_event(item, repo, number))

    count = db.upsert_events(conn, events)
    db.set_state(conn, key, started.isoformat())
    log.info("%s: %d item(s) since %s", repo, count, since)
    return count


def poll_all(conn) -> int:
    repos = config.get_list("GITHUB_REPOS")
    if not repos:
        log.info("GITHUB_REPOS not set; skipping GitHub")
        return 0
    client = GitHubClient(config.require("GITHUB_TOKEN"))
    days = config.get_int("GITHUB_BACKFILL_DAYS", 90)
    return sum(poll_repo(conn, client, repo, days) for repo in repos)
