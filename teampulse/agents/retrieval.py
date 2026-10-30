"""Retrieval Agent (LangGraph): plan → retrieve → answer with citations → check citations.

    python -m teampulse.agents.retrieval "What did we finish this week?"

Every step is recorded in `trace`, which the Streamlit app shows and Week 4's Supervisor reuses.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import END, StateGraph

from teampulse.index import EventIndex
from teampulse.llm import LLM, extract_json

SOURCES = ["github", "notion", "discord"]
MAX_EVIDENCE = 24
SNIPPET_CHARS = 700

PLAN_PROMPT = """You plan searches over a student team's project data. Today is {today}.
Data: GitHub (issues, prs, commits, issue comments, reviews), Notion (task pages with status/owner/due,
plan pages), Discord (team chat, often Hinglish: Hindi + English in Latin script).

Return ONLY JSON:
{{"search_queries": [1-4 short queries; include a Hinglish phrasing when chat is likely relevant],
  "sources": [subset of "github","notion","discord"; empty list = all],
  "since_days": number of days to look back if the question is about a time window, else null,
  "wants_recent_activity": true if the question asks what happened/changed/was done in a period}}"""

ANSWER_PROMPT = """You answer questions about a student team's project using ONLY the numbered evidence.
Rules:
- After every factual statement, cite evidence like [3] or [2][5]. Never cite a number not listed.
- If sources disagree, say so and cite both sides.
- If the evidence doesn't answer the question, say what is missing. Do not guess.
- Evidence in Hinglish is normal; understand it, but answer in the language of the question.
- Be concise: short paragraphs or bullets."""


class AgentState(TypedDict, total=False):
    question: str
    plan: dict
    evidence: list[dict]
    answer: str
    cited: list[int]
    warnings: list[str]
    provider: str
    trace: list[dict]


def _step(state: AgentState, name: str, started: float, **detail) -> list[dict]:
    return state.get("trace", []) + [{"step": name, "ms": int((time.time() - started) * 1000), **detail}]


def _load_events(conn: sqlite3.Connection, ids: list[str]) -> dict[str, sqlite3.Row]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: r for r in conn.execute(f"SELECT * FROM events WHERE id IN ({marks})", ids)}


def _evidence_item(conn, row: sqlite3.Row, why: str, score: float | None) -> dict:
    parent = None
    if row["parent_id"]:
        p = conn.execute("SELECT title, body FROM events WHERE id = ?", (row["parent_id"],)).fetchone()
        if p:
            parent = p["title"] or (p["body"] or "")[:100]
    text = "\n".join(x for x in (row["title"], row["body"]) if x)
    return {
        "event_id": row["id"], "source": row["source"], "kind": row["kind"], "author": row["author_name"],
        "status": row["status"], "assignees": json.loads(row["assignees"] or "[]"),
        "due": json.loads(row["extra"] or "{}").get("due"), "channel": row["channel"],
        "date": (row["updated_at"] or "")[:16].replace("T", " "), "url": row["url"], "parent": parent,
        "text": text[:SNIPPET_CHARS], "why": why, "score": score,
    }


def format_evidence(evidence: list[dict]) -> str:
    return "\n\n".join(format_item(n, e) for n, e in enumerate(evidence, 1))


def format_item(n: int, e: dict) -> str:
    """One evidence item exactly as the answering agent sees it: header (source, date, author, status,
    assignees, due date) and text. The Verifier and RAGAS get the same, so they judge what the agent saw."""
    meta = [f"{e.get('source', '')} {e.get('kind', '')}".strip(), e.get("date") or ""]
    for label, key in (("by ", "author"), ("in ", "channel"), ("status=", "status"), ("due=", "due")):
        if e.get(key):
            meta.append(f"{label}{e[key]}")
    if e.get("assignees"):
        meta.append("assigned=" + ",".join(e["assignees"]))
    head = f"[{n}] ({'; '.join(m for m in meta if m)})"
    if e.get("parent"):
        head += f" re: {e['parent']}"
    return f"{head}\n{e.get('text', '')}"


log = logging.getLogger("teampulse.retrieval")
CITATION = re.compile(r"\[(\d+)\]")


def verify_citations(answer: str, n_evidence: int, provider: str | None):
    """Remove citations to evidence that doesn't exist; warn if the answer cites nothing.

    Returns (clean_text, cited_numbers, invalid_numbers, warnings)."""
    invalid: list[int] = []

    def keep(match):
        num = int(match.group(1))
        if 1 <= num <= n_evidence:
            return match.group(0)
        invalid.append(num)
        return ""

    text = CITATION.sub(keep, answer)
    cited = sorted({int(x) for x in CITATION.findall(text)})
    invalid = sorted(set(invalid))
    warnings = []
    if invalid:
        warnings.append(f"Removed citation(s) to non-existent evidence: {invalid}")
    if n_evidence and not cited and provider not in ("none", "error"):
        warnings.append("The answer has no citations, so treat it as unverified.")
    return text, cited, invalid, warnings


class RetrievalAgent:
    def __init__(self, conn: sqlite3.Connection, index: EventIndex | None = None, llm: LLM | None = None):
        self.conn = conn
        self.index = index or EventIndex(conn)
        self.llm = llm or LLM(conn)
        self.graph = self._build()

    # ---- nodes ---------------------------------------------------------------------------
    def plan(self, state: AgentState) -> AgentState:
        started = time.time()
        today = datetime.now(timezone.utc).strftime("%A %Y-%m-%d")
        messages = [{"role": "system", "content": PLAN_PROMPT.format(today=today)},
                    {"role": "user", "content": state["question"]}]
        fallback = {"search_queries": [state["question"]], "sources": [], "since_days": None,
                    "wants_recent_activity": False}
        try:
            reply = self.llm.chat(messages, json_mode=True)
            plan = {**fallback, **extract_json(reply.text)}
            provider = reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:
            plan, provider = fallback, f"fallback ({type(exc).__name__})"
        plan["sources"] = [s for s in plan.get("sources") or [] if s in SOURCES]
        queries = [q for q in plan.get("search_queries") or [] if isinstance(q, str) and q.strip()][:4]
        if state["question"] not in queries:
            queries.append(state["question"])   # always search the raw question too
        plan["search_queries"] = queries
        return {"plan": plan, "trace": _step(state, "plan", started, provider=provider, plan=plan)}

    def retrieve(self, state: AgentState) -> AgentState:
        started = time.time()
        plan = state["plan"]
        sources = plan["sources"] or None
        since_days = plan.get("since_days")
        since_ts = None
        if isinstance(since_days, (int, float)) and since_days > 0:
            since_ts = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())

        best: dict[str, tuple[float, str]] = {}
        for query in plan["search_queries"]:
            for hit in self.index.search(query, k=8, sources=sources, since_ts=since_ts):
                if hit["event_id"] not in best or hit["score"] > best[hit["event_id"]][0]:
                    best[hit["event_id"]] = (hit["score"], f"matched '{query}'")
        ranked = sorted(best, key=lambda i: best[i][0], reverse=True)

        recent: list[str] = []
        if since_ts and plan.get("wants_recent_activity"):
            cutoff = datetime.fromtimestamp(since_ts, timezone.utc).isoformat()
            sql = "SELECT id FROM events WHERE updated_at >= ?"
            args: list = [cutoff]
            if sources:
                sql += f" AND source IN ({','.join('?' * len(sources))})"
                args += sources
            recent = [r[0] for r in self.conn.execute(sql + " ORDER BY updated_at DESC LIMIT 40", args)]

        # Interleave so both semantic matches and time-window activity make the cut.
        order, seen = [], set()
        for pair in zip(ranked, recent):
            for event_id in pair:
                if event_id not in seen:
                    order.append(event_id); seen.add(event_id)
        for event_id in ranked + recent:
            if event_id not in seen:
                order.append(event_id); seen.add(event_id)
        # Follow reply threads: a message's direct replies often hold the answer ("who helped?", "was it fixed?").
        threaded, seen = [], set()
        for event_id in order[:MAX_EVIDENCE]:
            if event_id in seen:
                continue
            threaded.append(event_id); seen.add(event_id)
            if event_id.startswith("discord:"):
                for (reply_id,) in self.conn.execute("SELECT id FROM events WHERE parent_id = ? ORDER BY created_at "
                                                     "LIMIT 2", (event_id,)):
                    if reply_id not in seen:
                        threaded.append(reply_id); seen.add(reply_id)
                        best.setdefault(reply_id, (None, "reply in the same thread"))
        order = threaded[:MAX_EVIDENCE]

        rows = _load_events(self.conn, order)
        evidence = []
        for event_id in order:
            if event_id in rows:
                score, why = best.get(event_id, (None, f"activity in last {since_days} days"))
                evidence.append(_evidence_item(self.conn, rows[event_id], why, score))
        return {"evidence": evidence, "trace": _step(
            state, "retrieve", started, semantic_hits=len(ranked), time_window_hits=len(recent),
            kept=len(evidence), sources=sources or "all", since_days=since_days)}

    def answer(self, state: AgentState) -> AgentState:
        started = time.time()
        if not state["evidence"]:
            text = "I couldn't find anything in GitHub, Notion or Discord about that."
            return {"answer": text, "provider": "none",
                    "trace": _step(state, "answer", started, provider="none", note="no evidence")}
        today = datetime.now(timezone.utc).strftime("%A %Y-%m-%d")
        messages = [
            {"role": "system", "content": ANSWER_PROMPT + f"\nToday is {today}."},
            {"role": "user", "content": f"Evidence:\n\n{format_evidence(state['evidence'])}\n\n"
                                        f"Question: {state['question']}"},
        ]
        try:
            reply = self.llm.chat(messages)
            text, provider = reply.text.strip(), reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:
            log.warning("Answering failed: %s", exc)   # details for the server log, not for the reader
            text, provider = "The AI is unavailable right now, so here are the sources an answer would use.", "error"
        return {"answer": text, "provider": provider,
                "trace": _step(state, "answer", started, provider=provider)}

    def check_citations(self, state: AgentState) -> AgentState:
        started = time.time()
        text, cited, invalid, warnings = verify_citations(
            state["answer"], len(state.get("evidence", [])), state.get("provider"))
        return {"answer": text, "cited": cited, "warnings": list(state.get("warnings", [])) + warnings,
                "trace": _step(state, "check_citations", started, cited=cited, invalid=invalid)}

    # ---- graph -----------------------------------------------------------------------------
    def _build(self):
        graph = StateGraph(AgentState)
        graph.add_node("plan", self.plan)
        graph.add_node("retrieve", self.retrieve)
        graph.add_node("answer", self.answer)
        graph.add_node("check_citations", self.check_citations)
        graph.set_entry_point("plan")
        graph.add_edge("plan", "retrieve")
        graph.add_edge("retrieve", "answer")
        graph.add_edge("answer", "check_citations")
        graph.add_edge("check_citations", END)
        return graph.compile()

    def ask(self, question: str) -> AgentState:
        return self.graph.invoke({"question": question, "trace": [], "warnings": []})


def main():
    from teampulse import db

    question = " ".join(sys.argv[1:]) or "What did the team finish this week and what is blocking us?"
    conn = db.connect()
    agent = RetrievalAgent(conn)
    agent.index.sync()
    result = agent.ask(question)
    print(result["answer"], "\n")
    for n in result["cited"]:
        e = result["evidence"][n - 1]
        print(f"[{n}] {e['source']}/{e['kind']} {e['date']} {e['url']}")
    for w in result["warnings"]:
        print("warning:", w)


if __name__ == "__main__":
    main()
