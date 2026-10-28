"""Discrepancy detection: where GitHub, Notion and chat disagree about a task.

    python -m teampulse.discrepancies            # refresh claims, detect, print
    python -m teampulse.discrepancies --no-ai    # rules only

Detection is plain rules over the Week 6 claims (reliable, testable, free). The AI is used only for one
judgement rules can't make: does a decision in chat contradict what the plan page says? Without AI,
decisions made after the plan was last edited are flagged as "possibly outdated" (unverified, low).

Types and default severity:
  status        a tracker says done but GitHub is open and/or chat says it's broken     high / medium
  stale_plan    GitHub shows it done but Notion still says it isn't                      low
  owner         assigned people aren't the ones doing the work (commits, PRs, chat)       medium
                (or GitHub and Notion assign different people)                            low
  deadline      sources give different dates                                              medium (high if ≤3 days / passed)
  plan_outdated a chat decision contradicts the plan page                                 medium (low if unverified)

Each discrepancy is stored with a stable id (type + task + conflicting values). When the sources agree
again, it is marked resolved; people can dismiss false alarms, which then stay dismissed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
from datetime import date

from teampulse.claims import task_cards
from teampulse.llm import LLM, extract_json
from teampulse.models import utc_now

log = logging.getLogger("teampulse.discrepancies")

FINISHED = {"done", "dropped"}
NOT_DONE = {"open", "todo", "in_progress", "in_review", "blocked"}
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
WORKER_DAYS = 30
STATUS_TEXT = {"open": "open", "todo": "to do", "in_progress": "in progress", "in_review": "in review",
               "blocked": "blocked", "done": "done", "dropped": "dropped"}
SOURCE_NAME = {"github": "GitHub", "notion": "Notion", "chat": "chat"}

PLAN_PROMPT = """Below is a student team's project plan page and decisions the team made in chat AFTER the
page was last edited. For each decision, say whether it contradicts or changes something the plan states
(a different tool, scope, date, owner, approach). Ignore decisions about things the plan doesn't mention.
Return ONLY JSON: {"items": [{"decision": 1, "conflicts": true/false, "plan_says": "short paraphrase or null",
"decision_says": "short paraphrase"}]}"""


def _side(conn, claim: dict, label: str | None = None) -> dict:
    ev = conn.execute("SELECT url, author_name, title, body FROM events WHERE id = ?", (claim["event_id"],)).fetchone()
    return {"source": claim["source"], "value": claim["value"], "event_id": claim["event_id"],
            "observed_at": claim["observed_at"], "method": claim.get("method"), "url": ev["url"] if ev else None,
            "label": label or SOURCE_NAME.get(claim["source"], claim["source"])}


def _id(kind: str, task: str, values) -> str:
    return hashlib.sha1(json.dumps([kind, task, sorted(map(str, values))]).encode(), usedforsecurity=False).hexdigest()[:16]


def _days(iso: str) -> int:
    return (date.fromisoformat(iso[:10]) - date.today()).days


def _names(value: str) -> set[str]:
    return {n.strip() for n in (value or "").split(",") if n.strip()}


def _person_key(name: str) -> str:
    """Compare people by the first part of their name/login, so an unlinked GitHub login 'kabir-ml' still
    matches Notion's 'Kabir'. Linking accounts in the members table (cli link) makes this exact."""
    return re.split(r"[\s\-_.]+", name.strip().lstrip("@").lower())[0]


def _keys(names: set[str]) -> set[str]:
    return {_person_key(n) for n in names if n}


