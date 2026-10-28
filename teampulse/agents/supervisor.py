"""Supervisor Agent (LangGraph): understands the question, splits it if needed, routes each part to a
specialist agent, and merges their cited answers into one.

    route → run_specialists → (one part)  single ────┐
                            → (many parts) synthesize ─┴→ verify (Verifier) → recall (team memory) → finish

Specialists are registered by name. Week 4 added the Retrieval Agent, Week 5 the Blocker Agent, Week 7 the Discrepancy Agent,
Week 8 the Contribution and Memory agents with `supervisor.register(...)`, and the router automatically
starts offering them.

    python -m teampulse.agents.supervisor "What changed this week and who owns payments?"
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, TypedDict

from langgraph.graph import END, StateGraph

from teampulse.agents.blockers import BlockerAgent
from teampulse.agents.contributions import ContributionAgent
from teampulse.agents.discrepancy import DiscrepancyAgent
from teampulse.agents.retrieval import CITATION, RetrievalAgent, format_evidence, verify_citations
from teampulse import config, memory, verifier
from teampulse.llm import LLM, extract_json

MAX_PARTS = 3
HISTORY_TURNS = 3

ROUTER_PROMPT = """You are the supervisor of a team of agents that answer questions about a student
team's project (GitHub, Notion, Discord chat in English/Hinglish). Today is {today}.

Available specialists:
{specialists}

Given the recent conversation and the new question, return ONLY JSON:
{{"standalone_question": the new question rewritten to make sense on its own (resolve "it", "that task",
   "and him?" using the conversation; otherwise copy it unchanged),
  "parts": [{{"question": "...", "specialist": "<name>"}}],
  "reason": "one short sentence"}}

