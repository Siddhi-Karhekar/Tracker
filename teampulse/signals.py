"""Chat signal scanner: labels every Discord message as blocker / decision / progress / help_request / noise,
and links it to a GitHub issue or Notion task.

    python -m teampulse.signals                 # scan new/edited messages (LLM, keyword fallback)
    python -m teampulse.signals --keyword       # rules only, no LLM (offline baseline)
    python -m teampulse.signals --rescan        # throw away old labels and redo everything

Pipeline (plain code except the one classification call per batch):
  1. messages → conversation windows (same channel, gaps < 45 min, ≤ 12 messages)
  2. windows packed into batches (≤ 30 messages) → one LLM call per batch, few-shot Hinglish prompt
  3. anything the LLM misses or mangles → keyword classifier
  4. task linking: exact #123 in the message → #123 elsewhere in the conversation → semantic match
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from teampulse import db
from teampulse.llm import LLM, extract_json
from teampulse.models import issue_refs, utc_now

log = logging.getLogger("teampulse.signals")

TYPES = ["blocker", "help_request", "decision", "progress", "noise"]
GAP = timedelta(minutes=45)
MAX_WINDOW = 12
BATCH_MESSAGES = 30


@dataclass
class Msg:
    id: str               # event id, e.g. "discord:9000123"
    author: str
    text: str
    created_at: datetime
    channel: str
    reply_to: str | None


def load_messages(conn: sqlite3.Connection, channels: set[str] | None = None) -> list[Msg]:
    sql = "SELECT id, author_name, body, created_at, channel, parent_id FROM events WHERE source='discord' AND kind='message'"
    args: list = []
    if channels is not None:
        if not channels:
            return []
        sql += f" AND channel IN ({','.join('?' * len(channels))})"
        args = sorted(channels)
    return [Msg(r[0], r[1] or "?", r[2] or "", datetime.fromisoformat(r[3]), r[4] or "?", r[5])
            for r in conn.execute(sql + " ORDER BY channel, created_at", args)]


def build_windows(messages: list[Msg]) -> list[list[Msg]]:
    """Split each channel's messages into conversations by time gaps and size."""
    windows: list[list[Msg]] = []
    current: list[Msg] = []
    for msg in sorted(messages, key=lambda m: (m.channel, m.created_at)):
        if current and (msg.channel != current[-1].channel or msg.created_at - current[-1].created_at > GAP
                        or len(current) >= MAX_WINDOW):
            windows.append(current)
            current = []
        current.append(msg)
    if current:
        windows.append(current)
    return windows


# ---- keyword classifier (offline fallback and evaluation baseline) ---------------------------

KW = {
    "noise_ack": re.compile(r"^\W*(ok+|okay|haan|ha+|han|thik|theek|cool|nice|lol|done|sahi|thanks|thx|ty)\b[\W\w]{0,12}$"),
    "help_request": re.compile(
        r"\b(please|plz|pls)\b|re-?review|\breview kar|help (kar|chahiye|needed)|"
        r"\b(koi|kisi|kaun|anyone|someone|can someone|can you|could you)\b.*\?|"
        r"(@\w+|\btu\b|\btum\b|\baap\b|\byou\b).*\?|"
        r"bata (do|de|na|sakta|sakti)|\bbhej(o|do|de)\b|kab (karenge|hoga|tak)\b"),
    "decision": re.compile(
        r"\b(final(ly)?|decided?|decision|agreed|tay hua)\b|use karenge|we('| wi)ll use|\bchalo\b.*\b(karte|karenge)\b"),
    "blocker": re.compile(
        r"nahi (chal|ho|aa|mil|ban|khul)\w* (raha|rahi|rahe)|abhi tak nahi|nahi mila|not (working|able|getting|loading)|"
        r"\b(stuck|blocked|blocker|error|exception|fail(ed|ing|s)?|crash(ed|ing)?|broken|bug|atak\w*|problem)\b|"
        r"can'?t\b|cannot\b|spam me"),
    "progress": re.compile(
        r"\b(done|kar diya|kar di|ho gaya|ho gayi|merged|completed|finish(ed)?|pushed|push kar|deploy(ed)?|added|"
        r"add kar|likh diya|bana diya|fixed|kar raha|kar rahi|dekh raha|dekh rahi|working on|almost|% done)\b"),
}
RESOLVED = re.compile(r"\b(fix(ed)?|ho gaya|ho gayi|chal gaya|aa gaya|mil gaya|resolved|sorted|kaam kar raha)\b")


