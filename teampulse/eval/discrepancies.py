"""Evaluate discrepancy detection.

    python -m teampulse.eval.discrepancies --key data/demo_answer_key.json   # demo: vs planted conflicts
    python -m teampulse.eval.discrepancies                                   # real team: vs your verdicts

With an answer key, both modes run on separate in-memory copies of the database so they don't affect
each other or your data:
    rules only   no AI anywhere (keyword chat scan, ref/similarity links, unverified plan check)
    rules + AI   AI chat scan, AI chat links and extraction, AI plan-page check
A detected conflict matches a planted one when the type agrees and they share at least one source item.

Without a key, precision comes from the verdicts people gave on the Discrepancies page
(real vs false alarm). Recall can't be measured without knowing what was missed.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from teampulse import config, db
from teampulse import discrepancies as disc
from teampulse.llm import LLM

SAME_TYPE = {"stale_plan": {"stale_plan", "plan_outdated"}, "status": {"status"}, "owner": {"owner"},
             "deadline": {"deadline"}}


def _copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    conn.backup(mem)
    mem.row_factory = sqlite3.Row
    return mem


def match(found: list[dict], gold: list[dict]) -> dict:
    used, hits, missed = set(), [], []
    for g in gold:
        hit = next((i for i, d in enumerate(found) if i not in used and d["type"] in SAME_TYPE[g["type"]]
                    and {s["event_id"] for s in d["sides"]} & set(g["events"])), None)
        if hit is None:
            missed.append(g)
        else:
            used.add(hit)
            hits.append((g, found[hit]))
    false_alarms = [d for i, d in enumerate(found) if i not in used]
    return {"gold": len(gold), "found": len(found), "matched": len(hits),
            "recall": round(len(hits) / len(gold), 3) if gold else 0.0,
            "precision": round(len(hits) / len(found), 3) if found else 0.0,
            "hits": [{"planted": g["detail"], "detected": d["title"], "severity": d["severity"]} for g, d in hits],
            "missed": [g["detail"] for g in missed], "false_alarms": [d["title"] for d in false_alarms]}


def run_with_key(conn, index, llm: LLM | None, key_path: str) -> tuple[dict, str]:
    gold = json.loads(Path(key_path).read_text(encoding="utf-8"))["discrepancies"]
    from teampulse import claims

    results = {}
    modes = {"rules only": None} | ({"rules + AI": llm} if llm is not None else {})
    for name, model in modes.items():
        copy = _copy(conn)
        with copy:   # start each mode from the same clean slate
            for table in ("signals", "extractions", "claims", "tasks", "task_links", "discrepancies"):
                copy.execute(f"DELETE FROM {table}")
        claims.refresh(copy, index, model, scan_chat=True)
        results[name] = match(disc.detect(copy, model), gold)

    lines = ["# Discrepancy detection evaluation", "", f"{datetime.now():%Y-%m-%d %H:%M} · {len(gold)} planted conflicts", "",
             "| Mode | Detected | Matched | Recall | Precision |", "|---|---|---|---|---|"]
    for name, r in results.items():
        lines.append(f"| {name} | {r['found']} | {r['matched']}/{r['gold']} | {r['recall']:.2f} | {r['precision']:.2f} |")
    for name, r in results.items():
        lines += ["", f"## {name}", ""]
        lines += [f"- ✅ ({h['severity']}) {h['detected']}" for h in r["hits"]]
        lines += [f"- ❌ missed: {m}" for m in r["missed"]]
        lines += [f"- ⚠️ false alarm: {f}" for f in r["false_alarms"]]
    return results, "\n".join(lines) + "\n"


def run_with_verdicts(conn) -> tuple[dict, str]:
    rows = conn.execute("SELECT type, verdict, COUNT(*) AS n FROM discrepancies WHERE verdict IS NOT NULL "
                        "GROUP BY type, verdict").fetchall()
    if not rows:
        raise SystemExit("No verdicts yet. Mark findings as real / false alarm on the Discrepancies page, or pass --key.")
    by_type: dict[str, dict] = {}
    for r in rows:
        by_type.setdefault(r["type"], {"real": 0, "false_alarm": 0})[r["verdict"]] = r["n"]
    total_real = sum(v["real"] for v in by_type.values())
    total = sum(v["real"] + v["false_alarm"] for v in by_type.values())
    lines = ["# Discrepancy detection: precision from human verdicts", "",
             "| Type | Real | False alarm | Precision |", "|---|---|---|---|"]
    for t, v in sorted(by_type.items()):
        n = v["real"] + v["false_alarm"]
        lines.append(f"| {t} | {v['real']} | {v['false_alarm']} | {v['real'] / n:.2f} |")
    lines.append(f"| **all** | {total_real} | {total - total_real} | {total_real / total:.2f} |")
    return {"by_type": by_type, "precision": round(total_real / total, 3)}, "\n".join(lines) + "\n"


def main():
    from teampulse.index import EventIndex

    parser = argparse.ArgumentParser()
    parser.add_argument("--key", help="answer key JSON with a 'discrepancies' list (e.g. data/demo_answer_key.json)")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args()
    conn = db.connect()
    if args.key:
        index = EventIndex(conn)
        _, report = run_with_key(conn, index, None if args.no_llm else LLM(conn), args.key)
    else:
        _, report = run_with_verdicts(conn)
    out = Path(config.DB_PATH).parent / "eval"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"discrepancies-{datetime.now():%Y%m%d-%H%M}.md"
    path.write_text(report, encoding="utf-8")
    print(report)
    print(f"Saved to {path}")


if __name__ == "__main__":
    main()