def detect_task(conn: sqlite3.Connection, card: dict) -> list[dict]:
    found = []
    name = (f"#{card['number']} " if card["number"] else "") + card["title"]
    st = card["status"]
    done = {s for s, c in st.items() if c["value"] in FINISHED}
    notdone = {s for s, c in st.items() if c["value"] in NOT_DONE}
    chat_blocked = st.get("chat", {}).get("value") == "blocked"

    def add(kind, severity, title, suggestion, sides, verified=True):
        found.append({"id": _id(kind, card["id"], [s["value"] for s in sides]), "task_id": card["id"], "task": name,
                      "type": kind, "severity": severity, "title": title, "suggestion": suggestion,
                      "sides": sides, "verified": verified})

    # ---- status ----
    if "notion" in done and notdone:
        reasons = []
        if st.get("github", {}).get("value") in NOT_DONE:
            reasons.append(f"the GitHub {'issue is still open' if st['github']['value'] == 'open' else 'shows ' + STATUS_TEXT[st['github']['value']]}")
        if st.get("chat", {}).get("value") in NOT_DONE:
            reasons.append(f"chat says it's {STATUS_TEXT[st['chat']['value']]}")
        sides = [_side(conn, st[s]) for s in ("notion", "github", "chat") if s in st]
        add("status", "high" if chat_blocked else "medium",
            f"{name}: marked done in Notion, but " + " and ".join(reasons),
            "Check whether it really works. If not, move it back in Notion; if it does, close the GitHub issue "
            "and reply in chat.", sides)
    elif "github" in done and "notion" in notdone:
        add("stale_plan", "low",
            f"{name}: GitHub shows it done, but Notion still says '{STATUS_TEXT[st['notion']['value']]}'",
            "Update the Notion task to Done.", [_side(conn, st["github"]), _side(conn, st["notion"])])
        if chat_blocked and (st["chat"]["observed_at"] or "") > (st["github"]["observed_at"] or ""):
            add("status", "medium", f"{name}: closed on GitHub, but chat reported a problem afterwards",
                "Reopen the issue if the problem is real.", [_side(conn, st["github"]), _side(conn, st["chat"])])
    elif "chat" in done and notdone:
        add("status", "low", f"{name}: chat says it's done, but the trackers don't",
            "If it's really finished, close the issue / mark it done in Notion.",
            [_side(conn, st[s]) for s in ("chat", "github", "notion") if s in st])
    elif "github" in done and chat_blocked and (st["chat"]["observed_at"] or "") > (st["github"]["observed_at"] or ""):
        add("status", "medium", f"{name}: closed on GitHub, but chat reported a problem afterwards",
            "Reopen the issue if the problem is real.", [_side(conn, st["github"]), _side(conn, st["chat"])])

    finished = bool(done) and not notdone

    # ---- owner ----
    owners = card["owner"]
    assigned_claims = [owners[s] for s in ("github", "notion") if s in owners]
    assigned = set().union(*(_names(c["value"]) for c in assigned_claims)) if assigned_claims else set()
    workers = conn.execute(
        "SELECT * FROM claims WHERE task_id = ? AND attribute = 'worker' AND observed_at >= date('now', ?) "
        "ORDER BY observed_at DESC", (card["id"], f"-{WORKER_DAYS} days")).fetchall()
    doer_claims = [dict(w) for w in workers]
    if "chat" in owners:
        doer_claims.insert(0, owners["chat"])
    doers = set().union(*(_names(c["value"]) for c in doer_claims)) if doer_claims else set()
    if assigned and doers and not (_keys(assigned) & _keys(doers)) and not finished:
        evidence = []
        for c in doer_claims:   # one piece of evidence per person doing the work, chat first
            if not any(e["value"] == c["value"] for e in evidence):
                evidence.append(_side(conn, c, "chat" if c["source"] == "chat" else f"{c['method'].replace('_', ' ')}"))
        add("owner", "medium",
            f"{name}: assigned to {', '.join(sorted(assigned))}, but {', '.join(sorted(doers))} is doing the work",
            "Reassign it in GitHub and Notion, or confirm in chat who is responsible.",
            [_side(conn, c) for c in assigned_claims] + evidence[:3])
    elif "github" in owners and "notion" in owners and \
            _keys(_names(owners["github"]["value"])) != _keys(_names(owners["notion"]["value"])):
        add("owner", "low", f"{name}: GitHub and Notion assign different people",
            "Make the assignees match.", [_side(conn, owners["github"]), _side(conn, owners["notion"])])

    # ---- deadline ----
    dl = card["deadline"]
    dates = {s: c["value"] for s, c in dl.items() if c["value"]}
    if len(set(dates.values())) > 1 and not finished:
        spread = abs((date.fromisoformat(max(dates.values())) - date.fromisoformat(min(dates.values()))).days)
        if spread >= 2:
            soonest = min(dates.values())
            urgent = _days(soonest) <= 3
            parts = ", ".join(f"{SOURCE_NAME[s]} {d}" for s, d in dates.items())
            add("deadline", "high" if urgent else "medium", f"{name}: different deadlines ({parts})",
                ("The earliest date is " + ("already past" if _days(soonest) < 0 else f"in {_days(soonest)} day(s)")
                 + ". Agree on one date and update the others."),
                [_side(conn, dl[s]) for s in dates])
    return found


