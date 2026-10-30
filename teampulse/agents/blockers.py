"""Blocker Agent: answers "what is blocking us / who needs help / what did we decide" from classified chat.

It reads the `signals` table (filled by teampulse.signals), adds the current state of each linked task
from GitHub/Notion, works out whether each blocker still looks open, and answers with citations.
Registered with the Supervisor as the "blockers" specialist.

    python -m teampulse.agents.blockers "what is blocking us?"
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

from teampulse import signals as sig
from teampulse.agents.retrieval import _evidence_item, format_evidence, verify_citations
from teampulse.llm import LLM

DEFAULT_DAYS = 21
MAX_SIGNALS = 15
DONE_STATES = {"closed", "merged", "done", "complete", "completed", "closed_not_planned"}

TYPE_WORDS = {
    "blocker": r"block|stuck|atak|problem|issue|broken|not working|nahi chal|risk|blocking|kya scene|status",
    "help_request": r"help|review|waiting|needs?|pending|request|madad|unanswered",
    "decision": r"decid|decision|final|agreed|chose|choose|tay",
    "progress": r"progress|update|done|finish|kya hua|what happened|working on|kaun kar",
}

ANSWER_PROMPT = """You report on blockers, help requests and decisions found in a student team's chat.
Each numbered item is either a chat message (with its detected type and open/resolved status) or the
current state of the task it is about (GitHub issue / Notion task).
Rules:
- Group by task. For each: what the problem or request is, who raised it, when, and whether it still
  looks open. If the chat says something is blocked but the linked task is closed/done (or the reverse),
  point that out.
