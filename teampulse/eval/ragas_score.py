"""Score TeamPulse's answers with RAGAS, using a free AI model as the judge.

RAGAS needs older LangChain and OpenAI packages than TeamPulse, so it runs in its own environment and reads
the file that `python -m teampulse.eval.agents` writes (data/eval/agents-<time>-ragas.jsonl):

    python -m venv .venv-eval
    .venv-eval/bin/pip install -r requirements-eval.txt          # Windows: .venv-eval\\Scripts\\pip
    .venv-eval/bin/python -m teampulse.eval.ragas_score data/eval/agents-<time>-ragas.jsonl --judge groq

Metrics (each 0–1, higher is better):
  faithfulness           every claim in the answer is backed by the retrieved sources (no hallucination)
  context_precision      the sources it used are the relevant ones, ranked first      (uses the reference)
  context_recall         the sources cover everything the reference answer needs     (uses the reference)
  factual_correctness    the answer's claims match the reference answer               (F1 over claims)

Judges: groq (GROQ_API_KEY), gemini (GEMINI_API_KEY) or ollama (local; best for privacy). The judge reads the
demo team's fictional data only. Free tiers allow ~30 requests a minute and each answer takes about 4–10 judge
calls, so --min-interval spaces them out (default 2 s).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

JUDGES = {   # base URL, key variable, model variable, default model
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "GEMINI_MODEL",
               "gemini-3.8-flash"),
    "ollama": (None, None, "OLLAMA_MODEL", "qwen2.5:7b"),
}
METRICS = ("faithfulness", "context_precision", "context_recall", "factual_correctness")


def _env(name: str, default: str | None = None) -> str | None:
    if name in os.environ:
        return os.environ[name]
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.split("#")[0].strip():
                return value.split("#")[0].strip().strip('"').strip("'")
    return default


def judge_llm(name: str):
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory

    base_url, key_var, model_var, default_model = JUDGES[name]
    if name == "ollama":
        base_url, api_key = (_env("OLLAMA_URL", "http://localhost:11434") or "").rstrip("/") + "/v1", "ollama"
    else:
        api_key = _env(key_var)
        if not api_key:
            raise SystemExit(f"Set {key_var} in .env or the environment to use the {name} judge.")
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=3, timeout=90)
    return llm_factory(_env(model_var, default_model), provider="openai", client=client)


def build_metrics(llm) -> dict:
    from ragas.metrics.collections import ContextPrecisionWithReference, ContextRecall, FactualCorrectness, Faithfulness

    return {"faithfulness": Faithfulness(llm=llm), "context_precision": ContextPrecisionWithReference(llm=llm),
            "context_recall": ContextRecall(llm=llm), "factual_correctness": FactualCorrectness(llm=llm)}


async def score_row(row: dict, metrics: dict, min_interval: float) -> dict:
    """One answer, every metric. A metric that fails (rate limit, bad judge reply) is recorded as None."""
    contexts = row["retrieved_contexts"] or ["(no sources)"]
    calls = {
        "faithfulness": lambda m: m.ascore(user_input=row["user_input"], response=row["response"],
                                           retrieved_contexts=contexts),
        "context_precision": lambda m: m.ascore(user_input=row["user_input"], reference=row["reference"],
                                                retrieved_contexts=contexts),
        "context_recall": lambda m: m.ascore(user_input=row["user_input"], retrieved_contexts=contexts,
                                             reference=row["reference"]),
        "factual_correctness": lambda m: m.ascore(response=row["response"], reference=row["reference"]),
    }
    out = {"id": row["id"], "agent": row.get("agent", "supervisor"), "question": row["user_input"]}
    for name, metric in metrics.items():
        try:
            result = await calls[name](metric)
            out[name] = None if result.value is None else round(float(result.value), 3)
        except Exception as exc:   # noqa: BLE001  (one bad judge reply shouldn't stop the run)
            out[name], out.setdefault("errors", {})[name] = None, f"{type(exc).__name__}: {str(exc)[:160]}"
        await asyncio.sleep(min_interval)
    return out


def summarise(rows: list[dict]) -> dict:
    out = {}
    for agent in sorted({r["agent"] for r in rows}):
        mine = [r for r in rows if r["agent"] == agent]
        out[agent] = {m: (round(statistics.mean(v), 3) if (v := [r[m] for r in mine if r.get(m) is not None]) else None)
                      for m in METRICS}
        out[agent]["answers"] = len(mine)
        out[agent]["metric_errors"] = sum(len(r.get("errors", {})) for r in mine)
    return out


def to_markdown(summary: dict, rows: list[dict], judge: str) -> str:
    pct = lambda v: "–" if v is None else f"{v:.0%}"
    lines = [f"## RAGAS scores (judge: {judge})", "",
             "| Agent | Answers | Faithfulness | Context precision | Context recall | Factual correctness | Judge errors |",
             "|---|---|---|---|---|---|---|"]
    for agent, s in summary.items():
        lines.append(f"| {agent} | {s['answers']} | {pct(s['faithfulness'])} | {pct(s['context_precision'])} | "
                     f"{pct(s['context_recall'])} | {pct(s['factual_correctness'])} | {s['metric_errors']} |")
    weakest = sorted((r for r in rows if r.get("faithfulness") is not None), key=lambda r: r["faithfulness"])[:5]
    if weakest:
        lines += ["", "Lowest faithfulness (check these answers first):", ""]
        lines += [f"- {r['id']} “{r['question']}”: {pct(r['faithfulness'])}" for r in weakest]
    return "\n".join(lines) + "\n"


def run(rows: list[dict], llm, min_interval: float = 2.0) -> tuple[list[dict], dict]:
    metrics = build_metrics(llm)

    async def go():
        return [await score_row(r, metrics, min_interval) for r in rows]
    scored = asyncio.run(go())
    return scored, summarise(scored)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="the *-ragas.jsonl file from teampulse.eval.agents")
    parser.add_argument("--judge", choices=sorted(JUDGES), default="groq")
    parser.add_argument("--min-interval", type=float, default=2.0, help="seconds between judge calls")
    parser.add_argument("--limit", type=int, help="score only the first N answers (quick check)")
    args = parser.parse_args()
    path = Path(args.input)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = rows[: args.limit] if args.limit else rows
    print(f"Scoring {len(rows)} answers with the {args.judge} judge…")
    started = time.time()
    scored, summary = run(rows, judge_llm(args.judge), args.min_interval)
    report = to_markdown(summary, scored, args.judge)
    stem = path.with_suffix("").as_posix().removesuffix("-ragas")
    Path(stem + "-ragas-scores.json").write_text(json.dumps({"summary": summary, "rows": scored}, indent=2,
                                                            ensure_ascii=False), encoding="utf-8")
    Path(stem + "-ragas-scores.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"Done in {time.time() - started:.0f} s. Saved {stem}-ragas-scores.md and .json")


if __name__ == "__main__":
    main()
