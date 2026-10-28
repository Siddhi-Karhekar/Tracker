"""Every evaluation in one command → data/eval/report-<time>.md, ready to paste into your project report.

    python -m teampulse.eval.run_all              # everything, using your free AI keys (≈ 200–300 calls)
    python -m teampulse.eval.run_all --no-llm     # rules/keyword parts only, offline, free, ~1 minute
    python -m teampulse.eval.run_all --skip-qa    # everything except the 3-system comparison (≈ 15 calls)

Sections:
  1. Chat classifier (Week 5)      keyword baseline vs AI, synthetic Hinglish dev set (+ your labels if any)
  2. Chat → task linking (Week 6)  refs vs similarity vs AI, demo answer key (+ your corrections if any)
  3. Discrepancy detection (Week 7) rules only vs rules + AI, demo answer key (+ your verdicts if any)
  4. Question answering (Week 9)   plain RAG vs single agent vs multi-agent, 18 demo questions
"""
from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

from teampulse import config, db
from teampulse.eval import blockers, discrepancies, linking, qa
from teampulse.llm import LLM

CAVEATS = """## How to read these numbers

- **The demo is small and synthetic** (4 people, 41 items, 18 questions), and I designed the demo data and the
  rules together, so scores on it are a sanity check and a fair *comparison between methods*, not proof of
  real-world accuracy. Use the "your data" rows (from labels, link corrections and verdicts you add in the app)
  for real-world claims.
- **Correctness** is keyword-based: each expected fact can be phrased several ways, but an answer can still be
  right in words the checker doesn't expect. Spot-check the answers in the JSON file.
- **Faithfulness** uses one AI judge for every system. Where the method says "lexical", no AI was available and a
  rough word-overlap check was used instead; it under-rates Hinglish evidence and paraphrases.
- **Latency and AI calls** depend on the provider and its load at the time; compare systems within one run.
- The keyword chat rules were adjusted once against the dev set, so the keyword baseline there is optimistic.
"""


def environment() -> str:
    providers = config.get("LLM_PROVIDERS", "groq,gemini,ollama")
    models = {p: config.get(f"{p.upper()}_MODEL", "") for p in providers.split(",")}
    return (f"- Date: {time.strftime('%Y-%m-%d %H:%M')}\n- AI providers (in order): {providers} · models: {models}\n"
            f"- Embedding model: {config.get('TEAMPULSE_EMBEDDER', 'intfloat/multilingual-e5-small')}\n"
            f"- Python {platform.python_version()} on {platform.system()}\n")


def main():
    from teampulse.index import EventIndex
    from teampulse.seed_demo import seed

    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true", help="offline: rules and keyword methods only")
    parser.add_argument("--skip-qa", action="store_true", help="skip the 3-system question-answering comparison")
    args = parser.parse_args()
    out = Path(config.DB_PATH).parent / "eval"
    out.mkdir(parents=True, exist_ok=True)
    make_llm = (lambda: LLM(None, providers=["off"])) if args.no_llm else (lambda: LLM(None))
    use_llm = not args.no_llm
    if use_llm:
        from teampulse.llm import require_working
        print("Checking the AI providers…")
        require_working(LLM(None))
    sections, raw = [f"# TeamPulse evaluation report\n\n{environment()}"], {}

    print("1/4 Chat classifier…")
    which = "both" if use_llm else "keyword"
    res, md = blockers.run(str(blockers.DEV_SET), which, llm=make_llm() if use_llm else None)
    raw["classifier_dev"] = res
    sections.append("## 1. Chat classifier (synthetic Hinglish dev set)\n\n" + md.split("\n", 3)[3])
    own = db.connect()
    if blockers.gold_from_db(own) and own.execute("SELECT 1 FROM labels WHERE labeller != 'answer-key' LIMIT 1").fetchone():
        res, md = blockers.run("db", which, conn=own, llm=make_llm() if use_llm else None)
        raw["classifier_yours"] = res
        sections.append("### Your labelled messages\n\n" + md.split("\n", 3)[3])

    print("2/4 Linking and 3/4 discrepancies (fresh demo copy)…")
    demo_path = out / "eval_demo.db"
    for p in out.glob("eval_demo*"):
        if p.is_file():
            p.unlink()
    seed(str(demo_path))
    key = str(demo_path.with_name("demo_answer_key.json"))
    conn = db.connect(str(demo_path))
    index = EventIndex(conn, path=str(demo_path.with_suffix("")) + "_chroma")
    index.sync()
    from teampulse import claims
    claims.refresh(conn, index, make_llm() if use_llm else None, scan_chat=True)
    res, md = linking.run(conn, index, make_llm() if use_llm else None, key)
    raw["linking"] = res
    sections.append("## 2. Chat → task linking (demo answer key)\n\n" + md.split("\n", 2)[2])
    res, md = discrepancies.run_with_key(conn, index, make_llm() if use_llm else None, key)
    raw["discrepancies"] = res
    sections.append("## 3. Discrepancy detection (demo answer key)\n\n" + md.split("\n", 2)[2])
    try:
        res, md = discrepancies.run_with_verdicts(own)
        raw["discrepancies_yours"] = res
        sections.append("### Your verdicts\n\n" + md.split("\n", 2)[2])
    except SystemExit:
        pass

    if not args.skip_qa:
        print("4/4 Question answering: 3 systems × 18 questions (this takes a while on free tiers)…")
        result = qa.run(qa.load_questions(), qa.SYSTEMS, make_llm=make_llm, judge=make_llm() if use_llm else None,
                        db_path=out / "qa_demo.db", setup_llm=make_llm() if use_llm else None)
        raw["qa"] = result
        sections.append(qa.to_markdown(result).replace("## Question answering", "## 4. Question answering", 1))
        if args.no_llm:
            sections.append("_Run without AI: the plain-RAG and single-agent answers are empty, while the specialist "
                            "agents fall back to evidence lists. Run with AI keys for the real comparison._\n")

    sections.append(CAVEATS)
    stamp = time.strftime("%Y%m%d-%H%M")
    report = "\n\n".join(sections)
    (out / f"report-{stamp}.md").write_text(report, encoding="utf-8")
    (out / f"report-{stamp}.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(report)
    print(f"\nSaved: {out / f'report-{stamp}.md'} (tables) and .json (every answer, for spot-checking)")


if __name__ == "__main__":
    main()
