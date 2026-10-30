"""Discrepancy Agent: "where do our sources disagree?", "is Notion up to date?", "who's really doing payments?"

Refreshes claims, runs the detection rules (teampulse.discrepancies), narrows to the tasks and conflict
types the question is about, and has the LLM explain each conflict citing both sides. Without an LLM it
returns the detected list directly. Registered with the Supervisor as the "discrepancies" specialist.

    python -m teampulse.agents.discrepancy "is our Notion up to date?"
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time

from teampulse import discrepancies as disc
from teampulse.agents.retrieval import _evidence_item, format_evidence, verify_citations
from teampulse.llm import LLM

STALE_MINUTES = 10

TYPE_WORDS = {
    "owner": r"\bowner|\bwho\b.*\b(doing|working|handling|does|owns)|assign|kaun|responsible",
    "deadline": r"deadline|due|date|\bkab\b|when|milestone|late|overdue",
    "status": r"status|done|finished|complete|really|actually|open|broken|working",
    "stale_plan": r"notion|up to date|updated|stale|outdated|tracker|board",
    "plan_outdated": r"\bplan\b|decision|decid|outdated|notion|up to date",
}

ANSWER_PROMPT = """You explain where a student team's GitHub, Notion and chat disagree.
You get numbered evidence and a list of detected conflicts (with severity and a suggested fix).
Rules:
- Most severe first. For each conflict: one line saying what disagrees, citing BOTH sides like [1] vs [3],
  then the suggested fix in a few words.
- Only use the evidence. Cite every statement. Never cite a number that isn't listed.
- If nothing is detected, say the sources agree on what was checked.
- Answer in the language of the question. Be concise: bullets."""


def wanted(question: str) -> set[str] | None:
    q = question.lower()
    found = {t for t, pattern in TYPE_WORDS.items() if re.search(pattern, q)}
    if "stale_plan" in found:
        found.add("status")      # "is Notion up to date" covers both directions
    return found or None


def task_filter(conn: sqlite3.Connection, question: str) -> set[str] | None:
    """Tasks the question names, by #number or by distinctive title words. None = all tasks."""
    q = question.lower()
    numbers = {int(n) for n in re.findall(r"#(\d+)", q)}
    hits = set()
    for t in conn.execute("SELECT id, title, github_number FROM tasks"):
        words = [w for w in re.findall(r"[a-z]{4,}", (t["title"] or "").lower())
                 if w not in {"with", "page", "screen", "task", "integration", "model", "test", "mode"}]
        if t["github_number"] in numbers or any(re.search(rf"\b{w}", q) for w in words):
            hits.add(t["id"])
    return hits or None


