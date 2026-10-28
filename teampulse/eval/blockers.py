"""Evaluate the chat classifier against human labels: precision / recall / F1 per type + confusion matrix.

    python -m teampulse.eval.blockers                                   # synthetic dev set, keyword + LLM
    python -m teampulse.eval.blockers --data db                         # labels you made in the app
    python -m teampulse.eval.blockers --data my_labels.jsonl --classifier keyword

Reports are saved to data/eval/ as Markdown (paste into your report) and JSON.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from teampulse import config, db
from teampulse.llm import LLM
from teampulse.models import Event
from teampulse.signals import TYPES, KeywordClassifier, LLMClassifier, build_windows, load_messages

DEV_SET = Path(__file__).resolve().parents[2] / "eval_data" / "hinglish_dev.jsonl"


def load_jsonl(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def jsonl_to_db(rows: list[dict], conn: sqlite3.Connection) -> dict[str, str]:
    """Load labelled messages as Discord events. Each conversation gets its own day so windows stay apart."""
    conversations = list(dict.fromkeys(r["conversation"] for r in rows))
    base = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    events, gold = [], {}
    for r in rows:
        day = conversations.index(r["conversation"])
        at = (base + timedelta(days=day, minutes=r.get("minutes", 0))).isoformat()
        events.append(Event(source="discord", kind="message", native_id=r["id"], author=r["author"],
                            author_name=r["author"], body=r["text"], channel=r.get("channel", "general"),
                            parent_id=f"discord:{r['reply_to']}" if r.get("reply_to") else None,
                            created_at=at, updated_at=at))
        gold[f"discord:{r['id']}"] = r["label"]
    db.upsert_events(conn, events)
    return gold


def gold_from_db(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT l.event_id, l.label FROM labels l JOIN events e ON e.id = l.event_id WHERE l.label IN "
        f"({','.join(repr(t) for t in TYPES)})")}


def predict(conn: sqlite3.Connection, classifier, targets: set[str]) -> dict[str, str]:
    messages = load_messages(conn)
    lookup = {m.id: m for m in messages}
    windows = [w for w in build_windows(messages) if any(m.id in targets for m in w)]
    return {k: v["type"] for k, v in classifier.classify(windows, targets, lookup).items()}


def metrics(gold: dict[str, str], pred: dict[str, str]) -> dict:
    ids = sorted(gold)
    confusion = {g: {p: 0 for p in TYPES} for g in TYPES}
    for i in ids:
        confusion[gold[i]][pred.get(i, "noise")] += 1
    per_class = {}
    for t in TYPES:
        tp = confusion[t][t]
        fp = sum(confusion[g][t] for g in TYPES if g != t)
        fn = sum(confusion[t][p] for p in TYPES if p != t)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[t] = {"precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
                        "support": tp + fn}
    present = [t for t in TYPES if per_class[t]["support"]]
    correct = sum(confusion[t][t] for t in TYPES)
    return {"n": len(ids), "accuracy": round(correct / len(ids), 3) if ids else 0.0,
            "macro_f1": round(sum(per_class[t]["f1"] for t in present) / len(present), 3) if present else 0.0,
            "per_class": per_class, "confusion": confusion,
            "errors": [{"id": i, "gold": gold[i], "pred": pred.get(i, "noise")} for i in ids
                       if gold[i] != pred.get(i, "noise")]}


def to_markdown(results: dict[str, dict], data_name: str, texts: dict[str, str]) -> str:
    lines = [f"# Chat classifier evaluation", "", f"Data: `{data_name}` · {datetime.now():%Y-%m-%d %H:%M}", ""]
    lines += ["| Classifier | Messages | Accuracy | Macro F1 | Blocker P | Blocker R | Blocker F1 |",
              "|---|---|---|---|---|---|---|"]
    for name, m in results.items():
        b = m["per_class"]["blocker"]
        lines.append(f"| {name} | {m['n']} | {m['accuracy']:.2f} | {m['macro_f1']:.2f} | "
                     f"{b['precision']:.2f} | {b['recall']:.2f} | {b['f1']:.2f} |")
    for name, m in results.items():
        lines += ["", f"## {name}", "", "| Type | Precision | Recall | F1 | Support |", "|---|---|---|---|---|"]
        for t in TYPES:
            c = m["per_class"][t]
            lines.append(f"| {t} | {c['precision']:.2f} | {c['recall']:.2f} | {c['f1']:.2f} | {c['support']} |")
        lines += ["", "Confusion matrix (rows = human label, columns = predicted):", "",
                  "| | " + " | ".join(TYPES) + " |", "|---" * (len(TYPES) + 1) + "|"]
        for g in TYPES:
            lines.append(f"| **{g}** | " + " | ".join(str(m["confusion"][g][p]) for p in TYPES) + " |")
        if m["errors"]:
            lines += ["", f"Mistakes ({len(m['errors'])}):", ""]
            for e in m["errors"][:25]:
                lines.append(f"- `{e['gold']}` → `{e['pred']}`: {texts.get(e['id'], '')[:90]}")
    return "\n".join(lines) + "\n"


def run(data: str, which: str, conn: sqlite3.Connection | None = None, llm: LLM | None = None) -> tuple[dict, str]:
    if data == "db":
        conn = conn or db.connect()
        gold = gold_from_db(conn)
        data_name = config.DB_PATH
    else:
        conn = db.connect(":memory:")
        gold = jsonl_to_db(load_jsonl(data), conn)
        data_name = Path(data).name
    if not gold:
        raise SystemExit("No labelled messages found. Label some in the app (Label chat page) first.")
    texts = {r[0]: r[1] for r in conn.execute("SELECT id, body FROM events WHERE source='discord'")}
    classifiers = {}
    if which in ("keyword", "both"):
        classifiers["keyword baseline"] = KeywordClassifier()
    if which in ("llm", "both"):
        classifiers["LLM few-shot"] = LLMClassifier(llm or LLM(conn if data == "db" else None))
    results = {name: metrics(gold, predict(conn, clf, set(gold))) for name, clf in classifiers.items()}
    return results, to_markdown(results, data_name, texts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DEV_SET), help="'db' or a .jsonl file of labelled messages")
    parser.add_argument("--classifier", choices=["keyword", "llm", "both"], default="both")
    args = parser.parse_args()
    results, report = run(args.data, args.classifier)
    out = Path(config.DB_PATH).parent / "eval"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (out / f"blockers-{stamp}.md").write_text(report, encoding="utf-8")
    (out / f"blockers-{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(report)
    print(f"Saved to {out}/blockers-{stamp}.md")


if __name__ == "__main__":
    main()
