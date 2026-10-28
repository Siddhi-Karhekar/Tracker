"""Question-answering evaluation and architecture comparison on the demo team.

    python -m teampulse.eval.qa                   # plain RAG vs single agent vs multi-agent, 18 questions
    python -m teampulse.eval.qa --systems multi-agent --questions q01,q04

Each question (eval_data/demo_questions.jsonl) has:
  must_mention   groups of words; each group needs any one of its words in the answer  → correctness
  must_cite      groups of event ids; each group needs any one cited                   → citation recall
  must_not       phrases that must not appear (e.g. calling a claim false)
  specialists    which specialist(s) the Supervisor may route to                       → routing accuracy
Plus, for every answer: the Verifier's faithfulness (same judge for all systems), latency, AI calls, tokens.

Each system gets its own uncached LLM client, so calls and latency are real. The demo database is
rebuilt fresh for the run (data/eval/qa_demo.db); preparing it (chat scan, linking, claims) is not counted.
Free tiers: ~18 questions × 3 systems ≈ 150–250 AI calls. TEAMPULSE_LLM_MIN_INTERVAL=2 keeps you under
30 requests/minute.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from datetime import date, timedelta
from pathlib import Path

from teampulse import config, db, verifier
from teampulse.agents.retrieval import CITATION

QUESTIONS = Path(__file__).resolve().parents[2] / "eval_data" / "demo_questions.jsonl"
SYSTEMS = ["plain RAG", "single agent", "multi-agent"]


def load_questions(path: str | Path = QUESTIONS, only: list[str] | None = None) -> list[dict]:
    qs = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return [q for q in qs if not only or q["id"] in only]


def expand(term: str) -> list[str]:
    """'{date:+18}' → the date 18 days from today in the formats people write it."""
    if term.startswith("{date:") and term.endswith("}"):
        d = date.today() + timedelta(days=int(term[6:-1]))
        day = str(d.day)
        return [d.isoformat(), f"{day} {d:%b}".lower(), f"{d:%b} {day}".lower(), f"{day} {d:%B}".lower(),
                f"{d:%B} {day}".lower(), f"{day}th {d:%b}".lower(), f"{d:%b} {day}th".lower(), f"{d:%d/%m}"]
    return [term.lower()]


def _says(text: str, term: str) -> bool:
    """Whole-word start match: 'recommend' matches 'recommendation', but 'pr' doesn't match 'provider'."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}", text) is not None


def score(q: dict, result: dict) -> dict:
    text = CITATION.sub("", result.get("answer", "")).lower()
    if result.get("provider") == "error" or not text.strip():
        text = ""   # an error message is not an answer, whatever words it happens to contain
    mentions = [any(_says(text, v) for t in group for v in expand(t)) for group in q["must_mention"]]
    evidence = result.get("evidence", [])
    cited_ids = {evidence[n - 1]["event_id"] for n in result.get("cited", []) if 1 <= n <= len(evidence)}
    cites = [bool(set(group) & cited_ids) for group in q.get("must_cite", [])]
    bad = [p for p in q.get("must_not", []) if _says(text, p.lower())]
    out = {"correctness": round(sum(mentions) / len(mentions), 3), "passed": all(mentions) and not bad,
           "citation_recall": round(sum(cites) / len(cites), 3) if cites else None, "must_not_hits": bad}
    route = result.get("route")
    if route:
        used = [p["specialist"] for p in route.get("parts", [])]
        out["specialists"] = used
        out["routed_ok"] = bool(used) and all(u in q["specialists"] for u in used) and len(used) >= q.get("min_parts", 1)
    return out


def prepare(path: Path, llm) -> tuple:
    """Fresh demo database + index + chat scan, links, claims and discrepancies (not timed)."""
    from teampulse import discrepancies
    from teampulse.index import EventIndex
    from teampulse.seed_demo import seed

    for p in path.parent.glob(path.stem + "*"):
        if p.is_file():
            p.unlink()
    seed(str(path))
    conn = db.connect(str(path))
    index = EventIndex(conn, path=str(path.with_suffix("")) + "_chroma")
    discrepancies.run(conn, index, llm, refresh=True)
    return conn, index


def build_system(name: str, conn, index, llm):
    from teampulse.agents.baseline import PlainRAG
    from teampulse.agents.retrieval import RetrievalAgent
    from teampulse.agents.supervisor import SupervisorAgent

    if name == "plain RAG":
        return PlainRAG(conn, index, llm).ask
    retrieval = RetrievalAgent(conn, index=index, llm=llm)
    if name == "single agent":
        return retrieval.ask
    supervisor = SupervisorAgent(conn, retrieval=retrieval, llm=llm)
    return supervisor.ask


