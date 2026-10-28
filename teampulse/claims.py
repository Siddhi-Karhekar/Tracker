"""Claims: what each source says about each task, in one comparable format.

    claim = (task, attribute, value, source, event it came from, when, confidence, method)

attributes:
    status    todo | in_progress | in_review | blocked | done | dropped
    owner     a team member's name (who is responsible / doing it)
    deadline  YYYY-MM-DD
    worker    a team member who actually did work on it (commits, PRs)

Sources:
    github    issue state, assignees, milestone due date; merged PRs; commit/PR authors
    notion    task status, assignee, due date
    chat      what Discord messages say (LLM extraction, keyword fallback), linked through tasks.py

The Discrepancy Agent (Week 7) compares these. Everything here is rebuilt from scratch on every refresh
(cheap), except LLM extractions, which are cached per message in the `extractions` table.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import date

from teampulse.llm import LLM, extract_json
from teampulse.models import utc_now

log = logging.getLogger("teampulse.claims")

STATUSES = ["todo", "in_progress", "in_review", "blocked", "done", "dropped"]
STATUS_WORDS = [   # order matters: "not started" must be caught before "started", "not planned" before "planned"
    ("dropped", r"not.?planned|won'?t|cancel|drop|abandon"),
    ("todo", r"not.?started|not.?begun|to.?do|backlog"),
    ("done", r"done|complete|finish|closed|merged|shipped|resolved|live"),
    ("blocked", r"block|stuck|on.?hold|waiting"),
    ("in_review", r"review|testing|qa"),
    ("in_progress", r"progress|doing|started|wip|working|ongoing|active"),
    ("todo", r"open|planned|new|pending|idea"),
]
EXTRACT_BATCH = 15

EXTRACT_PROMPT = """Each chat message below is from a student team's Discord (often Hinglish) and is about the
task named under it. Extract ONLY what the message itself says about that task:
- "status": todo | in_progress | in_review | blocked | done | null
- "owner": name of the person doing the task according to the message, else null.
  "X dekh raha hai" / "X kar raha hai" / "X is handling it" → X. "main kar raha hu" / "I'm on it" → the author.
  Use the names exactly as listed under Team.
- "deadline": an ISO date (YYYY-MM-DD) if one can be worked out from the message date ("kal tak" = next
  day, "Friday tak" = the coming Friday), or a milestone name like "M2", else null.
