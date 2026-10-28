"""Evaluate chat→task linking: how often a message is attached to the right task (or correctly to none).

    python -m teampulse.eval.linking --key data/demo_answer_key.json     # demo answer key
    python -m teampulse.eval.linking                                     # your corrections from the Tasks page

Compares three methods on the same messages:
    refs        exact "#12" in the message or (unambiguously) in its conversation
    + similarity  refs, else the most similar task above the threshold
    + LLM       refs, else the LLM picks from a shortlist of similar tasks (or says none)

Gold labels from the Tasks page are corrections you made, so the predictions here are recomputed from
scratch rather than read back from the database (which already contains your corrections).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from teampulse import config, db
from teampulse.llm import LLM
from teampulse.signals import Msg, TaskLinker, build_windows, load_messages
from teampulse.tasks import Registry, llm_choose


def gold_from(conn: sqlite3.Connection, key_path: str | None) -> dict[str, str | None]:
    if key_path:
        return json.loads(Path(key_path).read_text(encoding="utf-8"))["chat_links"]
    return {r[0]: r[1] for r in conn.execute("SELECT event_id, task_id FROM link_feedback")}


def predict(conn: sqlite3.Connection, index, llm: LLM | None, gold: dict) -> dict[str, dict]:
    Registry(conn).build()
    to_task = {r[0]: r[1] for r in conn.execute("SELECT event_id, task_id FROM task_links")}
    messages = load_messages(conn)
    window_of: dict[str, list[Msg]] = {m.id: w for w in build_windows(messages) for m in w}
    hints = {r[0]: r[1] for r in conn.execute("SELECT event_id, task_hint FROM signals")}
    refs_only, with_sim = TaskLinker(conn, None), TaskLinker(conn, index)
    out = {"refs": {}, "+ similarity": {}, "+ LLM": {}}
    need_llm = []
    for event_id in gold:
        msg = next((m for m in window_of.get(event_id, []) if m.id == event_id), None)
        if msg is None:
            continue
        hint = hints.get(event_id) or ""
        ref_event, method, _ = refs_only.link(msg, window_of[event_id], hint)
        out["refs"][event_id] = to_task.get(ref_event)
        sim_event, _, _ = with_sim.link(msg, window_of[event_id], hint)
        out["+ similarity"][event_id] = to_task.get(sim_event)
        if ref_event:
            out["+ LLM"][event_id] = to_task.get(ref_event)
        else:
            need_llm.append({"event_id": event_id, "body": msg.text, "task_hint": hint, "author_name": msg.author,
                             "channel": msg.channel, "created_at": msg.created_at.isoformat()})
    if llm is not None and index is not None and need_llm:
        choices, _ = llm_choose(conn, index, llm, need_llm)
        out["+ LLM"].update({e: choices.get(e) for e in (r["event_id"] for r in need_llm)})
    else:
        out.pop("+ LLM")
    return out


def score(gold: dict, pred: dict) -> dict:
    ids = [i for i in gold if i in pred]
    correct = sum(1 for i in ids if pred[i] == gold[i])
    linked = [i for i in ids if pred[i]]
    linked_right = sum(1 for i in linked if pred[i] == gold[i])
    should_link = [i for i in ids if gold[i]]
    return {"n": len(ids), "accuracy": round(correct / len(ids), 3) if ids else 0.0,
            "precision": round(linked_right / len(linked), 3) if linked else 0.0,
            "recall": round(sum(1 for i in should_link if pred[i] == gold[i]) / len(should_link), 3) if should_link else 0.0,
            "errors": [{"id": i, "gold": gold[i], "pred": pred[i]} for i in ids if pred[i] != gold[i]]}


def run(conn: sqlite3.Connection, index, llm: LLM | None, key_path: str | None = None) -> tuple[dict, str]:
    gold = gold_from(conn, key_path)
    if not gold:
        raise SystemExit("No gold links. Correct some links on the Tasks page, or pass --key.")
    results = {name: score(gold, pred) for name, pred in predict(conn, index, llm, gold).items()}
    texts = {r[0]: r[1] for r in conn.execute("SELECT id, body FROM events WHERE source='discord'")}
    lines = ["# Chat → task linking evaluation", "", f"{datetime.now():%Y-%m-%d %H:%M} · {len(gold)} gold links", "",
             "| Method | Messages | Accuracy | Precision (links made) | Recall (links needed) |", "|---|---|---|---|---|"]
    for name, r in results.items():
        lines.append(f"| {name} | {r['n']} | {r['accuracy']:.2f} | {r['precision']:.2f} | {r['recall']:.2f} |")
    for name, r in results.items():
        if r["errors"]:
            lines += ["", f"**{name}: mistakes**", ""]
            lines += [f"- {texts.get(e['id'], e['id'])[:70]} → expected `{e['gold']}`, got `{e['pred']}`"
                      for e in r["errors"]]
    return results, "\n".join(lines) + "\n"


def main():
    from teampulse.index import EventIndex

    parser = argparse.ArgumentParser()
    parser.add_argument("--key", help="answer key JSON with a 'chat_links' map (e.g. data/demo_answer_key.json)")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    conn = db.connect()
    index = EventIndex(conn)
    index.sync()
    results, report = run(conn, index, None if args.no_llm else LLM(conn), args.key)
    out = Path(config.DB_PATH).parent / "eval"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"linking-{datetime.now():%Y%m%d-%H%M}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