def run(questions: list[dict], systems: list[str], make_llm, judge, db_path: Path, setup_llm=None) -> dict:
    conn, index = prepare(db_path, setup_llm)
    rows = []
    for system in systems:
        llm = make_llm()
        ask = build_system(system, conn, index, llm)
        for q in questions:
            if hasattr(llm, "reset_stats"):
                llm.reset_stats()
            started = time.time()
            try:
                result = ask(q["question"])
                error = None
            except Exception as exc:   # one broken answer shouldn't stop the evaluation
                result, error = {"answer": "", "evidence": [], "cited": []}, f"{type(exc).__name__}: {exc}"
            latency = time.time() - started
            stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, **getattr(llm, "stats", {})}
            check = verifier.verify(result.get("answer", ""), result.get("evidence", []), judge)
            rows.append({"system": system, "id": q["id"], "category": q["category"], "question": q["question"],
                         "answer": result.get("answer", ""), "error": error, "latency": round(latency, 2),
                         "llm_calls": stats["calls"], "tokens": stats["prompt_tokens"] + stats["completion_tokens"],
                         "faithfulness": check["faithfulness"], "verify_method": check["method"],
                         **score(q, result)})
    return {"rows": rows, "summary": summarise(rows, systems)}


def _mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.mean(values), 3) if values else None


def summarise(rows: list[dict], systems: list[str]) -> dict:
    out = {}
    for system in systems:
        r = [x for x in rows if x["system"] == system]
        if not r:
            continue
        out[system] = {
            "questions": len(r), "pass_rate": _mean([1.0 if x["passed"] else 0.0 for x in r]),
            "correctness": _mean([x["correctness"] for x in r]), "citation_recall": _mean([x["citation_recall"] for x in r]),
            "faithfulness": _mean([x["faithfulness"] for x in r]),
            "median_latency": round(statistics.median(x["latency"] for x in r), 2),
            "llm_calls": _mean([x["llm_calls"] for x in r]), "tokens": _mean([x["tokens"] for x in r]),
            "routing": _mean([1.0 if x["routed_ok"] else 0.0 for x in r if "routed_ok" in x]),
            "errors": sum(1 for x in r if x["error"]),
            "by_category": {c: _mean([1.0 if x["passed"] else 0.0 for x in r if x["category"].split(" ·")[0] == c])
                            for c in sorted({x["category"].split(" ·")[0] for x in r})},
        }
    return out


def to_markdown(result: dict) -> str:
    s = result["summary"]
    fmt = lambda v, pct=False: "–" if v is None else (f"{v:.0%}" if pct else f"{v:.2f}" if isinstance(v, float) else str(v))
    lines = ["## Question answering: architecture comparison", "",
             f"{len({r['id'] for r in result['rows']})} questions about the demo team. Same index, evidence format and "
             "answer prompt for every system.", "",
             "| System | Pass rate | Correctness | Citation recall | Faithfulness | Median latency (s) | AI calls / question | Tokens / question | Routing |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, v in s.items():
        lines.append(f"| {name} | {fmt(v['pass_rate'], True)} | {fmt(v['correctness'], True)} | {fmt(v['citation_recall'], True)} | "
                     f"{fmt(v['faithfulness'], True)} | {v['median_latency']:.1f} | {fmt(v['llm_calls'])} | {fmt(v['tokens'])} | "
                     f"{fmt(v['routing'], True)} |")
    cats = sorted({c for v in s.values() for c in v["by_category"]})
    lines += ["", "Pass rate by question type:", "", "| Type | " + " | ".join(s) + " |", "|---" * (len(s) + 1) + "|"]
    for c in cats:
        lines.append(f"| {c} | " + " | ".join(fmt(v["by_category"].get(c), True) for v in s.values()) + " |")
    failed = [r for r in result["rows"] if not r["passed"]]
    if failed:
        lines += ["", "<details><summary>Questions not passed</summary>", ""]
        for r in failed:
            why = "error: " + r["error"] if r["error"] else f"correctness {r['correctness']:.0%}" + (
                f", said: {r['must_not_hits']}" if r["must_not_hits"] else "")
            lines.append(f"- **{r['system']}** · {r['id']} “{r['question']}”: {why}")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def main():
    from teampulse.llm import LLM

    parser = argparse.ArgumentParser()
    parser.add_argument("--systems", default=",".join(SYSTEMS))
    parser.add_argument("--questions", help="comma-separated ids, e.g. q01,q04")
    args = parser.parse_args()
    out = Path(config.DB_PATH).parent / "eval"
    out.mkdir(parents=True, exist_ok=True)
    result = run(load_questions(only=args.questions.split(",") if args.questions else None),
                 [s.strip() for s in args.systems.split(",")], make_llm=lambda: LLM(None), judge=LLM(None),
                 db_path=out / "qa_demo.db", setup_llm=LLM(None))
    report = to_markdown(result)
    stamp = time.strftime("%Y%m%d-%H%M")
    (out / f"qa-{stamp}.md").write_text(report, encoding="utf-8")
    (out / f"qa-{stamp}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