def keyword_type(text: str) -> str:
    t = text.lower().strip()
    if not t or KW["noise_ack"].match(t):
        return "noise"
    for kind in ("help_request", "decision", "blocker", "progress"):
        if KW[kind].search(t):
            return kind
    return "noise"


class KeywordClassifier:
    name = "keyword"

    def classify(self, windows: list[list[Msg]], targets: set[str], lookup: dict[str, Msg] | None = None) -> dict:
        out = {}
        for window in windows:
            for i, msg in enumerate(window):
                if msg.id not in targets:
                    continue
                kind = keyword_type(msg.text)
                resolved = kind == "blocker" and any(
                    RESOLVED.search(later.text.lower()) for later in window[i + 1:]
                    if later.reply_to == msg.id or later.author != msg.author)
                out[msg.id] = {"type": kind, "summary": "" if kind == "noise" else msg.text[:140],
                               "task": "", "resolved": resolved, "classifier": self.name}
        return out


# ---- LLM classifier -------------------------------------------------------------------------------

CLASSIFY_PROMPT = """You label messages from a student project team's Discord chat. Messages are often
Hinglish (Hindi written in English letters, mixed with English).

Types:
- blocker: someone is stuck, something is broken / not working, or they are waiting on something they
  cannot get. e.g. "API nahi chal raha", "dataset abhi tak nahi mila", "build fail ho raha hai"
- help_request: asks a person or the team to do, check, review or answer something.
  e.g. "PR re-review kar de please", "koi bata sakta hai env kaha hai?", "deploy kab karenge?"
- decision: the team settles a choice. e.g. "final: Postgres use karenge", "chalo React Native hi lete hai"
- progress: work done or in progress, or who is doing what. e.g. "login UI push kar diya", "payment main dekh raha hu"
- noise: greetings, jokes, plain acknowledgements ("ok 👍", "haan"), off-topic chat, and social or
  timetable questions that aren't about the project ("lunch?", "kal class hai kya?", "kaun aa raha hai?").
  A question is only a help_request if it asks for something the PROJECT needs.

Rules:
- Label EVERY line that starts with an id like m3. Lines starting with "context" are only background.
- If a message reports progress AND asks for something, choose help_request.
- If a LATER message says a blocker was fixed, set "resolved": true on the blocker.
- "summary": one short English sentence (empty for noise). "task": the feature/task it is about in 2-5
  words, plus #number if mentioned, else "".

Example input:
Conversation 1 (#backend)
m1 [Riya] bhai signup pe 500 error aa raha hai #8
m2 [Aman, reply to m1] haan dekh raha hu
m3 [Aman] fixed, env var missing tha
m4 [Kabir] 😂

Example output:
{"labels": [
 {"id": "m1", "type": "blocker", "summary": "Signup returns a 500 error", "task": "signup #8", "resolved": true},
 {"id": "m2", "type": "progress", "summary": "Aman is looking into the signup error", "task": "signup #8", "resolved": false},
 {"id": "m3", "type": "progress", "summary": "Signup error fixed (missing env var)", "task": "signup #8", "resolved": false},
 {"id": "m4", "type": "noise", "summary": "", "task": "", "resolved": false}]}

Return ONLY JSON in that format."""


