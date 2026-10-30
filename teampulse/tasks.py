"""Task registry and entity linking.

One task = one piece of work, merged across sources:
  * every GitHub issue is a task;
  * a Notion task page joins an issue by "#12" in its title/text, else by a near-identical title, else by
    an LLM yes/no check on close candidates; otherwise it becomes a Notion-only task;
  * PRs, commits, comments and reviews attach through "#12" references and parent links;
  * chat messages attach through the Week 5 scanner, upgraded here by an LLM that picks the right task
    from a shortlist, and by human corrections, which always win.

    python -m teampulse.tasks            # rebuild registry + chat links + claims (see claims.py)
"""
from __future__ import annotations

import difflib
import logging
import re
import sqlite3

from teampulse.llm import LLM, extract_json
from teampulse.models import utc_now

log = logging.getLogger("teampulse.tasks")

TITLE_AUTO = 0.82      # titles this similar are the same task without asking
TITLE_ASK = 0.45       # between ASK and AUTO, ask the LLM
LINK_BATCH = 12
LETTERS = "ABCDEFGH"


def _reply_number(value) -> int | None:
    """1, "1", "Message 1", "m1" → 1. Models don't always echo the exact format asked for."""
    m = re.search(r"\d+", str(value)) if value is not None else None
    return int(m.group(0)) if m else None


def _letter_index(value) -> int:
    """"A", "a", "A)", "(B)", "Option C" → 0, 0, 0, 1, 2. Anything else (null, "none") → -1."""
    if not isinstance(value, str):
        return -1
    m = re.fullmatch(r"\s*(?:option\s+|task\s+)?\(?([A-H])\)?[.):]?\s*", value, re.I)
    return LETTERS.find(m.group(1).upper()) if m else -1


def norm_title(text: str) -> str:
    text = re.sub(r"\(?#\d+\)?", " ", text or "").lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def title_similarity(a: str, b: str) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    wa, wb = set(a.split()), set(b.split())
    jaccard = len(wa & wb) / len(wa | wb)
    return round(max(ratio, jaccard), 3)


def _refs(row: sqlite3.Row) -> list[int]:
    import json
    return json.loads(row["extra"] or "{}").get("issue_refs", [])


def _number(row: sqlite3.Row) -> int | None:
    import json
    return json.loads(row["extra"] or "{}").get("number")


MATCH_PROMPT = """Decide whether each Notion task is the same piece of work as one of its candidate GitHub
issues. Titles may be worded differently or mix Hindi and English. Answer null if none is clearly the same.
Return ONLY JSON: {"matches": [{"item": 1, "match": "A" or null}, ...]}"""

LINK_PROMPT = """Each chat message below comes from a student team's Discord (often Hinglish). Pick the task
it is about from its candidate list, or null if it isn't about any listed task (e.g. general chat, a
decision about tools, a different task). "login wala page" = the login page task, etc.
Return ONLY JSON: {"links": [{"message": 1, "task": "A" or null}, ...]}"""


