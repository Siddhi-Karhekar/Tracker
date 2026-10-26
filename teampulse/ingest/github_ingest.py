"""GitHub → events + claims.

Works on GitHub REST shapes, so the same code handles the seed files (data/demo/github)
and live data from PyGithub (`obj.raw_data`).

Claims produced (deterministic, extractor "rule:github"):
  issue   → status (open / done / dropped), owner (each assignee), deadline (milestone due date)
  PR      → for each issue it closes: owner (PR author), status (in_progress while open, done when merged)
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from teampulse import db
from teampulse.config import Team
from teampulse.ingest import to_utc_z

CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.I)
HASH_RE = re.compile(r"(?<![\w/])#(\d+)\b")


def task_id(issue_number: int) -> str:
    return f"T-{issue_number}"


def _login(user: dict | None) -> str | None:
    return user.get("login") if user else None


# ---------------------------------------------------------------------------
# issues
# ---------------------------------------------------------------------------
def ingest_issue(conn: sqlite3.Connection, team: Team, bundle: dict) -> None:
    issue, comments, events = bundle["issue"], bundle.get("comments", []), bundle.get("events", [])
    n = issue["number"]
    tid = task_id(n)
    eid = f"github:issue:{n}"
    db.upsert_task(conn, tid, issue["title"], github_issue=n)
    author_raw = _login(issue.get("user"))
    db.upsert_event(conn, {
        "event_id": eid, "source": "github", "type": "issue", "native_id": str(n), "url": issue.get("html_url"),
        "author": team.from_github(author_raw), "author_raw": author_raw,
        "timestamp": to_utc_z(issue["created_at"]), "title": issue["title"], "body": issue.get("body") or "",
        "task_ids": [tid],
        "payload": {
            "state": issue["state"], "state_reason": issue.get("state_reason"),
            "assignees": [team.from_github(_login(a)) for a in issue.get("assignees") or []],
            "labels": [lb["name"] for lb in issue.get("labels") or []],
            "milestone": (issue.get("milestone") or {}).get("title"),
            "due_on": (issue.get("milestone") or {}).get("due_on"),
            "closed_at": to_utc_z(issue.get("closed_at")), "updated_at": to_utc_z(issue.get("updated_at")),
        },
    })

    # status: when did the current state begin? last close/reopen event, else creation
    state_events = [e for e in events if e.get("event") in ("closed", "reopened")]
    if issue["state"] == "closed":
        status = "dropped" if issue.get("state_reason") == "not_planned" else "done"
        since = issue.get("closed_at") or (state_events[-1]["created_at"] if state_events else issue["created_at"])
        actor = _login((state_events[-1] if state_events else {}).get("actor"))
    else:
        status = "open"
        reopened = [e for e in state_events if e["event"] == "reopened"]
        since = reopened[-1]["created_at"] if reopened else issue["created_at"]
        actor = _login(reopened[-1]["actor"]) if reopened else author_raw
    db.add_claim(conn, {"task_id": tid, "attribute": "status", "value": status, "source": "github",
                        "source_ref": eid, "author": team.from_github(actor), "timestamp": to_utc_z(since),
                        "extractor": "rule:github"})

    assigned = {_login(e.get("assignee")): e["created_at"] for e in events if e.get("event") == "assigned"}
    for a in issue.get("assignees") or []:
        login = _login(a)
        db.add_claim(conn, {"task_id": tid, "attribute": "owner", "value": team.from_github(login),
                            "source": "github", "source_ref": eid, "author": team.from_github(author_raw),
                            "timestamp": to_utc_z(assigned.get(login, issue["created_at"])),
                            "extractor": "rule:github"})

    ms = issue.get("milestone")
    if ms and ms.get("due_on"):
        db.add_claim(conn, {"task_id": tid, "attribute": "deadline", "value": ms["due_on"][:10],
                            "source": "github", "source_ref": eid, "author": team.from_github(author_raw),
                            "timestamp": to_utc_z(issue["created_at"]), "extractor": "rule:github"})

    for c in comments:
        login = _login(c.get("user"))
        db.upsert_event(conn, {
            "event_id": f"github:issue_comment:{c['id']}", "source": "github", "type": "issue_comment",
            "native_id": str(c["id"]), "url": c.get("html_url"), "author": team.from_github(login),
            "author_raw": login, "timestamp": to_utc_z(c["created_at"]), "parent_id": eid,
            "body": c.get("body") or "", "task_ids": [tid],
        })
    for e in state_events:
        login = _login(e.get("actor"))
        db.upsert_event(conn, {
            "event_id": f"github:issue_event:{e['id']}", "source": "github", "type": f"issue_{e['event']}",
            "native_id": str(e["id"]), "author": team.from_github(login), "author_raw": login,
            "timestamp": to_utc_z(e["created_at"]), "parent_id": eid, "task_ids": [tid],
            "body": f"{e['event']} #{n}",
        })


# ---------------------------------------------------------------------------
# pull requests
# ---------------------------------------------------------------------------
def linked_issues(pull: dict) -> list[int]:
    text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
    return sorted({int(n) for n in CLOSES_RE.findall(text)})


def ingest_pull(conn: sqlite3.Connection, team: Team, bundle: dict) -> None:
    pull, reviews, commits = bundle["pull"], bundle.get("reviews", []), bundle.get("commits", [])
    n = pull["number"]
    eid = f"github:pull_request:{n}"
    closes = linked_issues(pull)
    tids = [task_id(i) for i in closes]
    author_raw = _login(pull.get("user"))
    author = team.from_github(author_raw)
    db.upsert_event(conn, {
        "event_id": eid, "source": "github", "type": "pull_request", "native_id": str(n),
        "url": pull.get("html_url"), "author": author, "author_raw": author_raw,
        "timestamp": to_utc_z(pull["created_at"]), "title": pull["title"], "body": pull.get("body") or "",
        "task_ids": tids,
        "payload": {"state": pull["state"], "draft": pull.get("draft", False),
                    "merged_at": to_utc_z(pull.get("merged_at")),
                    "merged_by": team.from_github(_login(pull.get("merged_by"))),
                    "branch": (pull.get("head") or {}).get("ref"), "closes": closes},
    })
    for tid in tids:
        db.add_claim(conn, {"task_id": tid, "attribute": "owner", "value": author, "source": "github",
                            "source_ref": eid, "author": author, "timestamp": to_utc_z(pull["created_at"]),
                            "confidence": 0.9, "extractor": "rule:github-pr"})
        if pull.get("merged_at"):
            db.add_claim(conn, {"task_id": tid, "attribute": "status", "value": "done", "source": "github",
                                "source_ref": eid, "author": team.from_github(_login(pull.get("merged_by"))),
                                "timestamp": to_utc_z(pull["merged_at"]), "confidence": 0.9,
                                "extractor": "rule:github-pr"})
        elif pull["state"] == "open":
            db.add_claim(conn, {"task_id": tid, "attribute": "status", "value": "in_progress",
                                "source": "github", "source_ref": eid, "author": author,
                                "timestamp": to_utc_z(pull["created_at"]), "confidence": 0.8,
                                "extractor": "rule:github-pr"})

    for r in reviews:
        login = _login(r.get("user"))
        db.upsert_event(conn, {
            "event_id": f"github:review:{r['id']}", "source": "github", "type": "review", "native_id": str(r["id"]),
            "url": r.get("html_url"), "author": team.from_github(login), "author_raw": login,
            "timestamp": to_utc_z(r.get("submitted_at")), "parent_id": eid, "body": r.get("body") or "",
            "task_ids": tids, "payload": {"state": r.get("state"), "pull": n},
        })
    for c in commits:
        ingest_commit(conn, team, c, pull_number=n, pull_tasks=tids)


def ingest_commit(conn: sqlite3.Connection, team: Team, c: dict, pull_number: int | None = None,
                  pull_tasks: list[str] | None = None) -> None:
    msg = c["commit"]["message"]
    login = _login(c.get("author")) or c["commit"]["author"].get("name")
    refs = [task_id(int(x)) for x in HASH_RE.findall(msg)]
    db.upsert_event(conn, {
        "event_id": f"github:commit:{c['sha']}", "source": "github", "type": "commit", "native_id": c["sha"],
        "url": c.get("html_url"), "author": team.from_github(login), "author_raw": login,
        "timestamp": to_utc_z(c["commit"]["author"]["date"]),
        "parent_id": f"github:pull_request:{pull_number}" if pull_number else None,
        "title": msg.splitlines()[0], "body": msg,
        "task_ids": sorted(set(refs + (pull_tasks or []))),
        "payload": {"sha": c["sha"], "pull": pull_number},
    })


# ---------------------------------------------------------------------------
# offline (seed files / saved snapshots)
# ---------------------------------------------------------------------------
def load_offline(conn: sqlite3.Connection, team: Team, folder: str | Path) -> dict:
    folder = Path(folder)
    issues = json.loads((folder / "issues.json").read_text(encoding="utf-8"))
    pulls = json.loads((folder / "pulls.json").read_text(encoding="utf-8"))
    commits_path = folder / "commits.json"
    commits = json.loads(commits_path.read_text(encoding="utf-8")) if commits_path.exists() else []
    for b in issues:
        ingest_issue(conn, team, b)
    for b in pulls:
        ingest_pull(conn, team, b)
    pr_shas = {c["sha"] for b in pulls for c in b.get("commits", [])}
    for c in commits:
        if c["sha"] not in pr_shas:  # direct pushes to main
            ingest_commit(conn, team, c)
    conn.commit()
    return {"issues": len(issues), "pulls": len(pulls), "commits": len(commits)}


# ---------------------------------------------------------------------------
# live polling
# ---------------------------------------------------------------------------
STATE_EVENTS = {"closed", "reopened", "assigned", "unassigned"}


def poll(conn: sqlite3.Connection, team: Team, token: str, repo_name: str, snapshot_dir: Path | None = None) -> dict:
    """Fetch everything updated since the last cursor. Safe to run repeatedly (cron / --watch)."""
    from datetime import datetime

    from github import Auth, Github

    gh = Github(auth=Auth.Token(token), per_page=100)
    repo = gh.get_repo(repo_name)
    cursor = db.get_cursor(conn, "github", repo_name)
    since = datetime.fromisoformat(cursor.replace("Z", "+00:00")) if cursor else None
    newest = cursor
    counts = {"issues": 0, "pulls": 0, "commits": 0}
    snapshot = {"issues": [], "pulls": [], "commits": []}

    kwargs = {"state": "all", "sort": "updated", "direction": "asc"}
    if since:
        kwargs["since"] = since
    for item in repo.get_issues(**kwargs):
        if item.pull_request:
            pr = repo.get_pull(item.number)
            bundle = {"pull": pr.raw_data, "reviews": [r.raw_data for r in pr.get_reviews()],
                      "commits": [c.raw_data for c in pr.get_commits()]}
            ingest_pull(conn, team, bundle)
            snapshot["pulls"].append(bundle)
            counts["pulls"] += 1
        else:
            bundle = {"issue": item.raw_data, "comments": [c.raw_data for c in item.get_comments()],
                      "events": [e.raw_data for e in item.get_events() if e.event in STATE_EVENTS]}
            ingest_issue(conn, team, bundle)
            snapshot["issues"].append(bundle)
            counts["issues"] += 1
        updated = to_utc_z(item.raw_data["updated_at"])
        newest = max(newest or updated, updated)

    for c in repo.get_commits(since=since) if since else repo.get_commits():
        ingest_commit(conn, team, c.raw_data)
        snapshot["commits"].append(c.raw_data)
        counts["commits"] += 1

    if newest:
        db.set_cursor(conn, "github", repo_name, newest)
    conn.commit()
    if snapshot_dir:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for k, v in snapshot.items():
            (snapshot_dir / f"{k}.json").write_text(json.dumps(v, indent=1, default=str), encoding="utf-8")
    return counts