class LLMClassifier:
    def __init__(self, llm: LLM, fallback: KeywordClassifier | None = None):
        self.llm = llm
        self.fallback = fallback or KeywordClassifier()
        self.name = "llm"
        self.calls = 0

    @staticmethod
    def _batches(windows):
        batch, size = [], 0
        for window in windows:
            if batch and size + len(window) > BATCH_MESSAGES:
                yield batch
                batch, size = [], 0
            batch.append(window)
            size += len(window)
        if batch:
            yield batch

    @staticmethod
    def render(batch: list[list[Msg]], targets: set[str], lookup: dict[str, Msg]) -> tuple[str, dict[str, str]]:
        """Format a batch with short ids (m1, m2...) so the model can't mangle long ids."""
        short: dict[str, str] = {}
        lines = []
        for c, window in enumerate(batch, 1):
            lines.append(f"Conversation {c} (#{window[0].channel})")
            in_window = {m.id for m in window}
            first = window[0]
            if first.reply_to and first.reply_to not in in_window and first.reply_to in lookup:
                parent = lookup[first.reply_to]
                lines.append(f"context [{parent.author}] {parent.text}")
            for msg in window:
                when = msg.created_at.strftime("%b %d %H:%M")
                reply = ""
                if msg.reply_to and msg.reply_to in short:
                    reply = f", reply to {short[msg.reply_to]}"
                if msg.id in targets:
                    short[msg.id] = f"m{len(short) + 1}"
                    lines.append(f"{short[msg.id]} [{msg.author}, {when}{reply}] {msg.text}")
                else:
                    lines.append(f"context [{msg.author}, {when}{reply}] {msg.text}")
            lines.append("")
        return "\n".join(lines), {v: k for k, v in short.items()}

    def classify(self, windows: list[list[Msg]], targets: set[str], lookup: dict[str, Msg] | None = None) -> dict:
        lookup = lookup or {m.id: m for w in windows for m in w}
        out: dict[str, dict] = {}
        for batch in self._batches(windows):
            text, back = self.render(batch, targets, lookup)
            if not back:
                continue
            try:
                reply = self.llm.chat([{"role": "system", "content": CLASSIFY_PROMPT},
                                       {"role": "user", "content": text}], json_mode=True)
                self.calls += 1
                labels = extract_json(reply.text).get("labels", [])
                provider = reply.provider
            except Exception as exc:
                log.warning("LLM classification failed (%s); using keywords for this batch", exc)
                labels, provider = [], None
            for item in labels:
                if not isinstance(item, dict) or item.get("id") not in back:
                    continue
                kind = str(item.get("type", "")).strip().lower().replace(" ", "_")
                if kind not in TYPES:
                    continue
                out[back[item["id"]]] = {
                    "type": kind, "summary": str(item.get("summary") or "")[:300],
                    "task": str(item.get("task") or "")[:120], "resolved": bool(item.get("resolved")),
                    "classifier": f"llm:{provider}"}
            missing = set(back.values()) - set(out)
            if missing:   # anything the model skipped or labelled with a made-up type
                out.update(self.fallback.classify(batch, missing, lookup))
        return out


# ---- task linking ---------------------------------------------------------------------------------