Return ONLY JSON: {"items": [{"message": 1, "status": ..., "owner": ..., "deadline": ...}, ...]}"""


def norm_status(raw) -> str | None:
    if not raw:
        return None
    text = str(raw).strip().lower()
    if text in STATUSES:
        return text
    for status, pattern in STATUS_WORDS:
        if re.search(pattern, text):
            return status
    return None


class People:
    """Resolve GitHub logins, Discord ids, Notion ids and free-text names to one display name."""

    def __init__(self, conn: sqlite3.Connection):
        self.by_key: dict[str, str] = {}
        self.names: list[str] = []
        for m in conn.execute("SELECT * FROM members"):
            name = m["display_name"]
            if not name:
                continue
            self.names.append(name)
            for key in (m["discord_id"], m["github_login"], m["notion_user_id"], name):
                if key:
                    self.by_key[str(key).lower()] = name

    def resolve(self, raw) -> str | None:
        if not raw:
            return None
        key = str(raw).strip().lower().lstrip("@")
        if key in self.by_key:
            return self.by_key[key]
        for name in self.names:   # "Aman Sharma" → "Aman"
            if re.search(rf"\b{re.escape(name.lower())}\b", key):
                return name
        return str(raw).strip()


def _milestones(conn) -> dict[str, str]:
    out = {}
    for r in conn.execute("SELECT extra FROM events WHERE source='github' AND kind='issue'"):
        extra = json.loads(r["extra"] or "{}")
        if extra.get("milestone") and extra.get("milestone_due"):
            out[extra["milestone"].lower()] = extra["milestone_due"][:10]
    return out


def norm_deadline(raw, milestones: dict[str, str]) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return milestones.get(text.lower())


# ---- chat extraction ------------------------------------------------------------------------------

DONE_RE = re.compile(r"\b(done|kar diya|kar di|ho gaya|ho gayi|merged|completed|finish(ed)?|fixed|live)\b")
DOING_RE = re.compile(r"\b(\w+)\s+(dekh|kar|bana|likh)\s+(raha|rahi)\b|\b(main|mai|i'?m|i am)\b.*\b(kar|dekh)\s+(raha|rahi)|working on")
MILESTONE_RE = re.compile(r"\b(M\d+)\b")


def keyword_extract(text: str, author: str, signal_type: str, resolved: bool, people: People) -> dict:
    t = (text or "").lower()
    out = {"status": None, "owner": None, "deadline": None}
    if signal_type == "blocker" and not resolved:
        out["status"] = "blocked"
    elif DONE_RE.search(t):
        out["status"] = "done"
    match = DOING_RE.search(t)
    if match:
        out["status"] = out["status"] or "in_progress"
        who = match.group(1)
        out["owner"] = people.resolve(who) if who and who not in ("main", "mai", "hu", "ye", "wo") and \
            people.resolve(who) in people.names else author
    m = MILESTONE_RE.search(text or "")
    if m:
        out["deadline"] = m.group(1)
    return out


def extract_chat(conn, llm: LLM | None, people: People) -> dict:
    """Fill `extractions` for linked chat messages that are new or edited."""
    rows = conn.execute(
        "SELECT tl.event_id, tl.task_id, t.title, e.body, e.author_name, e.created_at, e.updated_at, "
        "s.type, s.resolved FROM task_links tl JOIN tasks t ON t.id = tl.task_id "
        "JOIN events e ON e.id = tl.event_id JOIN signals s ON s.event_id = tl.event_id "
        "LEFT JOIN extractions x ON x.event_id = tl.event_id "
        "WHERE tl.role = 'chat' AND (x.event_id IS NULL OR x.event_updated_at IS NOT e.updated_at "
        "OR x.task_id IS NOT tl.task_id OR x.method = 'keyword')").fetchall()
    if not rows:
        return {"extracted": 0, "llm_calls": 0}
    results: dict[str, tuple[dict, str]] = {}
    calls = 0
    if llm is not None:
        team = ", ".join(people.names) or "(unknown)"
        for start in range(0, len(rows), EXTRACT_BATCH):
            batch = rows[start:start + EXTRACT_BATCH]
            text = f"Team: {team}\n\n" + "\n\n".join(
                f"Message {i} [{r['author_name']}, {r['created_at'][:10]}]: {r['body']}\nTask: {r['title']}"
                for i, r in enumerate(batch, 1))
            try:
                reply = llm.chat([{"role": "system", "content": EXTRACT_PROMPT},
                                  {"role": "user", "content": text}], json_mode=True)
                calls += 1
                for item in extract_json(reply.text).get("items", []):
                    if isinstance(item, dict) and str(item.get("message", "")).isdigit():
                        i = int(item["message"])
                        if 1 <= i <= len(batch):
                            results[batch[i - 1]["event_id"]] = (item, f"llm:{reply.provider}")
            except Exception as exc:
                log.warning("Chat claim extraction failed (%s); using keyword rules", exc)
                break
    with conn:
        for r in rows:
            data, method = results.get(r["event_id"]) or (
                keyword_extract(r["body"], r["author_name"], r["type"], bool(r["resolved"]), people), "keyword")
            data = {k: data.get(k) for k in ("status", "owner", "deadline")}
            if not norm_status(data["status"]) and r["type"] == "blocker" and not r["resolved"]:
                data["status"] = "blocked"   # the scanner already knows this message reports a blocker
            conn.execute("INSERT OR REPLACE INTO extractions VALUES (?, ?, ?, ?, ?)",
                         (r["event_id"], r["updated_at"], r["task_id"], json.dumps(data, ensure_ascii=False), method))
    return {"extracted": len(rows), "llm_calls": calls}


# ---- claims ---------------------------------------------------------------------------------------

def build_claims(conn: sqlite3.Connection, people: People | None = None) -> int:
    people = people or People(conn)
    milestones = _milestones(conn)
    claims: list[tuple] = []

    def add(task_id, attribute, value, event, source, confidence, method, author=None, at=None):
        if value in (None, "", []):
            return
        claims.append((task_id, attribute, value, event["id"], source, author or people.resolve(event["author"]),
                       at or event["updated_at"], confidence, method))

    rows = conn.execute("SELECT tl.task_id, tl.role, e.* FROM task_links tl JOIN events e ON e.id = tl.event_id").fetchall()
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        task = r["task_id"]
        if r["role"] == "issue":
            # An open issue only says "not done" (not which stage), so it keeps the value "open".
            status = "dropped" if r["status"] == "closed_not_planned" else ("done" if r["status"] == "closed" else "open")
            add(task, "status", status, r, "github", 1.0, "issue_state")
            for login in json.loads(r["assignees"] or "[]"):
                add(task, "owner", people.resolve(login), r, "github", 1.0, "assignee")
            add(task, "deadline", (extra.get("milestone_due") or "")[:10] or None, r, "github", 1.0,
                f"milestone {extra.get('milestone')}")
        elif r["role"] == "pr":
            if r["status"] == "merged":
                add(task, "status", "done", r, "github", 0.8, "pr_merged")
            elif r["status"] == "open":
                add(task, "status", "in_review", r, "github", 0.7, "pr_open")
            add(task, "worker", people.resolve(r["author"]), r, "github", 1.0, "pr_author", at=r["created_at"])
        elif r["role"] == "commit":
            add(task, "worker", people.resolve(r["author"]), r, "github", 1.0, "commit_author", at=r["created_at"])
        elif r["role"] == "notion_task":
            add(task, "status", norm_status(r["status"]), r, "notion", 1.0, "notion_status")
            for person in json.loads(r["assignees"] or "[]"):
                add(task, "owner", people.resolve(person), r, "notion", 1.0, "notion_assignee")
            add(task, "deadline", norm_deadline(extra.get("due"), milestones), r, "notion", 1.0, "notion_due")
    for x in conn.execute("SELECT x.*, e.author, e.author_name, e.created_at, e.id AS eid FROM extractions x "
                          "JOIN events e ON e.id = x.event_id JOIN task_links tl ON tl.event_id = x.event_id "
                          "AND tl.task_id = x.task_id"):
        data = json.loads(x["data"])
        event = {"id": x["eid"], "author": x["author"], "updated_at": x["created_at"]}
        confidence = 0.7 if x["method"].startswith("llm") else 0.5
        add(x["task_id"], "status", norm_status(data.get("status")), event, "chat", confidence, x["method"])
        owner = people.resolve(data.get("owner"))
        add(x["task_id"], "owner", owner, event, "chat", confidence, x["method"])
        add(x["task_id"], "deadline", norm_deadline(data.get("deadline"), milestones), event, "chat",
            confidence, x["method"])
    with conn:
        conn.execute("DELETE FROM claims")
        conn.executemany("INSERT INTO claims (task_id, attribute, value, event_id, source, author, observed_at, "
                         "confidence, method) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", claims)
    return len(claims)


def minutes_since_refresh(conn: sqlite3.Connection) -> float | None:
    from datetime import datetime, timezone

    row = conn.execute("SELECT value FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone()
    if row is None:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 60


def refresh(conn: sqlite3.Connection, index=None, llm: LLM | None = None, scan_chat: bool = False) -> dict:
    """Full Week 6 pipeline: [scan chat] → registry → LLM chat links → registry → chat extraction → claims."""
    from teampulse import signals
    from teampulse.tasks import Registry, link_chat

    stats: dict = {}
    if index is not None:
        index.sync()
    if scan_chat:
        stats["scan"] = signals.scan(conn, signals.LLMClassifier(llm) if llm else signals.KeywordClassifier(), index)
    registry = Registry(conn, llm)
    stats["registry"] = registry.build()
    stats["chat_links"] = link_chat(conn, index, llm)
    stats["registry"] = registry.build()          # re-attach chat with the improved links
    people = People(conn)
    stats["extraction"] = extract_chat(conn, llm, people)
    stats["claims"] = build_claims(conn, people)
    with conn:
        conn.execute("INSERT OR REPLACE INTO sync_state VALUES ('tasks:refreshed_at', ?, ?)", (utc_now(), utc_now()))
    return stats


# ---- task cards (what the Tasks page and the Discrepancy Agent read) -------------------------------

def task_cards(conn: sqlite3.Connection) -> list[dict]:
    cards = []
    for t in conn.execute("SELECT * FROM tasks ORDER BY github_number IS NULL, github_number, title"):
        card = {"id": t["id"], "title": t["title"], "url": t["url"], "number": t["github_number"],
                "status": {}, "owner": {}, "deadline": {}, "workers": Counter(), "links": Counter(),
                "last_activity": None}
        for c in conn.execute("SELECT * FROM claims WHERE task_id = ? ORDER BY observed_at", (t["id"],)):
            if c["attribute"] == "worker":
                card["workers"][c["value"]] += 1
                continue
            # latest claim per source wins; several owners from the same event are kept together
            slot = card[c["attribute"]].get(c["source"])
            if slot and slot["event_id"] == c["event_id"] and c["attribute"] == "owner":
                slot["value"] = f"{slot['value']}, {c['value']}"
            elif not slot or (c["observed_at"] or "") >= (slot["observed_at"] or ""):
                card[c["attribute"]][c["source"]] = dict(c)
        for r in conn.execute("SELECT tl.role, e.updated_at FROM task_links tl JOIN events e ON e.id = tl.event_id "
                              "WHERE tl.task_id = ?", (t["id"],)):
            card["links"][r["role"]] += 1
            if r["updated_at"] and (card["last_activity"] or "") < r["updated_at"]:
                card["last_activity"] = r["updated_at"]
        cards.append(card)
    return cards


def days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    return (date.fromisoformat(iso[:10]) - date.today()).days