Use ONE part unless the question asks about clearly separate things (e.g. "what changed this week AND who
owns payments") or needs a comparison that is easier to answer piece by piece. At most {max_parts} parts.
Pick the most specific specialist that fits each part."""

SYNTH_PROMPT = """Combine the partial answers below into one answer to the user's question.
Rules:
- Use only the numbered evidence and the partial answers. Keep citations like [3] exactly as numbered.
- Every factual statement needs a citation. Never invent a number.
- If parts disagree, say so and cite both.
- Answer in the language of the question. Be concise: short paragraphs or bullets."""


@dataclass
class Specialist:
    name: str
    description: str
    run: Callable[[str], dict]   # returns {"answer", "evidence", "cited", "warnings", "trace", "provider"}


class SupervisorState(TypedDict, total=False):
    question: str
    history: list[dict]          # [{"question": ..., "answer": ...}] oldest first
    route: dict
    results: list[dict]          # one per part: {"question", "specialist", "result"}
    answer: str
    evidence: list[dict]
    cited: list[int]
    warnings: list[str]
    provider: str
    trace: list[dict]
    memories: list[dict]         # remembered facts relevant to the question, each checked against live data
    verification: dict           # Verifier result: per-statement verdicts + faithfulness
    allow_memory_write: bool     # False for read-only users: memory commands are refused, whoever routed them


def _step(state, name: str, started: float, **detail) -> list[dict]:
    return state.get("trace", []) + [{"step": name, "ms": int((time.time() - started) * 1000), **detail}]


FALLBACK_ROUTES = [   # used only when the AI router is unavailable (e.g. free quota used up)
    ("contributions", r"\b(what|kya) (did|has|have) \w+ (do|done|did|work)|contribut|\bhelped\b|really (finish|complete)|\d+\s*%"),
    ("discrepancies", r"disagree|\bsync\b|up to date|outdated|mismatch|conflict|really (doing|working)|actually (doing|working)"),
    ("blockers", r"block|stuck|\batak|\batka|waiting|(code|pr) review|re-?review|needs? (a )?review|help|decid|decision|kya scene"),
]


def keyword_route(question: str, available) -> str:
    q = question.lower()
    for name, pattern in FALLBACK_ROUTES:
        if name in available and re.search(pattern, q):
            return name
    return "retrieval"


def _history_text(history: list[dict]) -> str:
    lines = []
    for turn in history[-HISTORY_TURNS:]:
        answer = CITATION.sub("", turn.get("answer", ""))[:400]
        lines.append(f"User: {turn['question']}\nAssistant: {answer}")
    return "\n\n".join(lines) or "(no earlier conversation)"


def merge_results(results: list[dict]) -> tuple[list[dict], list[str], int]:
    """Merge evidence from several specialist results into one numbered list, cited items first,
    and renumber each partial answer's citations to the merged numbering.

    Returns (merged_evidence, renumbered_answers, number_of_items_given_to_the_synthesizer)."""
    merged: list[dict] = []
    position: dict[str, int] = {}

    def add(item: dict) -> int:
        if item["event_id"] not in position:
            merged.append(item)
            position[item["event_id"]] = len(merged)
        return position[item["event_id"]]

    # Pass 1: everything a partial answer cited (or its top items if it cited nothing).
    for part in results:
        res = part["result"]
        evidence = res.get("evidence", [])
        chosen = res.get("cited") or list(range(1, min(len(evidence), 6) + 1))
        for n in chosen:
            add(evidence[n - 1])
    for_synthesis = len(merged)
    # Pass 2: the rest, so the UI can still show everything that was considered.
    for part in results:
        for item in part["result"].get("evidence", []):
            add(item)

    answers = []
    for part in results:
        evidence = part["result"].get("evidence", [])

        def renumber(match, evidence=evidence):
            n = int(match.group(1))
            return f"[{position[evidence[n - 1]['event_id']]}]" if 1 <= n <= len(evidence) else ""

        answers.append(CITATION.sub(renumber, part["result"].get("answer", "")))
    return merged, answers, for_synthesis


class SupervisorAgent:
    def __init__(self, conn: sqlite3.Connection, retrieval: RetrievalAgent | None = None, llm: LLM | None = None,
                 blockers: BlockerAgent | None = None, discrepancies: DiscrepancyAgent | None = None):
        self.conn = conn
        self.llm = llm or LLM(conn)
        self.retrieval = retrieval or RetrievalAgent(conn, llm=self.llm)
        self.index = self.retrieval.index
        self.specialists: dict[str, Specialist] = {}
        self.register(Specialist(
            "retrieval",
            "General questions: progress, what changed, status, owners, deadlines, decisions, what someone "
            "said. Searches GitHub, Notion and Discord and answers with citations.",
            self.retrieval.ask))
        self.blockers = blockers or BlockerAgent(conn, llm=self.llm, index=self.index)
        self.register(Specialist(BlockerAgent.name, BlockerAgent.description, self.blockers.ask))
        self.discrepancies = discrepancies or DiscrepancyAgent(conn, llm=self.llm, index=self.index)
        self.register(Specialist(DiscrepancyAgent.name, DiscrepancyAgent.description, self.discrepancies.ask))
        self.contributions = ContributionAgent(conn, llm=self.llm)
        self.register(Specialist(ContributionAgent.name, ContributionAgent.description, self.contributions.ask))
        self.memory = memory.MemoryAgent(conn, llm=self.llm)
        self.register(Specialist(memory.MemoryAgent.name, memory.MemoryAgent.description, self.memory.ask))
        self.verify_answers = (config.get("TEAMPULSE_VERIFY", "on") or "on").lower() != "off"
        self.graph = self._build()

    def register(self, specialist: Specialist) -> None:
        self.specialists[specialist.name] = specialist

    # ---- nodes ------------------------------------------------------------------------------
    def route(self, state: SupervisorState) -> SupervisorState:
        started = time.time()
        question = state["question"]
        if memory.REMEMBER_RE.match(question) or memory.FORGET_RE.match(question) or memory.RECALL_RE.search(question):
            route = {"standalone_question": question, "parts": [{"question": question, "specialist": "memory"}],
                     "reason": "memory command"}
            return {"route": route, "trace": _step(state, "route", started, provider="none (memory command)",
                                                   parts=route["parts"], reason=route["reason"])}
        fallback = {"standalone_question": question,
                    "parts": [{"question": question, "specialist": keyword_route(question, self.specialists)}],
                    "reason": "fallback"}
        catalog = "\n".join(f"- {s.name}: {s.description}" for s in self.specialists.values())
        messages = [
            {"role": "system", "content": ROUTER_PROMPT.format(
                today=datetime.now(timezone.utc).strftime("%A %Y-%m-%d"), specialists=catalog, max_parts=MAX_PARTS)},
            {"role": "user", "content": f"Conversation so far:\n{_history_text(state.get('history', []))}\n\n"
                                        f"New question: {question}"},
        ]
        try:
            reply = self.llm.chat(messages, json_mode=True)
            route = {**fallback, **extract_json(reply.text)}
            provider = reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:
            route, provider = fallback, f"fallback ({type(exc).__name__})"

        standalone = route.get("standalone_question")
        if not isinstance(standalone, str) or not standalone.strip():
            standalone = question
        parts = []
        for part in route.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("question"), str) and part["question"].strip():
                name = part.get("specialist") if part.get("specialist") in self.specialists else "retrieval"
                parts.append({"question": part["question"].strip(), "specialist": name})
        parts = parts[:MAX_PARTS] or [{"question": standalone, "specialist": "retrieval"}]
        route.update(standalone_question=standalone, parts=parts)
        return {"route": route, "trace": _step(state, "route", started, provider=provider,
                                               standalone_question=standalone, parts=parts,
                                               reason=route.get("reason"))}

    def run_specialists(self, state: SupervisorState) -> SupervisorState:
        trace, results = state.get("trace", []), []
        for part in state["route"]["parts"]:   # sequential on purpose: kinder to free-tier rate limits
            started = time.time()
            writes_memory = part["specialist"] == "memory" and (
                memory.REMEMBER_RE.match(part["question"]) or memory.FORGET_RE.match(part["question"]))
            if writes_memory and not state.get("allow_memory_write", True):
                result = {"answer": "Only the team leader can change team memory. You can still ask what's "
                                    "remembered.", "evidence": [], "cited": [], "warnings": [], "provider": "memory",
                          "trace": [{"step": "memory_write_refused", "ms": 0}]}
            else:
                result = self.specialists[part["specialist"]].run(part["question"])
            results.append({**part, "result": result})
            trace = trace + [{"step": f"specialist:{part['specialist']}", "question": part["question"],
                              "ms": int((time.time() - started) * 1000), "trace": result.get("trace", [])}]
        return {"results": results, "trace": trace}

    def single(self, state: SupervisorState) -> SupervisorState:
        res = state["results"][0]["result"]
        return {"answer": res.get("answer", ""), "evidence": res.get("evidence", []), "cited": res.get("cited", []),
                "warnings": list(res.get("warnings", [])), "provider": res.get("provider", "")}

    def synthesize(self, state: SupervisorState) -> SupervisorState:
        started = time.time()
        evidence, answers, shown = merge_results(state["results"])
        partials = "\n\n".join(f"Part {i}: {part['question']}\n{answer}"
                               for i, (part, answer) in enumerate(zip(state["results"], answers), 1))
        messages = [
            {"role": "system", "content": SYNTH_PROMPT},
            {"role": "user", "content": f"Evidence:\n\n{format_evidence(evidence[:shown])}\n\n"
                                        f"Partial answers:\n\n{partials}\n\nUser's question: {state['question']}"},
        ]
        try:
            reply = self.llm.chat(messages)
            text, provider = reply.text.strip(), reply.provider + (" (cached)" if reply.cached else "")
        except Exception as exc:   # no LLM: still give the user every part, correctly numbered
            text, provider = partials, f"error ({type(exc).__name__})"
        text, cited, invalid, warnings = verify_citations(text, shown, provider)
        sub_warnings = [w for part in state["results"] for w in part["result"].get("warnings", [])]
        return {"answer": text, "evidence": evidence, "cited": cited, "provider": provider,
                "warnings": sub_warnings + warnings,
                "trace": _step(state, "synthesize", started, provider=provider, parts=len(answers),
                               evidence_merged=len(evidence), cited=cited, invalid=invalid)}

    def verify(self, state: SupervisorState) -> SupervisorState:
        """Check each cited statement against the evidence it cites (one AI call; skipped for templates)."""
        started = time.time()
        provider = (state.get("provider") or "").split(" ")[0]
        if not self.verify_answers or not state.get("cited") or provider in ("template", "none", "memory", "error", "fallback"):
            return {}
        result = verifier.verify(state["answer"], state["evidence"], self.llm)
        warnings = list(state.get("warnings", []))
        bad = [s["text"] for s in result["statements"] if s["verdict"] == "unsupported"]
        if bad:
            warnings.append("Not supported by the cited source: " + " | ".join(f"“{b[:90]}”" for b in bad))
        return {"verification": result, "warnings": warnings,
                "trace": _step(state, "verify", started, method=result["method"], faithfulness=result["faithfulness"],
                               unsupported=len(bad), statements=len(result["statements"]))}

    def recall(self, state: SupervisorState) -> SupervisorState:
        """Attach remembered facts relevant to the question, each re-checked against live data."""
        started = time.time()
        if any(p["specialist"] == "memory" for p in state["route"]["parts"]):
            return {"memories": []}
        found = []
        for mem in memory.relevant(self.conn, state["route"]["standalone_question"]):
            found.append({"fact": mem["fact"], "created_at": mem["created_at"], **memory.verify(self.conn, mem)})
        trace = _step(state, "recall_memory", started, memories=len(found)) if found else state.get("trace", [])
        return {"memories": found, "trace": trace}

    # ---- graph -----------------------------------------------------------------------------
    def _build(self):
        graph = StateGraph(SupervisorState)
        graph.add_node("route", self.route)
        graph.add_node("run_specialists", self.run_specialists)
        graph.add_node("single", self.single)
        graph.add_node("synthesize", self.synthesize)
        graph.add_node("verify", self.verify)
        graph.add_node("recall", self.recall)
        graph.set_entry_point("route")
        graph.add_edge("route", "run_specialists")
        graph.add_conditional_edges("run_specialists",
                                    lambda s: "single" if len(s["results"]) == 1 else "synthesize",
                                    {"single": "single", "synthesize": "synthesize"})
        graph.add_edge("single", "verify")
        graph.add_edge("synthesize", "verify")
        graph.add_edge("verify", "recall")
        graph.add_edge("recall", END)
        return graph.compile()

    def ask(self, question: str, history: list[dict] | None = None, allow_memory_write: bool = True) -> SupervisorState:
        return self.graph.invoke({"question": question, "history": history or [], "trace": [], "warnings": [],
                                  "allow_memory_write": allow_memory_write})


def main():
    from teampulse import db, tracing

    tracing.setup()
    question = " ".join(sys.argv[1:]) or "What did the team finish this week, and who is working on payments?"
    agent = SupervisorAgent(db.connect())
    agent.index.sync()
    result = agent.ask(question)
    print(json.dumps(result["route"]["parts"], indent=2, ensure_ascii=False), "\n")
    print(result["answer"], "\n")
    for n in result["cited"]:
        e = result["evidence"][n - 1]
        print(f"[{n}] {e['source']}/{e['kind']} {e['date']} {e['url']}")
    for w in result["warnings"]:
        print("warning:", w)


if __name__ == "__main__":
    main()
