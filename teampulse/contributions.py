"""Contribution reports: what each member did, backed by evidence, with claims checked against it.

Design principles (say these in your report and viva):
  * Evidence, not scores. No ranking, no single number. Commit counts reward small commits and miss
    design, docs, testing, meetings and helping others, so every item is listed and linked instead.
  * Opt-in. Only members who have consented (`!teampulse join` on Discord, or consent in the members
    table) get a report.
  * Self-review first. A member sees their own report, can add context ("I did the UI designs in Figma")
    and approves it before the team view shows it (TEAMPULSE_CONTRIB_REVIEW=off disables this).
  * Careful wording. A claim without matching evidence is "no linked evidence found", never "false":
    plenty of real work never touches GitHub or Notion.

    python -m teampulse.contributions --days 14
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from teampulse import config
from teampulse.models import utc_now

CLAIM_RE = re.compile(r"\b(done|kar diya|kar di|ho gaya|ho gayi|likh diya|bana diya|finish(ed)?|complete(d)?|"
                      r"push kar|deploy kar|merged|fixed)\b|\d+\s*%")
NEAR = timedelta(days=3)
LOOKBACK = timedelta(days=14)
CATEGORIES = ["Code", "Reviews & discussion", "Planning & docs", "Team chat", "Helping others"]


@dataclass
class Member:
    name: str
    github: str | None
    discord: str | None
    notion: str | None
    consent: bool

    def owns(self, source: str, author: str | None) -> bool:
        if not author:
            return False
        key = {"github": self.github, "discord": self.discord, "notion": self.notion}.get(source)
        return bool(key) and str(author).lower() == str(key).lower()


@dataclass
class Report:
    member: Member
    days: int
    items: dict[str, list[dict]] = field(default_factory=lambda: {c: [] for c in CATEGORIES})
    tasks: list[dict] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    review: dict | None = None

    @property
    def counts(self) -> dict[str, int]:
        return {c: len(v) for c, v in self.items.items()}


def members(conn: sqlite3.Connection) -> list[Member]:
    return [Member(r["display_name"], r["github_login"], r["discord_id"], r["notion_user_id"], bool(r["consent"]))
            for r in conn.execute("SELECT * FROM members WHERE display_name IS NOT NULL ORDER BY display_name")]


def review_required() -> bool:
    return (config.get("TEAMPULSE_CONTRIB_REVIEW", "on") or "on").lower() != "off"


def get_review(conn, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM contribution_reviews WHERE member = ?", (name,)).fetchone()
    return dict(row) if row else None


def set_review(conn, name: str, status: str, comment: str = "") -> None:
    if status not in ("approved", "disputed"):
        raise ValueError(f"Unknown review status: {status!r}")
    with conn:
        conn.execute("INSERT OR REPLACE INTO contribution_reviews VALUES (?, ?, ?, ?)",
                     (name, status, comment.strip() or None, utc_now()))


def visible_to_team(conn, member: Member) -> bool:
    if not member.consent:
        return False
    if not review_required():
        return True
    review = get_review(conn, member.name)
    return bool(review) and review["status"] == "approved"


def _item(row, label: str, task: str | None = None) -> dict:
    return {"event_id": row["id"], "label": label, "url": row["url"], "date": (row["created_at"] or "")[:10],
            "task": task, "source": row["source"], "kind": row["kind"]}


def _task_titles(conn) -> dict[str, str]:
    out = {}
    for r in conn.execute("SELECT tl.event_id, t.title, t.github_number FROM task_links tl JOIN tasks t ON t.id = tl.task_id"):
        out[r["event_id"]] = (f"#{r['github_number']} " if r["github_number"] else "") + r["title"]
    return out


def build_report(conn: sqlite3.Connection, member: Member, days: int = 14) -> Report:
    report = Report(member, days, review=get_review(conn, member.name))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    task_of = _task_titles(conn)

    for r in conn.execute("SELECT * FROM events WHERE source='github' AND created_at >= ? ORDER BY created_at", (since,)):
        if not member.owns("github", r["author"]):
            continue
        task = task_of.get(r["id"])
        if r["kind"] == "pr":
            report.items["Code"].append(_item(r, f"Opened PR “{r['title']}” ({r['status']})", task))
        elif r["kind"] == "commit":
            report.items["Code"].append(_item(r, f"Commit: {r['title']}", task))
        elif r["kind"] == "review":
            report.items["Reviews & discussion"].append(_item(r, f"Reviewed a PR ({(r['status'] or '').replace('_', ' ')})", task))
        elif r["kind"] == "issue_comment":
            report.items["Reviews & discussion"].append(_item(r, f"Commented: {(r['body'] or '')[:80]}", task))
        elif r["kind"] == "issue":
            report.items["Planning & docs"].append(_item(r, f"Opened issue “{r['title']}”", task))

    for r in conn.execute("SELECT * FROM events WHERE source='notion' AND updated_at >= ?", (since,)):
        if member.owns("notion", r["author"]):   # Notion only reports the LAST editor of a page
            row = {**dict(r), "created_at": r["updated_at"]}
            report.items["Planning & docs"].append(_item(row, f"Last edited Notion page “{r['title']}”", task_of.get(r["id"])))

    signals = {r["event_id"]: r for r in conn.execute("SELECT * FROM signals")}
    chat = conn.execute("SELECT * FROM events WHERE source='discord' AND created_at >= ? ORDER BY created_at", (since,)).fetchall()
    for r in chat:
        if not member.owns("discord", r["author"]):
            continue
        s = signals.get(r["id"])
        if s is None or s["type"] == "noise":
            continue
        label = {"progress": "Progress update", "decision": "Announced a decision", "blocker": "Raised a blocker",
                 "help_request": "Asked for help/review"}[s["type"]]
        report.items["Team chat"].append(_item(r, f"{label}: {(s['summary'] or r['body'])[:90]}", task_of.get(r["id"])))

    # Helping others: replying to (or following up in the same conversation on) someone else's blocker or request
    by_id = {r["id"]: r for r in conn.execute("SELECT * FROM events WHERE source='discord'")}
    for r in chat:
        if not member.owns("discord", r["author"]):
            continue
        s = signals.get(r["id"])
        if s is None or s["type"] not in ("progress", "decision"):
            continue
        parent = by_id.get(r["parent_id"]) if r["parent_id"] else None
        target = parent if parent is not None and not member.owns("discord", parent["author"]) else None
        if target is None or signals.get(target["id"]) is None or signals[target["id"]]["type"] not in ("blocker", "help_request"):
            continue
        report.items["Helping others"].append(_item(r, f"Responded to {target['author_name']}'s "
                                                       f"{signals[target['id']]['type'].replace('_', ' ')}: "
                                                       f"{(r['body'] or '')[:70]}", task_of.get(r["id"])))

    # Tasks: assigned to them, and tasks they actually did work on
    assigned = {}
    for c in conn.execute("SELECT c.task_id, c.value, c.attribute, t.title, t.github_number FROM claims c "
                          "JOIN tasks t ON t.id = c.task_id WHERE c.attribute IN ('owner','worker') AND c.source != 'chat'"):
        if member.name.lower() in [n.strip().lower() for n in (c["value"] or "").split(",")]:
            entry = assigned.setdefault(c["task_id"], {"task_id": c["task_id"], "title": (f"#{c['github_number']} " if c["github_number"] else "") + c["title"],
                                                       "assigned": False, "worked": False})
            entry["assigned" if c["attribute"] == "owner" else "worked"] = True
    report.tasks = sorted(assigned.values(), key=lambda t: t["title"])
    report.claims = check_claims(conn, member, chat, signals)
    return report


def check_claims(conn, member: Member, chat, signals) -> list[dict]:
    """Chat messages where the member says they did something, compared with what the data shows."""
    out = []
    for r in chat:
        s = signals.get(r["id"])
        if not member.owns("discord", r["author"]) or s is None or s["type"] != "progress" or not CLAIM_RE.search((r["body"] or "").lower()):
            continue
        link = conn.execute("SELECT task_id FROM task_links WHERE event_id = ?", (r["id"],)).fetchone()
        claim = {"event_id": r["id"], "text": r["body"], "url": r["url"], "date": r["created_at"][:10], "task": None,
                 "verdict": "not linked to a task", "evidence": []}
        if link is None:
            out.append(claim)
            continue
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (link["task_id"],)).fetchone()
        claim["task"] = (f"#{task['github_number']} " if task["github_number"] else "") + task["title"]
        at = datetime.fromisoformat(r["created_at"])
        near, older = [], []
        for e in conn.execute("SELECT e.* FROM task_links tl JOIN events e ON e.id = tl.event_id "
                              "WHERE tl.task_id = ? AND tl.role IN ('commit','pr','issue','notion_task','review','comment')",
                              (link["task_id"],)):
            if not member.owns(e["source"], e["author"]):
                continue
            when = datetime.fromisoformat(e["updated_at"] if e["source"] == "notion" else e["created_at"])
            if abs(when - at) <= NEAR:
                near.append(e)
            elif at - LOOKBACK <= when < at:
                older.append(e)
        describe = lambda e: {"event_id": e["id"], "url": e["url"], "date": (e["updated_at"] if e["source"] == "notion" else e["created_at"])[:10],
                              "label": {"commit": "commit", "pr": "PR", "notion_page": "Notion edit", "review": "review",
                                        "issue_comment": "comment", "issue": "issue"}.get(e["kind"], e["kind"])}
        if near:
            claim["verdict"], claim["evidence"] = "supported", [describe(e) for e in near]
        elif older:
            newest = max(older, key=lambda e: e["created_at"])
            gap = (at - datetime.fromisoformat(newest["created_at"])).days
            claim["verdict"] = f"older evidence only (latest {gap} day(s) before the message)"
            claim["evidence"] = [describe(e) for e in older]
        else:
            claim["verdict"] = "no linked evidence found (may be offline work)"
        out.append(claim)
    return out


def to_markdown(report: Report) -> str:
    """Report as Markdown. Every piece of text from GitHub, Notion or chat is escaped; links are http(s) only."""
    from teampulse.ui import md_text, url as safe_url

    m = report.member
    lines = [f"### {md_text(m.name)} — last {report.days} days"]
    if report.review and report.review.get("comment"):
        lines.append(f"> **{md_text(m.name)}'s note:** {md_text(report.review['comment'])}")
    for cat in CATEGORIES:
        items = report.items[cat]
        if items:
            lines.append(f"\n**{cat}** ({len(items)})")
            for it in items[:12]:
                task = f" · {md_text(it['task'])}" if it["task"] else ""
                lines.append(f"- {it['date']} · [{md_text(it['label'])}]({safe_url(it['url'])}){task}")
            if len(items) > 12:
                lines.append(f"- …and {len(items) - 12} more")
    if report.tasks:
        lines.append("\n**Tasks**")
        for t in report.tasks:
            role = " and ".join(x for x, ok in (("assigned", t["assigned"]), ("did work", t["worked"])) if ok)
            lines.append(f"- {md_text(t['title'])} ({role})")
    if report.claims:
        lines.append("\n**Claims in chat vs evidence**")
        for c in report.claims:
            ev = ", ".join(f"[{e['label']} {e['date']}]({safe_url(e['url'])})" for e in c["evidence"][:4])
            task = f" ({md_text(c['task'])})" if c["task"] else ""
            lines.append(f"- {c['date']} “{md_text(c['text'][:80])}”{task} → **{c['verdict']}**" + (f": {ev}" if ev else ""))
    if not any(report.items.values()):
        lines.append("\nNo activity found in GitHub, Notion or chat for this period.")
    return "\n".join(lines)


def main():
    from teampulse import db

    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()
    conn = db.connect()
    for m in members(conn):
        if not m.consent:
            print(f"### {m.name}: not opted in, no report\n")
            continue
        print(to_markdown(build_report(conn, m, args.days)), "\n")


if __name__ == "__main__":
    main()
