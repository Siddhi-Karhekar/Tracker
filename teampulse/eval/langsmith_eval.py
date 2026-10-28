"""LangSmith: the golden questions as a dataset, each run as an experiment you can compare over time.

    LANGSMITH_API_KEY=... python -m teampulse.eval.langsmith_eval            # with your AI provider
    LANGSMITH_API_KEY=... python -m teampulse.eval.langsmith_eval --offline  # rule-based paths only

Creates (once) the dataset "teampulse-golden" from eval_data/demo_questions.jsonl, runs the whole system on
every question, and scores each answer with TeamPulse's own evaluators:
  correctness       share of expected facts the answer states
  citation_recall   share of expected sources it cites
  routing           the Supervisor used only the right specialists
  guardrails_clean  no guardrail had to block or repair the answer
  faithfulness      the Verifier's share of statements supported by their cited source
Results appear in LangSmith under Datasets → teampulse-golden → Experiments, side by side with earlier runs.

Live tracing (optional): set LANGSMITH_TRACING=true and LANGSMITH_API_KEY, and every question the app answers
shows up in LangSmith as a trace (Supervisor graph, specialists, AI calls). Traces contain the team's chat and
issue text, so use it with the demo team, or only if your team agrees.

Free Developer plan: 1 seat, 5,000 traces a month, 14-day trace retention (LangChain pricing page, Oct 2026).
One evaluation of the 18 questions is roughly 18 traces plus their AI calls.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from teampulse import config, verifier
from teampulse.eval import qa

DATASET = "teampulse-golden"


def examples(questions: list[dict]) -> list[dict]:
    return [{"inputs": {"question": q["question"]},
             "outputs": {"reference": q.get("reference", ""), "must_mention": q["must_mention"],
                         "must_cite": q.get("must_cite", []), "must_not": q.get("must_not", []),
                         "specialists": q["specialists"]},
             "metadata": {"id": q["id"], "category": q["category"]}} for q in questions]


def _question(outputs_ref: dict) -> dict:
    return {"must_mention": outputs_ref["must_mention"], "must_cite": outputs_ref.get("must_cite", []),
            "must_not": outputs_ref.get("must_not", []), "specialists": outputs_ref["specialists"]}


# ---- evaluators (LangSmith passes outputs and reference_outputs by parameter name) ---------------------

def correctness(outputs: dict, reference_outputs: dict) -> dict:
    return {"key": "correctness", "score": qa.score(_question(reference_outputs), outputs["result"])["correctness"]}


def citation_recall(outputs: dict, reference_outputs: dict) -> dict:
    return {"key": "citation_recall",
            "score": qa.score(_question(reference_outputs), outputs["result"])["citation_recall"]}


def routing(outputs: dict, reference_outputs: dict) -> dict:
    used = [p["specialist"] for p in (outputs["result"].get("route") or {}).get("parts", [])]
    return {"key": "routing", "score": float(bool(used) and all(u in reference_outputs["specialists"] for u in used))}


def guardrails_clean(outputs: dict) -> dict:
    fired = [g for g in outputs["result"].get("guardrails", []) if g["action"] in ("block", "repair")]
    return {"key": "guardrails_clean", "score": 0.0 if fired else 1.0,
            "comment": ", ".join(f"{g['agent']}:{g['guard']}" for g in fired) or None}


def faithfulness(outputs: dict) -> dict:
    return {"key": "faithfulness", "score": outputs.get("faithfulness")}


EVALUATORS = [correctness, citation_recall, routing, guardrails_clean, faithfulness]


def make_target(agent, judge, local: bool = False):
    from contextlib import nullcontext

    from langsmith import tracing_context

    def target(inputs: dict) -> dict:
        # In a local run, LangGraph's own tracer would still try to upload the graph's steps; switch it off.
        with tracing_context(enabled=False) if local else nullcontext():
            result = agent.ask(inputs["question"])
        check = verifier.verify(result.get("answer", ""), result.get("evidence", []), judge)
        slim = {k: result.get(k) for k in ("answer", "cited", "route", "guardrails", "warnings", "provider")}
        slim["evidence"] = [{"event_id": e["event_id"], "source": e["source"]} for e in result.get("evidence", [])]
        return {"answer": result.get("answer", ""), "result": slim, "faithfulness": check["faithfulness"]}
    return target


def sync_dataset(client, questions: list[dict], name: str = DATASET):
    """Create the dataset once; later runs add only questions it doesn't have yet."""
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
        have = {(e.metadata or {}).get("id") for e in client.list_examples(dataset_id=dataset.id)}
        new = [e for e in examples(questions) if e["metadata"]["id"] not in have]
    else:
        dataset = client.create_dataset(name, description="TeamPulse golden questions about the fictional demo team")
        new = examples(questions)
    if new:
        client.create_examples(dataset_id=dataset.id, examples=new)
    return dataset


def run_experiment(offline: bool, folder: Path, client=None, data=None, upload: bool = True, metadata: dict | None = None):
    """Run the system on the dataset and score it. `data`/`upload=False` allow a local run without an account."""
    from langsmith import Client, evaluate

    from teampulse.eval import agents
    from teampulse.llm import LLM

    llm = LLM(None, providers=["none"]) if offline else LLM(None)
    conn, index, _ = agents.prepare(folder, llm)
    agent = agents.supervisor(conn, index, llm)
    questions = qa.load_questions()
    if data is None:
        client = client or Client()
        data = sync_dataset(client, questions).name
    elif not upload and client is None:   # local run: preset server info so the client never calls home
        from langsmith.schemas import LangSmithInfo
        client = Client(api_key="local-only", info=LangSmithInfo())
    return evaluate(make_target(agent, None if offline else llm, local=not upload), data=data, evaluators=EVALUATORS,
                    experiment_prefix=f"teampulse-{'offline' if offline else 'ai'}", client=client,
                    metadata={"mode": "offline" if offline else "ai",
                              "providers": config.get("LLM_PROVIDERS", "groq,gemini,ollama"), **(metadata or {})},
                    max_concurrency=1, upload_results=upload,
                    # with upload off, LangSmith still uploads evaluator traces unless told not to
                    disable_evaluator_tracing=not upload)


def summary(results) -> dict:
    """Mean of each evaluator over an ExperimentResults object."""
    scores: dict[str, list] = {}
    for row in results:
        for r in row["evaluation_results"]["results"]:
            if r.score is not None:
                scores.setdefault(r.key, []).append(float(r.score))
    return {k: round(sum(v) / len(v), 3) for k, v in scores.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    if not config.get("LANGSMITH_API_KEY"):
        raise SystemExit("Set LANGSMITH_API_KEY (free at smith.langchain.com → Settings → API keys).")
    started = time.time()
    results = run_experiment(args.offline, Path(config.DB_PATH).parent / "eval")
    print(summary(results))
    print(f"Done in {time.time() - started:.0f} s. Open LangSmith → Datasets → {DATASET} → Experiments.")


if __name__ == "__main__":
    main()
