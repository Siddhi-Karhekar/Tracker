"""Contribution Agent: "what did Aman do this week?", "contribution report", "did Kabir really finish 80%?"

Builds evidence-based reports (teampulse.contributions) for the members the question names (or everyone),
respecting opt-in and self-review: a member's details are only shared once they have approved their own
report. The AI writes a neutral summary citing the evidence; without AI the evidence list is returned.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time

from teampulse import contributions as contrib
from teampulse.agents.blockers import window_days
from teampulse.agents.retrieval import _evidence_item, format_evidence, verify_citations
from teampulse.llm import LLM

MAX_EVIDENCE = 40

ANSWER_PROMPT = """You summarise what members of a student project team did, using ONLY the numbered evidence and the
listed claim checks.
Rules:
- Neutral and factual. Do NOT rank or compare members, score them, or judge effort, skill or character.
- Mention non-code work too: reviews, planning, docs, decisions, helping teammates.
- For chat claims, repeat the check's wording exactly. "no linked evidence found" means the data doesn't
  show it, NOT that the claim is false (work can happen offline). Never call a claim false or a lie.
- If a member added their own note, include it.
- Cite every statement like [3]. Never cite a number that isn't listed. Answer in the language of the question."""


def mentioned(question: str, members: list[contrib.Member]) -> list[contrib.Member]:
    q = question.lower()
    hits = [m for m in members if re.search(rf"\b{re.escape(m.name.lower())}\b", q)
            or (m.github and m.github.lower() in q)]
    return hits


class ContributionAgent:
    name = "contributions"
    description = ("What a team member (or everyone) actually did, from evidence: PRs, commits, reviews, Notion "
                   "edits, chat updates, helping others; and whether chat claims like '80% done' are backed by "
                   "evidence. Use for 'what did X do this week', 'contribution report', 'did X really finish Y'.")

    def __init__(self, conn: sqlite3.Connection, llm: LLM | None = None):
        self.conn = conn
        self.llm = llm or LLM(conn)

    def ask(self, question: str) -> dict:
        started = time.time()
        everyone = contrib.members(self.conn)
        chosen = mentioned(question, everyone) or everyone
        days = window_days(question)
        days = 14 if days == 21 else days       # 21 is the "nothing said" default
        notes, reports = [], []
        for m in chosen:
            if not m.consent:
                notes.append(f"{m.name} hasn't opted in, so there's no report for them.")
            elif not contrib.visible_to_team(self.conn, m):
                notes.append(f"{m.name} hasn't reviewed and approved their report yet (Contributions page).")
            else:
                reports.append(contrib.build_report(self.conn, m, days))
        trace = [{"step": "build_reports", "ms": int((time.time() - started) * 1000), "days": days,
                  "members": [r.member.name for r in reports], "withheld": len(notes)}]
        if not reports:
            text = "\n".join(f"- {n}" for n in notes) or "No team members found. Link accounts with `python -m teampulse.cli link`."
            return {"answer": text, "evidence": [], "cited": [], "warnings": [], "provider": "none", "trace": trace}

        evidence, position = [], {}

        def cite(event_id: str) -> int | None:
            if event_id not in position:
                if len(evidence) >= MAX_EVIDENCE:
                    return None
                row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
                if row is None:
                    return None
                evidence.append(_evidence_item(self.conn, row, "contribution evidence", None))
                position[event_id] = len(evidence)
            return position[event_id]

        facts = []
        for r in reports:   # claims first so they always fit, then each category
            lines = [f"## {r.member.name} (last {r.days} days)"]
            if r.review and r.review.get("comment"):
                lines.append(f"Member's own note: {r.review['comment']}")
            for c in r.claims:
                n = cite(c["event_id"])
                ev = ", ".join(f"{e['label']} {e['date']} [{cite(e['event_id'])}]" for e in c["evidence"][:3])
                lines.append(f"- Claim [{n}] “{c['text'][:90]}” → {c['verdict']}" + (f"; evidence: {ev}" if ev else ""))
            for cat, items in r.items.items():
                refs = [f"{it['label'][:70]} [{cite(it['event_id'])}]" for it in items[:8] if cite(it["event_id"])]
                if refs:
                    lines.append(f"- {cat} ({len(items)}): " + "; ".join(refs))
            if r.tasks:
                lines.append("- Tasks: " + "; ".join(
                    f"{t['title']} ({'assigned' if t['assigned'] else ''}{' + ' if t['assigned'] and t['worked'] else ''}"
                    f"{'did work' if t['worked'] else ''})" for t in r.tasks))
            facts.append("\n".join(lines))
        trace.append({"step": "collect", "ms": 0, "evidence": len(evidence)})

        started = time.time()
        warnings = [n for n in notes]
        try:
            reply = self.llm.chat([
                {"role": "system", "content": ANSWER_PROMPT},
                {"role": "user", "content": f"Evidence:\n\n{format_evidence(evidence)}\n\nReports:\n\n" + "\n\n".join(facts)
                                            + f"\n\nQuestion: {question}"}])
            text, provider = reply.text.strip(), reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:
            text, provider = "\n\n".join(f.replace("## ", "**", 1).replace(")\n", ")**\n", 1) for f in facts), "template"
            warnings.append(f"Language model unavailable ({type(exc).__name__}); showing the evidence directly.")
        text, cited, invalid, extra = verify_citations(text, len(evidence), provider)
        trace.append({"step": "answer", "ms": int((time.time() - started) * 1000), "provider": provider,
                      "cited": cited, "invalid": invalid})
        return {"answer": text, "evidence": evidence, "cited": cited, "warnings": warnings + extra,
                "provider": provider, "trace": trace}


def main():
    from teampulse import db

    question = " ".join(sys.argv[1:]) or "What did everyone contribute in the last two weeks?"
    result = ContributionAgent(db.connect()).ask(question)
    print(result["answer"])
    for w in result["warnings"]:
        print("note:", w)


if __name__ == "__main__":
    main()