- Cite every statement like [2]. Never cite a number that isn't listed.
- Put still-open items first. Be concise: bullets. Answer in the language of the question."""


def wanted_types(question: str) -> list[str]:
    q = question.lower()
    found = [t for t, pattern in TYPE_WORDS.items() if re.search(pattern, q)]
    if "blocker" in found and "help_request" not in found:
        found.append("help_request")   # an unanswered request is often what's blocking someone
    return found or ["blocker", "help_request"]


def window_days(question: str) -> int:
    q = question.lower()
    if re.search(r"\btoday\b|\baaj\b", q):
        return 1
    if re.search(r"\byesterday\b|\bkal\b", q):
        return 2
    if re.search(r"this week|is hafte|past week|last week|\bweek\b", q):
        return 7
    match = re.search(r"(\d+)\s*(days?|din)", q)
    if match:
        return int(match.group(1))
    if re.search(r"month|mahine", q):
        return 30
    return DEFAULT_DAYS


class BlockerAgent:
    name = "blockers"
    description = ("Blockers, problems, things stuck or broken, help/review requests waiting on someone, and "
                   "decisions made in the team chat (Discord, often Hinglish). Use for 'what is blocking us', "
                   "'who needs help', 'what did we decide', 'kya atka hua hai'.")

    def __init__(self, conn: sqlite3.Connection, llm: LLM | None = None, index=None, classifier=None):
        self.conn = conn
        self.llm = llm or LLM(conn)
        self.index = index
        self.classifier = classifier

    def _task_state(self, task_id: str | None, after: str) -> tuple[sqlite3.Row | None, bool]:
        if not task_id:
            return None, False
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None, False
        done = (row["status"] or "").lower() in DONE_STATES
        return row, done and (row["updated_at"] or "") >= after

    def collect(self, question: str) -> dict:
        return self.collect_for(wanted_types(question), window_days(question))

    def collect_for(self, types: list[str], days: int, limit: int = MAX_SIGNALS) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            f"SELECT s.*, e.created_at AS at FROM signals s JOIN events e ON e.id = s.event_id "
            f"WHERE s.type IN ({','.join('?' * len(types))}) AND e.created_at >= ? "
            f"ORDER BY s.resolved, e.created_at DESC LIMIT ?", [*types, since, limit]).fetchall()

        evidence, items, position = [], [], {}

        def add(row: sqlite3.Row, why: str) -> int:
            if row["id"] not in position:
                evidence.append(_evidence_item(self.conn, row, why, None))
                position[row["id"]] = len(evidence)
            return position[row["id"]]

        for s in rows:
            message = self.conn.execute("SELECT * FROM events WHERE id = ?", (s["event_id"],)).fetchone()
            task, task_done_after = self._task_state(s["task_id"], s["at"])
            if s["resolved"]:
                state = "resolved in chat"
            elif s["type"] == "blocker" and task_done_after:
                state = "possibly resolved (linked task closed/done afterwards)"
            elif s["type"] in ("blocker", "help_request"):
                state = "open"
            else:
                state = ""
            n = add(message, f"chat {s['type']}")
            evidence[n - 1]["status"] = s["type"] + (f", {state}" if state else "")
            # Direct replies often say who is helping or whether it got fixed ("main ma'am se baat kar rahi hu").
            replies = [add(r, f"reply to [{n}]") for r in self.conn.execute(
                "SELECT * FROM events WHERE parent_id = ? ORDER BY created_at LIMIT 3", (message["id"],))]
            item = {"n": n, "type": s["type"], "state": state, "summary": s["summary"], "task": None,
                    "author": message["author_name"], "date": evidence[n - 1]["date"],
                    "classifier": s["classifier"], "link_method": s["link_method"], "link_score": s["link_score"],
                    "replies": replies}
            if task is not None:
                item["task"] = add(task, f"linked task ({s['link_method']})")
                evidence[item["task"] - 1]["why"] = f"task linked to [{n}] via {s['link_method']}"
            items.append(item)
        return {"types": types, "days": days, "evidence": evidence, "items": items}

    def template_answer(self, data: dict) -> str:
        """Answer without an LLM, so the feature still works offline or when free quotas run out."""
        if not data["items"]:
            return ""
        labels = {"blocker": "Blocker", "help_request": "Waiting for help", "decision": "Decision",
                  "progress": "Update"}
        lines = []
        for it in data["items"]:
            text = it["summary"] or self.conn.execute(
                "SELECT body FROM events WHERE id = ?", (data["evidence"][it["n"] - 1]["event_id"],)).fetchone()[0]
            line = f"- **{labels[it['type']]}** ({it['author']}, {it['date'][:10]}): {text} [{it['n']}]"
            if it["task"]:
                task = data["evidence"][it["task"] - 1]
                line += f" — task: {task['text'].splitlines()[0][:60]} is *{task['status']}* [{it['task']}]"
            if it["state"]:
                line += f" — {it['state']}"
            lines.append(line)
        return "\n".join(lines)

    def ask(self, question: str) -> dict:
        trace = []
        started = time.time()
        stats = sig.scan(self.conn, self.classifier or sig.LLMClassifier(self.llm), self.index)
        trace.append({"step": "scan_chat", "ms": int((time.time() - started) * 1000), **stats})

        started = time.time()
        data = self.collect(question)
        trace.append({"step": "collect", "ms": int((time.time() - started) * 1000), "types": data["types"],
                      "days": data["days"], "signals": len(data["items"]),
                      "open": sum(1 for i in data["items"] if i["state"] == "open")})

        started = time.time()
        if not data["items"]:
            names = " / ".join(t.replace("_", " ") + "s" for t in data["types"])
            return {"answer": f"No {names} found in the team chat in the last {data['days']} days.",
                    "evidence": [], "cited": [], "warnings": [], "provider": "none",
                    "trace": trace + [{"step": "answer", "ms": 0, "provider": "none"}]}
        facts = "\n".join(
            f"- [{it['n']}] {it['type']}" + (f" ({it['state']})" if it["state"] else "")
            + (f", task [{it['task']}]" if it["task"] else ", no linked task")
            + (f", replies {' '.join(f'[{r}]' for r in it['replies'])}" if it.get("replies") else "")
            for it in data["items"])
        messages = [
            {"role": "system", "content": ANSWER_PROMPT + f"\nToday is {datetime.now(timezone.utc):%A %Y-%m-%d}."},
            {"role": "user", "content": f"Evidence:\n\n{format_evidence(data['evidence'])}\n\n"
                                        f"Detected items:\n{facts}\n\nQuestion: {question}"},
        ]
        warnings = []
        try:
            reply = self.llm.chat(messages)
            text, provider = reply.text.strip(), reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:
            text, provider = self.template_answer(data), "template"
            warnings.append(f"Language model unavailable ({type(exc).__name__}); showing the detected items directly.")
        text, cited, invalid, extra = verify_citations(text, len(data["evidence"]), provider)
        trace.append({"step": "answer", "ms": int((time.time() - started) * 1000), "provider": provider,
                      "cited": cited, "invalid": invalid})
        fallback = self.template_answer(data)
        return {"answer": text, "evidence": data["evidence"], "cited": cited, "warnings": warnings + extra,
                "provider": provider, "trace": trace,
                # for the guardrails: what a blocked answer is replaced with, and the data the AI was given
                "fallback_answer": fallback, "grounding": facts + "\n" + fallback}


def main():
    from teampulse import db
    from teampulse.index import EventIndex

    question = " ".join(sys.argv[1:]) or "What is blocking us right now?"
    conn = db.connect()
    index = EventIndex(conn)
    index.sync()
    result = BlockerAgent(conn, index=index).ask(question)
    print(result["answer"], "\n")
    for n in result["cited"]:
        e = result["evidence"][n - 1]
        print(f"[{n}] {e['source']}/{e['kind']} {e['date']} {e['url']}")
    for w in result["warnings"]:
        print("warning:", w)


if __name__ == "__main__":
    main()