class Registry:
    def __init__(self, conn: sqlite3.Connection, llm: LLM | None = None):
        self.conn = conn
        self.llm = llm
        self.llm_calls = 0

    # ---- build ---------------------------------------------------------------------------------
    def build(self) -> dict:
        conn = self.conn
        tasks: dict[str, dict] = {}
        links: dict[str, tuple] = {}            # event_id -> (task_id, role, method, score)
        by_number: dict[tuple[str, int], str] = {}

        issues = conn.execute("SELECT * FROM events WHERE source='github' AND kind='issue'").fetchall()
        for row in issues:
            number = _number(row)
            task_id = f"task:{row['channel']}#{number}"
            tasks[task_id] = {"id": task_id, "title": row["title"], "primary_event": row["id"],
                              "github_number": number, "notion_page": None, "url": row["url"]}
            links[row["id"]] = (task_id, "issue", "primary", 1.0)
            by_number[(row["channel"], number)] = task_id
        repos = sorted({r["channel"] for r in issues})

        def task_for_number(n: int, repo: str | None = None) -> str | None:
            for r in ([repo] if repo else []) + repos:
                if (r, n) in by_number:
                    return by_number[(r, n)]
            return None

        # Notion task pages (pages with a status; plan pages without one aren't tasks)
        pages = conn.execute("SELECT * FROM events WHERE source='notion' AND status IS NOT NULL "
                             "AND status != 'archived'").fetchall()
        ask = []
        taken: set[str] = set()
        for page in pages:
            match = next((task_for_number(n) for n in _refs(page) if task_for_number(n)), None)
            if match and match not in taken:
                self._attach_page(tasks, links, page, match, "ref", 1.0)
                taken.add(match)
                continue
            scored = sorted(((title_similarity(page["title"], tasks[t]["title"]), t) for t in tasks
                             if t not in taken and tasks[t]["github_number"] is not None), reverse=True)
            if scored and scored[0][0] >= TITLE_AUTO:
                self._attach_page(tasks, links, page, scored[0][1], "title", scored[0][0])
                taken.add(scored[0][1])
            elif scored and scored[0][0] >= TITLE_ASK:
                ask.append((page, [t for s, t in scored[:4] if s >= TITLE_ASK]))
            else:
                ask.append((page, []))
        for page, choice in self._confirm_pages(tasks, ask, taken):
            if choice:
                self._attach_page(tasks, links, page, choice, "llm", None)
                taken.add(choice)
            else:
                task_id = f"task:notion:{page['native_id']}"
                tasks[task_id] = {"id": task_id, "title": page["title"], "primary_event": page["id"],
                                  "github_number": None, "notion_page": page["id"], "url": page["url"]}
                links[page["id"]] = (task_id, "notion_task", "primary", 1.0)

        # PRs: "fixes #14" style references, else a near-identical title
        for pr in conn.execute("SELECT * FROM events WHERE source='github' AND kind='pr'"):
            target = next((task_for_number(n, pr["channel"]) for n in _refs(pr)
                           if task_for_number(n, pr["channel"])), None)
            method, score = "ref", 1.0
            if target is None:
                best = max(((title_similarity(pr["title"], t["title"]), tid) for tid, t in tasks.items()),
                           default=(0, None))
                if best[0] >= TITLE_AUTO:
                    target, method, score = best[1], "title", best[0]
            if target:
                links[pr["id"]] = (target, "pr", method, score)
                by_number.setdefault((pr["channel"], _number(pr)), target)   # "#15" now means the PR's task

        # Commits by reference; comments and reviews through their parent issue/PR
        for commit in conn.execute("SELECT * FROM events WHERE source='github' AND kind='commit'"):
            target = next((task_for_number(n, commit["channel"]) for n in _refs(commit)
                           if task_for_number(n, commit["channel"])), None)
            if target:
                links[commit["id"]] = (target, "commit", "ref", 1.0)
        for child in conn.execute("SELECT * FROM events WHERE source='github' AND kind IN ('issue_comment','review')"):
            parent = links.get(child["parent_id"])
            if parent:
                links[child["id"]] = (parent[0], "comment" if child["kind"] == "issue_comment" else "review",
                                      "parent", 1.0)

        # Chat: whatever the scanner / LLM linker / humans decided (see link_chat)
        primary_to_task = {}
        for event_id, (task_id, role, *_rest) in links.items():
            if role in ("issue", "notion_task", "pr"):
                primary_to_task[event_id] = task_id
        for s in conn.execute("SELECT event_id, task_id, link_method, link_score FROM signals "
                              "WHERE task_id IS NOT NULL AND type != 'noise'"):
            target = primary_to_task.get(s["task_id"])
            if target:
                links[s["event_id"]] = (target, "chat", s["link_method"], s["link_score"])

        with conn:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM task_links")
            conn.executemany("INSERT INTO tasks VALUES (:id, :title, :primary_event, :github_number, :notion_page, :url)",
                             list(tasks.values()))
            conn.executemany("INSERT INTO task_links VALUES (?, ?, ?, ?, ?)",
                             [(e, *v) for e, v in links.items()])
        return {"tasks": len(tasks), "links": len(links), "notion_merged": len(taken),
                "llm_calls": self.llm_calls}

    @staticmethod
    def _attach_page(tasks, links, page, task_id, method, score):
        tasks[task_id]["notion_page"] = page["id"]
        links[page["id"]] = (task_id, "notion_task", method, score)

    def _confirm_pages(self, tasks, ask, taken):
        """Ask the LLM once for all Notion pages with plausible-but-uncertain issue matches."""
        decided = [(page, None) for page, cands in ask if not cands]
        pending = [(page, cands) for page, cands in ask if cands]
        if not pending:
            return decided
        if self.llm is None:
            return decided + [(page, None) for page, _ in pending]
        lines = []
        for i, (page, cands) in enumerate(pending, 1):
            lines.append(f"Item {i}. Notion task: {page['title']}\n" + "\n".join(
                f"  {LETTERS[j]}) GitHub issue: {tasks[c]['title']}" for j, c in enumerate(cands)))
        try:
            reply = self.llm.chat([{"role": "system", "content": MATCH_PROMPT},
                                   {"role": "user", "content": "\n\n".join(lines)}], json_mode=True)
            self.llm_calls += 1
            answers = {_reply_number(m.get("item")): m.get("match") for m in extract_json(reply.text).get("matches", [])
                       if isinstance(m, dict) and _reply_number(m.get("item")) is not None}
        except Exception as exc:
            log.warning("Notion↔GitHub match check failed (%s); keeping them separate", exc)
            answers = {}
        for i, (page, cands) in enumerate(pending, 1):
            idx = _letter_index(answers.get(i))
            choice = cands[idx] if 0 <= idx < len(cands) and cands[idx] not in taken else None
            decided.append((page, choice))
        return decided