class DiscrepancyAgent:
    name = "discrepancies"
    description = ("Where GitHub, Notion and team chat DISAGREE: tasks marked done that are still open or "
                   "broken, people assigned who aren't doing the work, conflicting deadlines, Notion/plan pages "
                   "that are out of date. Use for 'where do our sources disagree', 'is Notion up to date', "
                   "'who is really doing X', 'kya sab sync me hai'.")

    def __init__(self, conn: sqlite3.Connection, llm: LLM | None = None, index=None, refresh: bool = True):
        self.conn = conn
        self.llm = llm or LLM(conn)
        self.index = index
        self.refresh = refresh

    def ask(self, question: str) -> dict:
        trace = []
        started = time.time()
        from teampulse.claims import minutes_since_refresh

        age = minutes_since_refresh(self.conn)
        stale = age is None or age > STALE_MINUTES     # the poller normally keeps this fresh
        stats = disc.run(self.conn, self.index, self.llm, refresh=self.refresh and stale)
        trace.append({"step": "detect", "ms": int((time.time() - started) * 1000),
                      **{k: v for k, v in stats.items() if k != "refresh"}})

        started = time.time()
        items = disc.open_items(self.conn)
        types, tasks = wanted(question), task_filter(self.conn, question)
        if tasks:
            items = [d for d in items if d["task_id"] in tasks]
        if types:
            typed = [d for d in items if d["type"] in types]
            # A named task with no conflict of the guessed type: show its other conflicts rather than nothing.
            items = typed if (typed or not tasks) else items
        evidence, position = [], {}
        for d in items:
            d["refs"] = []
            for side in d["sides"]:
                if side["event_id"] not in position:
                    row = self.conn.execute("SELECT * FROM events WHERE id = ?", (side["event_id"],)).fetchone()
                    if row is None:
                        continue
                    evidence.append(_evidence_item(self.conn, row, f"{d['type']} conflict", None))
                    position[side["event_id"]] = len(evidence)
                d["refs"].append((side["label"], side["value"], position[side["event_id"]]))
        trace.append({"step": "select", "ms": int((time.time() - started) * 1000), "types": sorted(types or []),
                      "tasks": sorted(tasks or []), "conflicts": len(items)})

        started = time.time()
        if not items:
            scope = " for that" if (types or tasks) else ""
            return {"answer": f"No disagreements found between GitHub, Notion and chat{scope}.", "evidence": [],
                    "cited": [], "warnings": [], "provider": "none",
                    "trace": trace + [{"step": "answer", "ms": 0, "provider": "none"}]}
        facts = "\n".join(
            f"- ({d['severity']}) {d['title']} | sides: " + "; ".join(f"{label}: {value} [{n}]" for label, value, n in d["refs"])
            + f" | fix: {d['suggestion']}" + ("" if d["verified"] else " | unverified") for d in items)
        warnings = []
        try:
            reply = self.llm.chat([
                {"role": "system", "content": ANSWER_PROMPT},
                {"role": "user", "content": f"Evidence:\n\n{format_evidence(evidence)}\n\nDetected conflicts:\n{facts}"
                                            f"\n\nQuestion: {question}"}])
            text, provider = reply.text.strip(), reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:
            text, provider = self.template(items), "template"
            warnings.append(f"Language model unavailable ({type(exc).__name__}); showing the detected conflicts directly.")
        text, cited, invalid, extra = verify_citations(text, len(evidence), provider)
        if provider != "template":   # the AI may summarise, but it may not drop a detected conflict
            missing = [d for d in items if d["refs"] and not {n for _, _, n in d["refs"]} & set(cited)]
            if missing:
                text = text.rstrip() + "\n\nAlso found:\n" + self.template(missing)
                cited = sorted(set(cited) | {n for d in missing for _, _, n in d["refs"]})
                trace.append({"step": "added_missing_conflicts", "ms": 0, "count": len(missing)})
        trace.append({"step": "answer", "ms": int((time.time() - started) * 1000), "provider": provider,
                      "cited": cited, "invalid": invalid})
        return {"answer": text, "evidence": evidence, "cited": cited, "warnings": warnings + extra,
                "provider": provider, "trace": trace,
                "fallback_answer": self.template(items), "grounding": facts}   # used by the guardrails

    @staticmethod
    def template(items: list[dict]) -> str:
        icon = {"high": "🔴", "medium": "🟠", "low": "🟡"}
        lines = []
        for d in items:
            sides = " vs ".join(f"{label} *{value}* [{n}]" for label, value, n in d["refs"])
            lines.append(f"- {icon[d['severity']]} **{d['title']}** — {sides}. {d['suggestion']}")
        return "\n".join(lines)


def main():
    from teampulse import db
    from teampulse.index import EventIndex

    question = " ".join(sys.argv[1:]) or "Where do GitHub, Notion and chat disagree?"
    conn = db.connect()
    result = DiscrepancyAgent(conn, index=EventIndex(conn)).ask(question)
    print(result["answer"], "\n")
    for n in result["cited"]:
        e = result["evidence"][n - 1]
        print(f"[{n}] {e['source']}/{e['kind']} {e['date']} {e['url']}")
    for w in result["warnings"]:
        print("warning:", w)


if __name__ == "__main__":
    main()
