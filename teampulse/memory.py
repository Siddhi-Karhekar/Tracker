"""Long-term team memory: only facts someone explicitly asks TeamPulse to remember.

    "remember that our final review is on Nov 15"      → stored
    "yaad rakhna ki payments is our top priority"      → stored
    "forget the review date"                           → deactivated
    "what do you remember?"                            → listed, each checked against live data

Policy (from the original pitch): store sparingly (explicit requests only, never inferred from chat),
newer facts about the same subject replace older ones, and a remembered fact is never trusted over live
data. Every time a memory is shown, dates in it are checked against Notion pages and GitHub milestones,
and the user is told if the live data now says something different.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date

from teampulse.llm import LLM, extract_json
from teampulse.models import utc_now

log = logging.getLogger("teampulse.memory")
REMEMBER_RE = re.compile(r"^\s*(please\s+)?(remember|note down|note that|keep in mind|yaad\s+rakh\w*)\b[\s:,\-]*"
                         r"(that\s+|ki\s+)?(?P<fact>.+)$", re.I | re.S)
FORGET_RE = re.compile(r"^\s*(please\s+)?(forget|bhool\s+ja\w*|delete the memory)\b[\s:,\-]*(that\s+|about\s+|ki\s+)?"
                       r"(?P<fact>.+)$", re.I | re.S)
RECALL_RE = re.compile(r"what (do|did) you remember|team memory|\bmemories\b|kya yaad hai|what have you remembered", re.I)
STOP = {"the", "our", "is", "on", "at", "a", "an", "of", "for", "to", "and", "that", "this", "we", "us", "hai", "ki",
        "ka", "ke", "me", "are", "be", "will", "it", "what", "when", "how", "kab", "kya", "do", "you"}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

EXTRACT_PROMPT = """Turn the user's request into one memory record. Today is {today}.
Return ONLY JSON: {{"fact": "the fact as one short sentence", "kind": "deadline" | "priority" | "preference" | "fact",
"subject": "2-4 word topic, e.g. 'final review' or 'project priority'", "date": "YYYY-MM-DD if the fact has a date, else null"}}"""


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOP}


def parse_date(text: str, today: date | None = None) -> str | None:
    """ISO dates, '15 Nov', 'Nov 15', '15/11' (day first, Indian style). Picks the next such date."""
    today = today or date.today()
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\b", text.lower()) or \
        re.search(r"\b([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", text.lower())
    if m:
        a, b = m.groups()
        day, mon = (a, b) if a.isdigit() else (b, a)
        if mon in MONTHS:
            d = date(today.year, MONTHS[mon], int(day))
            return (d if d >= today else d.replace(year=today.year + 1)).isoformat()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    if m:
        d = date(today.year, int(m.group(2)), int(m.group(1)))
        return (d if d >= today else d.replace(year=today.year + 1)).isoformat()
    return None


KINDS = ("deadline", "priority", "preference", "fact")


def _fallback(fact: str) -> dict:
    when = parse_date(fact)
    low = fact.lower()
    kind = "deadline" if when else "priority" if re.search(r"priority|important|focus|zaroori", low) else \
        "preference" if re.search(r"prefer|always|never|don't|do not", low) else "fact"
    words = [w for w in tokens(fact) if not w.isdigit() and w not in MONTHS]
    subject = " ".join(sorted(words, key=lambda w: fact.lower().find(w))[:3])
    return {"fact": fact.strip().rstrip("."), "kind": kind, "subject": subject, "date": when}


def remember(conn: sqlite3.Connection, text: str, llm: LLM | None = None, who: str = "") -> dict:
    m = REMEMBER_RE.match(text)
    fact = (m.group("fact") if m else text).strip()
    record = _fallback(fact)
    if llm is not None:
        try:
            reply = llm.chat([{"role": "system", "content": EXTRACT_PROMPT.format(today=date.today().isoformat())},
                              {"role": "user", "content": fact}], json_mode=True)
            data = extract_json(reply.text)
            # The AI only tidies the record; its values are checked like any other input.
            if isinstance(data.get("fact"), str) and 3 <= len(data["fact"].strip()) <= 300:
                record["fact"] = data["fact"].strip()
            if data.get("kind") in KINDS:
                record["kind"] = data["kind"]
            if isinstance(data.get("subject"), str) and data["subject"].strip():
                record["subject"] = data["subject"].strip()[:80]
            if data.get("date"):
                record["date"] = str(data["date"]) if re.match(r"^\d{4}-\d{2}-\d{2}$", str(data["date"])) \
                    else parse_date(str(data["date"]))
        except Exception as exc:   # no AI, or an unusable reply: the rule-based record is still correct
            log.warning("Memory extraction by AI failed (%s); using the rule-based record", type(exc).__name__)
    replaced = []
    with conn:
        for old in conn.execute("SELECT id, fact, subject FROM memory WHERE active = 1 AND kind = ?", (record["kind"],)):
            if old["subject"] and tokens(old["subject"]) & tokens(record["subject"]):   # newer fact on the same topic wins
                conn.execute("UPDATE memory SET active = 0 WHERE id = ?", (old["id"],))
                replaced.append(old["fact"])
        cur = conn.execute("INSERT INTO memory (fact, source, created_at, kind, subject, fact_date, active, created_by) "
                           "VALUES (?, 'user', ?, ?, ?, ?, 1, ?)",
                           (record["fact"], utc_now(), record["kind"], record["subject"], record["date"], who or None))
    return {**record, "id": cur.lastrowid, "replaced": replaced}


def forget(conn: sqlite3.Connection, text: str) -> list[str]:
    m = FORGET_RE.match(text)
    wanted = tokens(m.group("fact") if m else text)
    rows = conn.execute("SELECT id, fact, subject FROM memory WHERE active = 1").fetchall()
    scored = sorted(((len(wanted & tokens(f"{r['fact']} {r['subject']}")), r) for r in rows), key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return []
    best = scored[0][0]
    hits = [r for s, r in scored if s == best]
    with conn:
        conn.executemany("UPDATE memory SET active = 0 WHERE id = ?", [(r["id"],) for r in hits])
    return [r["fact"] for r in hits]


def active(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM memory WHERE active = 1 ORDER BY created_at DESC")]


def relevant(conn: sqlite3.Connection, text: str, k: int = 3) -> list[dict]:
    words = tokens(text)
    scored = [(len(words & tokens(f"{m['fact']} {m['subject']}")), m) for m in active(conn)]
    return [m for s, m in sorted(scored, key=lambda x: -x[0]) if s > 0][:k]


def verify(conn: sqlite3.Connection, mem: dict) -> dict:
    """Check a remembered date against live data. Returns {"status": confirmed|differs|unchecked, "note": ...}."""
    result = {"status": "unchecked", "note": "Nothing in GitHub or Notion to check this against."}
    if mem.get("fact_date"):
        words = tokens(mem.get("subject") or mem["fact"])
        found = []
        for e in conn.execute("SELECT id, source, title, body, url, extra FROM events WHERE source IN ('notion','github') "
                              "AND kind IN ('notion_page','issue')"):
            for line in f"{e['title'] or ''}\n{e['body'] or ''}".splitlines():
                if words and len(words & tokens(line)) >= max(1, min(2, len(words))):
                    for d in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", line):
                        found.append((d, e))
            extra = json.loads(e["extra"] or "{}")
            if extra.get("milestone") and extra.get("milestone_due") and words & tokens(extra["milestone"]):
                found.append((extra["milestone_due"][:10], e))
        if found:
            dates = {d for d, _ in found}
            src = next(e for d, e in found if d != mem["fact_date"]) if mem["fact_date"] not in dates else \
                next(e for d, e in found if d == mem["fact_date"])
            where = f"{'Notion' if src['source'] == 'notion' else 'GitHub'} “{src['title']}”"
            if mem["fact_date"] in dates:
                result = {"status": "confirmed", "note": f"Matches {where}.", "url": src["url"]}
            else:
                result = {"status": "differs", "url": src["url"],
                          "note": f"Live data now says {', '.join(sorted(dates))} ({where}). Check which is right."}
    with conn:
        conn.execute("UPDATE memory SET last_verified = ? WHERE id = ?", (utc_now(), mem["id"]))
    return result


class MemoryAgent:
    """Supervisor specialist: remember / forget / list. Commands are detected by the Supervisor directly."""
    name = "memory"
    description = ("Team memory: storing ('remember that…', 'yaad rakhna…'), forgetting, or listing facts the "
                   "team asked to remember, such as review dates or priorities.")

    def __init__(self, conn: sqlite3.Connection, llm: LLM | None = None):
        self.conn = conn
        self.llm = llm

    def ask(self, question: str, who: str = "") -> dict:
        if REMEMBER_RE.match(question):
            rec = remember(self.conn, question, self.llm, who)
            check = verify(self.conn, {**rec, "fact_date": rec["date"]})
            text = f"📌 Remembered: **{rec['fact']}**"
            if rec["replaced"]:
                text += f" (replaces: {'; '.join(rec['replaced'])})"
            if check["status"] != "unchecked":
                text += f"\n\n{'✅' if check['status'] == 'confirmed' else '⚠️'} {check['note']}"
            step = {"step": "remember", "ms": 0, **{k: rec[k] for k in ("kind", "subject", "date")}}
        elif FORGET_RE.match(question):
            gone = forget(self.conn, question)
            text = ("🗑️ Forgot: " + "; ".join(f"**{g}**" for g in gone)) if gone else "I couldn't find a matching memory."
            step = {"step": "forget", "ms": 0, "removed": len(gone)}
        else:
            mems = active(self.conn)
            if not mems:
                text = "Nothing remembered yet. Say “remember that …” to add a fact."
            else:
                lines = []
                for m in mems:
                    check = verify(self.conn, m)
                    flag = {"confirmed": " ✅", "differs": f" ⚠️ {check['note']}", "unchecked": ""}[check["status"]]
                    lines.append(f"- {m['fact']}{flag}")
                text = "Here's what the team asked me to remember:\n" + "\n".join(lines)
            step = {"step": "recall", "ms": 0, "memories": len(mems)}
        return {"answer": text, "evidence": [], "cited": [], "warnings": [], "provider": "memory", "trace": [step]}