# ---- chat → task linking (upgrade of the Week 5 scanner's links) -----------------------------------

def _task_candidates(conn, index, text: str, k: int = 5) -> list[str]:
    """Shortlist task ids for a message: semantic search over issues + Notion tasks, mapped to tasks."""
    if index is None:
        return []
    out = []
    for hit in index.search(text, k=k * 2, sources=["github", "notion"], kinds=["issue", "notion_page"]):
        row = conn.execute("SELECT task_id FROM task_links WHERE event_id = ?", (hit["event_id"],)).fetchone()
        if row and row["task_id"] not in out:
            out.append(row["task_id"])
        if len(out) >= k:
            break
    return out


def apply_feedback(conn: sqlite3.Connection) -> int:
    """Human corrections always win: write them into the scanner's links."""
    rows = conn.execute(
        "SELECT f.event_id, f.task_id, t.primary_event FROM link_feedback f LEFT JOIN tasks t ON t.id = f.task_id "
        "JOIN signals s ON s.event_id = f.event_id").fetchall()
    with conn:
        for r in rows:
            conn.execute("UPDATE signals SET task_id = ?, link_method = 'human', link_score = 1.0, link_checked = 1 "
                         "WHERE event_id = ?", (r["primary_event"] if r["task_id"] else None, r["event_id"]))
    return len(rows)


