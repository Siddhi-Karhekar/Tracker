"""The seeded demo is only useful if its answer key is true. These tests check that.

They also act as the simplest possible rule-based reconciler for the structured sources,
which Week 7's Discrepancy Agent must beat on the Discord side.
"""
from __future__ import annotations

import csv
import json
from collections import Counter

import pytest

from seed import scenario as S
from teampulse import db
from teampulse.config import ROOT, load_team
from teampulse.ingest import discord_ingest, github_ingest, notion_ingest

DEMO = ROOT / "data" / "demo"
KEY = json.loads((DEMO / "answer_key.json").read_text(encoding="utf-8"))
LABELS = {"blocker", "help_request", "decision", "progress_update", "solution", "noise"}
OPEN_COMPATIBLE = {"open": {"todo", "in_progress", "open"}}


@pytest.fixture(scope="module")
def conn():
    team = load_team(DEMO / "team.toml")
    c = db.connect(":memory:")
    db.sync_members(c, team)
    github_ingest.load_offline(c, team, DEMO / "github")
    notion_ingest.load_offline(c, team, DEMO / "notion" / "tasks.json")
    discord_ingest.load_offline(c, team, DEMO / "discord")
    discord_ingest.link_hash_refs(c)
    yield c
    c.close()


def latest(conn, task, attribute, source, extractor_prefix="rule:"):
    """Latest structured claim value from one source (issue/page, not PR evidence)."""
    row = conn.execute(
        """SELECT value FROM claims WHERE task_id=? AND attribute=? AND source=? AND extractor=?
           ORDER BY timestamp DESC LIMIT 1""",
        (task, attribute, source, f"rule:{source}"),
    ).fetchone()
    return row[0] if row else None


def pr_owners(conn, task):
    return {r[0] for r in conn.execute(
        "SELECT value FROM claims WHERE task_id=? AND attribute='owner' AND extractor='rule:github-pr'", (task,))}


def compatible(a, b):
    if a is None or b is None or a == b:
        return True
    return b in OPEN_COMPATIBLE.get(a, ()) or a in OPEN_COMPATIBLE.get(b, ())


# ---------------------------------------------------------------- generated files are current
def test_build_is_up_to_date(tmp_path, monkeypatch):
    """data/demo must equal what seed/build.py produces now (re-run `python -m seed.build`)."""
    from seed import build

    monkeypatch.setattr(build, "OUT", tmp_path)
    build.main()
    for f in tmp_path.rglob("*"):
        if f.is_file():
            rel = f.relative_to(tmp_path)
            assert (DEMO / rel).read_bytes() == f.read_bytes(), f"{rel} is stale"


# ---------------------------------------------------------------- ingestion
def test_everything_ingested(conn):
    counts = dict(conn.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall())
    assert counts["message"] == len(S.MESSAGES)
    assert counts["issue"] == len(S.ISSUES)
    assert counts["pull_request"] == len(S.PULLS)
    assert counts["notion_page"] == len(S.NOTION_TASKS)
    tasks = {r[0] for r in conn.execute("SELECT task_id FROM tasks")}
    assert tasks == {f"T-{i['number']}" for i in S.ISSUES} | {KEY["task_ids"]["viva_preparation"]}


def test_every_answer_key_ref_exists(conn):
    refs = [s for d in KEY["discrepancies"] for s in d["sides"]]
    refs += [b["message"] for b in KEY["blockers"]] + [b["resolved_by"] for b in KEY["blockers"] if b["resolved_by"]]
    refs += [e["message"] for e in KEY["entity_links"]]
    refs += [c["message"] for c in KEY["chat_claims"]] + [e for c in KEY["chat_claims"] for e in c["evidence"]]
    refs += [r for q in KEY["eval_questions"] for r in q["refs"]]
    missing = [r["ref"] for r in refs if not conn.execute(
        "SELECT 1 FROM events WHERE event_id=?", (r["event_id"],)).fetchone()]
    assert not missing


def test_replies_point_at_stored_messages(conn):
    orphans = conn.execute(
        """SELECT e.event_id FROM events e LEFT JOIN events p ON p.event_id = e.parent_id
           WHERE e.parent_id IS NOT NULL AND p.event_id IS NULL""").fetchall()
    assert not orphans


def test_hash_refs_link_messages_to_tasks(conn):
    m027 = KEY["chat_claims"][-1]["message"]["event_id"]  # "...PR #18" → closes #10
    assert json.loads(conn.execute("SELECT task_ids FROM events WHERE event_id=?", (m027,)).fetchone()[0]) == ["T-10"]