def detect_plan(conn: sqlite3.Connection, llm: LLM | None) -> list[dict]:
    """Chat decisions made after a plan page was last edited, checked against that page."""
    found = []
    pages = conn.execute("SELECT * FROM events WHERE source='notion' AND kind='notion_page' AND status IS NULL").fetchall()
    for page in pages:
        decisions = conn.execute(
            "SELECT e.*, s.summary FROM signals s JOIN events e ON e.id = s.event_id "
            "WHERE s.type = 'decision' AND e.created_at > ? ORDER BY e.created_at", (page["updated_at"],)).fetchall()
        if not decisions:
            continue
        verdicts: dict[int, dict] = {}
        verified = False
        if llm is not None:
            listing = "\n".join(f"{i}. [{d['created_at'][:10]}, {d['author_name']}] {d['body']}"
                                for i, d in enumerate(decisions, 1))
            try:
                reply = llm.chat([{"role": "system", "content": PLAN_PROMPT},
                                  {"role": "user", "content": f"Plan page: {page['title']}\n{(page['body'] or '')[:4000]}"
                                                              f"\n\nDecisions:\n{listing}"}], json_mode=True)
                verdicts = {int(v["decision"]): v for v in extract_json(reply.text).get("items", [])
                            if isinstance(v, dict) and str(v.get("decision", "")).isdigit()}
                verified = True
            except Exception as exc:
                log.warning("Plan check failed (%s); flagging decisions as unverified", exc)
        plan_side = {"source": "notion", "value": f"plan last edited {page['updated_at'][:10]}", "event_id": page["id"],
                     "observed_at": page["updated_at"], "method": "plan_page", "url": page["url"], "label": "Notion plan"}
        for i, d in enumerate(decisions, 1):
            v = verdicts.get(i)
            if verified and not (v and v.get("conflicts")):
                continue
            says = (v or {}).get("decision_says") or d["summary"] or d["body"][:100]
            chat_side = {"source": "chat", "value": says, "event_id": d["id"], "observed_at": d["created_at"],
                         "method": "decision", "url": d["url"], "label": "chat decision"}
            if v and v.get("plan_says"):
                plan_side = {**plan_side, "value": v["plan_says"]}
            found.append({
                "id": _id("plan_outdated", page["id"], [d["id"]]), "task_id": None, "task": page["title"],
                "type": "plan_outdated", "severity": "medium" if verified else "low",
                "title": (f"Plan page is out of date: {says}" if verified else
                          f"Decision made after the plan was last edited (check the plan): {says}"),
                "suggestion": f"Update '{page['title']}' with this decision.",
                "sides": [plan_side, chat_side], "verified": verified})
    return found


def detect(conn: sqlite3.Connection, llm: LLM | None = None) -> list[dict]:
    found = []
    for card in task_cards(conn):
        found += detect_task(conn, card)
    found += detect_plan(conn, llm)
    found.sort(key=lambda d: (SEVERITY_RANK[d["severity"]], d["task"]))
    return found


def sync(conn: sqlite3.Connection, found: list[dict]) -> dict:
    """Store what was found: new → open, gone → resolved, dismissed stays dismissed."""
    now = utc_now()
    existing = {r["id"]: r for r in conn.execute("SELECT id, state FROM discrepancies")}
    seen = set()
    with conn:
        for d in found:
            seen.add(d["id"])
            if d["id"] in existing:
                state = existing[d["id"]]["state"]
                conn.execute("UPDATE discrepancies SET severity=?, title=?, suggestion=?, sides=?, verified=?, "
                             "last_seen=?, state=?, resolved_at=NULL WHERE id=?",
                             (d["severity"], d["title"], d["suggestion"], json.dumps(d["sides"], ensure_ascii=False),
                              int(d["verified"]), now, "dismissed" if state == "dismissed" else "open", d["id"]))
            else:
                conn.execute("INSERT INTO discrepancies (id, task_id, type, severity, title, suggestion, sides, verified, "
                             "state, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                             (d["id"], d["task_id"], d["type"], d["severity"], d["title"], d["suggestion"],
                              json.dumps(d["sides"], ensure_ascii=False), int(d["verified"]), now, now))
        gone = [i for i, r in existing.items() if i not in seen and r["state"] == "open"]
        conn.executemany("UPDATE discrepancies SET state='resolved', resolved_at=? WHERE id=?", [(now, i) for i in gone])
    return {"found": len(found), "new": len(seen - set(existing)), "resolved": len(gone)}


def run(conn: sqlite3.Connection, index=None, llm: LLM | None = None, refresh: bool = True) -> dict:
    from teampulse import claims

    stats = {}
    if refresh:
        stats["refresh"] = claims.refresh(conn, index, llm, scan_chat=True)
    stats.update(sync(conn, detect(conn, llm)))
    return stats


def open_items(conn: sqlite3.Connection, include: tuple = ("open",)) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM discrepancies WHERE state IN ({','.join('?' * len(include))})", include).fetchall()
    items = [{**dict(r), "sides": json.loads(r["sides"])} for r in rows]
    items.sort(key=lambda d: (SEVERITY_RANK[d["severity"]], d["first_seen"]))
    return items


def set_verdict(conn: sqlite3.Connection, discrepancy_id: str, verdict: str, who: str = "", note: str = "") -> None:
    """People's judgement. 'false_alarm' also dismisses it (it won't come back while the values are the same)."""
    if verdict not in ("real", "false_alarm"):   # a real check: asserts vanish under python -O
        raise ValueError(f"Unknown verdict: {verdict!r}")
    with conn:
        conn.execute("UPDATE discrepancies SET verdict=?, dismissed_by=?, note=?, "
                     "state=CASE WHEN ?='false_alarm' THEN 'dismissed' ELSE state END WHERE id=?",
                     (verdict, who or None, note or None, verdict, discrepancy_id))


def main():
    from teampulse import db
    from teampulse.index import EventIndex

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-ai", action="store_true")
    args = parser.parse_args()
    conn = db.connect()
    index = EventIndex(conn)
    print(run(conn, index, None if args.no_ai else LLM(conn)))
    for d in open_items(conn):
        print(f"[{d['severity']:6}] {d['type']:13} {d['title']}")
        for s in d["sides"]:
            print(f"           {s['label']:15} {s['value']}")


if __name__ == "__main__":
    main()
