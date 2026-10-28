"""Verifier: does each cited statement actually say what its sources say?

The citation check (Week 3) only makes sure [3] points at real evidence. This goes further: the answer is
split into statements, and an AI judge reads each statement next to the evidence it cites and labels it
supported / partial / unsupported (one call per answer). Without AI, a rough word-overlap check is used
instead and labelled as such, since it misses paraphrases and Hinglish→English.

faithfulness = (supported + 0.5 × partial) / statements with citations
"""
from __future__ import annotations

import re

from teampulse.agents.retrieval import CITATION
from teampulse.llm import LLM, extract_json

JUDGE_PROMPT = """You check an AI assistant's answer against its sources. For each numbered statement you get
the evidence it cites (chat messages may be Hinglish). Decide:
- "supported": the cited evidence clearly states it (paraphrase and translation are fine)
- "partial": some of it is in the evidence, some isn't
- "unsupported": the cited evidence doesn't say it, or contradicts it
Judge only against the cited evidence, not general knowledge.
Return ONLY JSON: {"items": [{"statement": 1, "verdict": "supported", "reason": "short"}]}"""

STOP = {"the", "and", "for", "but", "with", "that", "this", "has", "have", "was", "were", "are", "is", "its", "it's",
        "not", "yet", "still", "from", "into", "about", "also", "which", "who", "what", "their", "they", "there"}


def statements(answer: str) -> list[dict]:
    """Split into bullets/sentences; keep the ones that cite something. Headings and short fragments are skipped."""
    out = []
    for line in answer.splitlines():
        line = line.strip().lstrip("-*•0123456789.) ").strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z*])", line):
            cites = sorted({int(n) for n in CITATION.findall(sentence)})
            text = re.sub(r"\s+([.,;:!?])", r"\1", CITATION.sub("", sentence).replace("**", "")).strip(" *")
            if len(text) < 12:
                continue
            out.append({"text": text, "cites": cites})
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP}


def lexical_verdict(statement: str, evidence_text: str) -> str:
    words = _words(statement)
    if not words:
        return "supported"
    overlap = len(words & _words(evidence_text)) / len(words)
    return "supported" if overlap >= 0.35 else "partial" if overlap >= 0.15 else "unsupported"


def verify(answer: str, evidence: list[dict], llm: LLM | None = None) -> dict:
    items = statements(answer)
    cited = [s for s in items if s["cites"] and all(1 <= n <= len(evidence) for n in s["cites"])]
    uncited = [s for s in items if not s["cites"]]
    if not cited:
        return {"statements": [], "uncited": len(uncited), "faithfulness": None, "method": "none"}

    def source_text(s):
        return "\n".join(f"[{n}] {evidence[n - 1].get('text', '')}" for n in s["cites"])

    method, verdicts = "lexical", {}
    if llm is not None:
        block = "\n\n".join(f"Statement {i}: {s['text']}\nCited evidence:\n{source_text(s)}" for i, s in enumerate(cited, 1))
        try:
            reply = llm.chat([{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": block}],
                             json_mode=True)
            for v in extract_json(reply.text).get("items", []):
                if isinstance(v, dict) and str(v.get("statement", "")).isdigit() and \
                        v.get("verdict") in ("supported", "partial", "unsupported"):
                    verdicts[int(v["statement"])] = (v["verdict"], str(v.get("reason") or "")[:200])
            if verdicts:
                method = "llm"
        except Exception:
            verdicts = {}
    results = []
    for i, s in enumerate(cited, 1):
        if i in verdicts:
            verdict, reason = verdicts[i]
        else:
            verdict, reason = lexical_verdict(s["text"], source_text(s)), "word overlap (rough)"
        results.append({**s, "verdict": verdict, "reason": reason})
    score = sum(1 if r["verdict"] == "supported" else 0.5 if r["verdict"] == "partial" else 0 for r in results)
    return {"statements": results, "uncited": len(uncited), "faithfulness": round(score / len(results), 3),
            "method": method if len(verdicts) == len(cited) else ("mixed" if verdicts else "lexical")}
