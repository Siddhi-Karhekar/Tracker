"""Plain RAG baseline for the architecture comparison: search the raw question once, answer once.

No query planning, no Hinglish rewrites, no time-window activity, no specialists, no structured data
(signals, tasks, claims). Same index, same evidence format and same answer prompt as the Retrieval Agent,
so differences in the results come from the architecture, not from prompt wording.
"""
from __future__ import annotations

import sqlite3
import time

from teampulse.agents.retrieval import ANSWER_PROMPT, _evidence_item, _load_events, format_evidence, verify_citations
from teampulse.index import EventIndex
from teampulse.llm import LLM

K = 8


class PlainRAG:
    def __init__(self, conn: sqlite3.Connection, index: EventIndex, llm: LLM, k: int = K):
        self.conn, self.index, self.llm, self.k = conn, index, llm, k

    def ask(self, question: str) -> dict:
        started = time.time()
        hits = self.index.search(question, k=self.k)
        rows = _load_events(self.conn, [h["event_id"] for h in hits])
        evidence = [_evidence_item(self.conn, rows[h["event_id"]], "similarity", h["score"])
                    for h in hits if h["event_id"] in rows]
        trace = [{"step": "retrieve", "ms": int((time.time() - started) * 1000), "kept": len(evidence)}]
        started = time.time()
        try:
            reply = self.llm.chat([{"role": "system", "content": ANSWER_PROMPT},
                                   {"role": "user", "content": f"Evidence:\n\n{format_evidence(evidence)}\n\nQuestion: {question}"}])
            text, provider = reply.text.strip(), reply.provider
        except Exception as exc:
            text, provider = f"(no answer: {type(exc).__name__})", "error"
        text, cited, invalid, warnings = verify_citations(text, len(evidence), provider)
        trace.append({"step": "answer", "ms": int((time.time() - started) * 1000), "provider": provider, "invalid": invalid})
        return {"answer": text, "evidence": evidence, "cited": cited, "warnings": warnings, "provider": provider,
                "trace": trace}