# ---------------------------------------------------------------- planted discrepancies are real
@pytest.mark.parametrize("d", KEY["discrepancies"], ids=lambda d: d["id"])
def test_structured_sides_match_db(conn, d):
    for side in d["sides"]:
        if side["source"] == "discord":
            continue  # chat meaning is the Blocker Agent's job; existence checked above
        if side["ref"].startswith("github:pr:"):
            n = int(side["native_id"])
            if side["attribute"] == "owner":
                assert side["value"] in pr_owners(conn, d["task"])
            else:
                assert conn.execute("SELECT 1 FROM claims WHERE source_ref=? AND attribute=? AND value=?",
                                    (f"github:pull_request:{n}", side["attribute"], side["value"])).fetchone()
            continue
        got = latest(conn, d["task"], side["attribute"], side["source"])
        assert got == side["value"], f"{side['ref']} {side['attribute']}: db={got} key={side['value']}"


def test_each_discrepancy_has_two_disagreeing_sides():
    for d in KEY["discrepancies"]:
        values = {s["value"] for s in d["sides"]}
        assert len(values) >= 2, d["id"]
        assert len({s["source"] for s in d["sides"]}) >= 2, d["id"]


def test_all_four_conflict_types_planted():
    types = Counter(d["type"] for d in KEY["discrepancies"])
    assert set(types) == {"status_conflict", "owner_conflict", "deadline_conflict", "stale_plan"}
    assert all(n >= 2 for n in types.values())


# ---------------------------------------------------------------- nothing unplanned
def structured_conflicts(conn, task):
    found = set()
    gs, ns = latest(conn, task, "status", "github"), latest(conn, task, "status", "notion")
    if not compatible(gs, ns):
        found.add("status")
    gd, nd = latest(conn, task, "deadline", "github"), latest(conn, task, "deadline", "notion")
    if gd and nd and gd != nd:
        found.add("deadline")
    go, no = latest(conn, task, "owner", "github"), latest(conn, task, "owner", "notion")
    builders = pr_owners(conn, task)
    if (go and no and go != no) or (builders and (go or no) not in builders):
        found.add("owner")
    return found


def test_consistent_tasks_have_no_structured_conflict(conn):
    for t in KEY["consistent_tasks"]:
        assert not structured_conflicts(conn, t), t


def test_structured_conflicts_are_exactly_the_planted_ones(conn):
    def structured_disagree(d):
        vals = [s["value"] for s in d["sides"] if s["source"] != "discord"]
        return any(not compatible(a, b) for a in vals for b in vals)

    # D5, D6 and D8 are only visible once Discord is understood: that's the point of the project.
    planted = {(d["task"], d["sides"][0]["attribute"]) for d in KEY["discrepancies"] if structured_disagree(d)}
    assert {d["id"] for d in KEY["discrepancies"] if not structured_disagree(d)} == {"D5", "D6", "D8"}
    detected = {(t, a) for (t,) in conn.execute("SELECT task_id FROM tasks") for a in structured_conflicts(conn, t)}
    assert detected == planted


# ---------------------------------------------------------------- labels and blockers
def test_labels_csv():
    rows = list(csv.DictReader(open(DEMO / "labels" / "discord_messages.csv", encoding="utf-8")))
    assert len(rows) == len(S.MESSAGES)
    assert {r["label"] for r in rows} <= LABELS
    assert Counter(r["label"] for r in rows)["blocker"] >= 5


def test_unresolved_blockers():
    unresolved = [b for b in KEY["blockers"] if not b["resolved_by"]]
    # m025: first login-crash report, only "dekhta hu" in reply. m007/m028: review requests,
    # answered by doing the review rather than by a chat solution.
    assert {b["message"]["seed_key"] for b in unresolved} == {"m007", "m025", "m028"}


def test_some_messages_mix_scripts():
    assert any(any("ऀ" <= ch <= "ॿ" for ch in m["text"]) for m in S.MESSAGES)


# ---------------------------------------------------------------- privacy
def test_optout_skips_and_purges():
    team = load_team(DEMO / "team.toml")
    c = db.connect(":memory:")
    db.sync_members(c, team)
    discord_ingest.load_offline(c, team, DEMO / "discord")
    before = c.execute("SELECT COUNT(*) FROM events WHERE author='rohan'").fetchone()[0]
    assert before > 0
    assert discord_ingest.purge_member(c, "rohan") == before
    c.execute("INSERT INTO optouts VALUES ('rohan', '2026-10-26T00:00:00Z')")
    discord_ingest.load_offline(c, team, DEMO / "discord")  # re-ingest must not bring them back
    assert c.execute("SELECT COUNT(*) FROM events WHERE author='rohan'").fetchone()[0] == 0