def llm_choose(conn: sqlite3.Connection, index, llm: LLM, rows: list) -> tuple[dict, int]:
    """Ask the LLM which task each message is about, from a semantic shortlist. Writes nothing.

    rows need: event_id, body, task_hint, author_name, channel, created_at.
    Returns ({event_id: task_id or None}, llm_calls). Messages with no candidates map to None."""
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM tasks")}
    choices: dict[str, str | None] = {}
    work = []
    for r in rows:
        cands = _task_candidates(conn, index, " ".join(x for x in (r["task_hint"], r["body"]) if x))
        if cands:
            work.append((r, cands[:5]))
        else:
            choices[r["event_id"]] = None
    calls = 0
    for start in range(0, len(work), LINK_BATCH):
        batch = work[start:start + LINK_BATCH]
        blocks = []
        for i, (r, cands) in enumerate(batch, 1):
            prev = conn.execute("SELECT author_name, body FROM events WHERE source='discord' AND channel = ? "
                                "AND created_at < ? ORDER BY created_at DESC LIMIT 2",
                                (r["channel"], r["created_at"])).fetchall()[::-1]
            context = " | ".join(f"{p['author_name']}: {p['body'][:80]}" for p in prev)
            blocks.append(f"Message {i} [{r['author_name']}, #{r['channel']}]"
                          + (f" (earlier: {context})" if context else "") + f"\n{r['body']}\n"
                          + "\n".join(f"  {LETTERS[j]}) {titles[c]}" for j, c in enumerate(cands)))
        try:
            reply = llm.chat([{"role": "system", "content": LINK_PROMPT},
                              {"role": "user", "content": "\n\n".join(blocks)}], json_mode=True)
            calls += 1
            answers = {_reply_number(a.get("message")): a.get("task") for a in extract_json(reply.text).get("links", [])
                       if isinstance(a, dict) and _reply_number(a.get("message")) is not None}
            if not answers:   # say what came back, so a format problem is visible instead of silent
                log.warning("Chat link check: couldn't read the AI's reply: %s", reply.text[:300])
        except Exception as exc:
            log.warning("Chat link check failed (%s); keeping scanner links", exc)
            break
        for i, (r, cands) in enumerate(batch, 1):
            if i in answers:
                idx = _letter_index(answers[i])
                choices[r["event_id"]] = cands[idx] if 0 <= idx < len(cands) else None
    return choices, calls


def link_chat(conn: sqlite3.Connection, index, llm: LLM | None) -> dict:
    """Let the LLM choose (or reject) the task for chat messages that have no exact #ref link yet."""
    applied = apply_feedback(conn)
    if llm is None or index is None:
        return {"checked": 0, "llm_calls": 0, "feedback": applied}
    rows = conn.execute(
        "SELECT s.event_id, s.task_hint, e.body, e.author_name, e.created_at, e.channel "
        "FROM signals s JOIN events e ON e.id = s.event_id "
        "WHERE s.type != 'noise' AND s.link_checked = 0 AND COALESCE(s.link_method, '') NOT IN ('ref', 'human') "
        "ORDER BY e.created_at").fetchall()
    choices, calls = llm_choose(conn, index, llm, rows)
    primary = {r["id"]: r["primary_event"] for r in conn.execute("SELECT id, primary_event FROM tasks")}
    with conn:
        for event_id, task_id in choices.items():
            conn.execute("UPDATE signals SET task_id = ?, link_method = ?, link_score = NULL, link_checked = 1 "
                         "WHERE event_id = ?", (primary.get(task_id), "llm" if task_id else None, event_id))
    return {"checked": len(choices), "llm_calls": calls, "feedback": applied}


def save_feedback(conn: sqlite3.Connection, event_id: str, task_id: str | None, labeller: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO link_feedback VALUES (?, ?, ?, ?)",
                     (event_id, task_id, labeller, utc_now()))
    apply_feedback(conn)


def main():
    import logging as _logging

    from teampulse import claims, db
    from teampulse.index import EventIndex

    _logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = db.connect()
    index = EventIndex(conn)
    llm = LLM(conn)
    stats = claims.refresh(conn, index, llm)
    print(stats)
    for card in claims.task_cards(conn):
        print(f"- {card['title']}")
        for attr in ("status", "owner", "deadline"):
            vals = {src: v["value"] for src, v in card[attr].items()}
            if vals:
                print(f"    {attr:8} {vals}")


if __name__ == "__main__":
    main()