class TaskLinker:
    def __init__(self, conn: sqlite3.Connection, index=None):
        self.conn = conn
        self.index = index

    def by_number(self, number: int) -> str | None:
        rows = self.conn.execute(
            "SELECT id, kind FROM events WHERE source='github' AND kind IN ('issue','pr') "
            "AND json_extract(extra, '$.number') = ?", (number,)).fetchall()
        rows.sort(key=lambda r: r["kind"] != "issue")   # prefer the issue over its PR
        if rows:
            return rows[0]["id"]
        row = self.conn.execute(   # a Notion task that mentions the number, e.g. "Login (#12)"
            "SELECT e.id FROM events e, json_each(e.extra, '$.issue_refs') r "
            "WHERE e.source='notion' AND r.value = ? AND e.status IS NOT NULL LIMIT 1", (number,)).fetchone()
        return row["id"] if row else None

    def _is_task(self, event_id: str) -> bool:
        row = self.conn.execute("SELECT source, kind, status FROM events WHERE id = ?", (event_id,)).fetchone()
        return bool(row) and (row["kind"] == "issue" or (row["source"] == "notion" and row["status"]))

    def link(self, msg: Msg, window: list[Msg], hint: str) -> tuple[str | None, str | None, float | None]:
        for number in issue_refs(msg.text, hint):
            task = self.by_number(number)
            if task:
                return task, "ref", 1.0
        window_refs = set(issue_refs(*(m.text for m in window)))
        if len(window_refs) == 1:
            task = self.by_number(window_refs.pop())
            if task:
                return task, "window_ref", 0.9
        if self.index is not None:
            query = hint or msg.text
            threshold = getattr(self.index.embedder, "link_threshold", 0.84)
            for hit in self.index.search(query, k=3, sources=["github", "notion"], kinds=["issue", "notion_page"]):
                if hit["score"] < threshold:
                    break
                if self._is_task(hit["event_id"]):
                    return hit["event_id"], "semantic", hit["score"]
        return None, None, None


# ---- scanning -----------------------------------------------------------------------------------

def pending_targets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.id, e.channel FROM events e LEFT JOIN signals s ON s.event_id = e.id "
        "WHERE e.source='discord' AND e.kind='message' "
        "AND (s.event_id IS NULL OR s.event_updated_at IS NOT e.updated_at)").fetchall()


def scan(conn: sqlite3.Connection, classifier=None, index=None, rescan: bool = False) -> dict:
    """Classify new or edited messages and store them in `signals`. Safe to run any time."""
    if rescan:
        with conn:
            conn.execute("DELETE FROM signals")
    targets_rows = pending_targets(conn)
    if not targets_rows:
        return {"classified": 0, "windows": 0, "llm_calls": 0, "linked": 0}
    classifier = classifier or LLMClassifier(LLM(conn))
    targets = {r["id"] for r in targets_rows}
    messages = load_messages(conn, {r["channel"] for r in targets_rows})
    lookup = {m.id: m for m in messages}
    windows = [w for w in build_windows(messages) if any(m.id in targets for m in w)]
    results = classifier.classify(windows, targets, lookup)

    linker = TaskLinker(conn, index)
    updated = {r[0]: r[1] for r in conn.execute(
        "SELECT id, updated_at FROM events WHERE source='discord'")}
    rows, linked = [], 0
    for window in windows:
        for msg in window:
            res = results.get(msg.id)
            if res is None:
                continue
            task_id = method = score = None
            if res["type"] != "noise":
                task_id, method, score = linker.link(msg, window, res.get("task", ""))
                linked += task_id is not None
            rows.append((msg.id, res["type"], res["summary"], res.get("task", ""), task_id, method, score,
                         int(res["resolved"]), res["classifier"], window[0].id, updated.get(msg.id), utc_now()))
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO signals (event_id, type, summary, task_hint, task_id, link_method, link_score, "
            "resolved, classifier, window_id, event_updated_at, classified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return {"classified": len(rows), "windows": len(windows),
            "llm_calls": getattr(classifier, "calls", 0), "linked": linked}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", action="store_true", help="rules only, no LLM")
    parser.add_argument("--rescan", action="store_true", help="re-classify every message")
    args = parser.parse_args()
    conn = db.connect()
    from teampulse.index import EventIndex

    index = EventIndex(conn)
    index.sync()
    classifier = KeywordClassifier() if args.keyword else LLMClassifier(LLM(conn))
    print(scan(conn, classifier, index, rescan=args.rescan))
    for row in conn.execute("SELECT type, COUNT(*) FROM signals GROUP BY type ORDER BY 2 DESC"):
        print(f"  {row[0]:13} {row[1]}")


if __name__ == "__main__":
    main()
