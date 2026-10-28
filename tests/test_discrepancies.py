"""Week 7 tests: detection rules, lifecycle (resolve / dismiss), AI plan check, agent, evaluation, page."""
import json
import os
from datetime import datetime, timezone

import pytest
from streamlit.testing.v1 import AppTest

from teampulse import claims, db
from teampulse import discrepancies as disc
from teampulse.agents.discrepancy import DiscrepancyAgent, task_filter, wanted
from teampulse.agents.retrieval import RetrievalAgent
from teampulse.agents.supervisor import SupervisorAgent
from teampulse.eval import discrepancies as evaluation
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply
from teampulse.seed_demo import REPO, build
from tests.test_tasks import FakeLLM


class PlanAwareLLM(FakeLLM):
    """FakeLLM plus the plan-page check (says the Postgres decision contradicts the plan)."""

    def __init__(self, conflicts=True):
        super().__init__()
        self.conflicts = conflicts

    def chat(self, messages, json_mode=False, temperature=0.0):
        system = messages[0]["content"]
        if system.startswith("Below is a student team's project plan"):
            return LLMReply(json.dumps({"items": [{"decision": 1, "conflicts": self.conflicts,
                                                   "plan_says": "Database: Firebase",
                                                   "decision_says": "Use Postgres instead of Firebase"}]}), "fake")
        if system.startswith("You explain where"):
            return LLMReply("Login is done in Notion [1] but open on GitHub [2]. Bogus [42].", "fake")
        return super().chat(messages, json_mode, temperature)


@pytest.fixture()
def demo(tmp_path):
    conn = db.connect(str(tmp_path / "demo.db"))
    events, key = build()
    db.upsert_events(conn, events)
    conn.executemany("INSERT INTO members (display_name, discord_id, github_login, notion_user_id, consent) VALUES (?, ?, ?, ?, 1)",
                     [("Riya", "1001", "riya-dev", "n-riya"), ("Aman", "1002", "aman-codes", "n-aman"),
                      ("Kabir", "1003", "kabir-ml", "n-kabir"), ("Neha", "1004", "neha-pm", "n-neha")])
    conn.commit()
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    index.sync()
    return conn, index, key


def by_type(found):
    return {(d["task_id"], d["type"]): d for d in found}


def test_rules_find_all_planted_conflicts(demo):
    conn, index, _ = demo
    claims.refresh(conn, index, None, scan_chat=True)
    found = by_type(disc.detect(conn, None))
    assert set(found) == {(f"task:{REPO}#12", "status"), (f"task:{REPO}#14", "stale_plan"),
                          (f"task:{REPO}#16", "owner"), (f"task:{REPO}#16", "deadline"), (None, "plan_outdated")}
    assert found[(f"task:{REPO}#12", "status")]["severity"] == "high"          # chat says blocked
    assert found[(f"task:{REPO}#16", "deadline")]["severity"] == "high"        # Notion date is 2 days away
    assert found[(None, "plan_outdated")]["verified"] is False and found[(None, "plan_outdated")]["severity"] == "low"
    sides = [s["source"] for s in found[(f"task:{REPO}#12", "status")]["sides"]]
    assert sides == ["notion", "github", "chat"]


def test_lifecycle_resolve_and_dismiss(demo):
    conn, index, _ = demo
    assert disc.run(conn, index, None)["new"] == 5
    assert disc.run(conn, index, None, refresh=False)["new"] == 0                # same findings, same ids
    owner = next(d for d in disc.open_items(conn) if d["type"] == "owner")
    disc.set_verdict(conn, owner["id"], "false_alarm", "tester")
    # Fix Notion: login back to "In progress" → the login status conflict resolves on the next check.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("UPDATE events SET status='In progress', updated_at=? WHERE id='notion:task-login'", (now,))
    conn.commit()
    stats = disc.run(conn, index, None)
    assert stats["resolved"] == 1
    states = {(d["task_id"], d["type"]): d["state"] for d in disc.open_items(conn, ("open", "dismissed", "resolved"))}
    assert states[(f"task:{REPO}#12", "status")] == "resolved"
    assert states[(f"task:{REPO}#16", "owner")] == "dismissed"                  # false alarms stay dismissed
    assert len(disc.open_items(conn)) == 3


def test_ai_plan_check(demo):
    conn, index, _ = demo
    claims.refresh(conn, index, None, scan_chat=True)
    plan = by_type(disc.detect(conn, PlanAwareLLM(conflicts=True)))[(None, "plan_outdated")]
    assert plan["verified"] and plan["severity"] == "medium" and plan["sides"][0]["value"] == "Database: Firebase"
    assert (None, "plan_outdated") not in by_type(disc.detect(conn, PlanAwareLLM(conflicts=False)))


def test_question_understanding(demo):
    conn, index, _ = demo
    claims.refresh(conn, index, None)
    assert "owner" in wanted("who is really doing payments?")
    assert {"stale_plan", "status", "plan_outdated"} <= wanted("is our Notion up to date?")
    assert wanted("kya sab sync me hai?") is None
    assert task_filter(conn, "who is really doing payments?") == {f"task:{REPO}#16"}
    assert task_filter(conn, "what about #14?") == {f"task:{REPO}#14"}


def test_agent_offline_and_with_llm(demo):
    conn, index, _ = demo

    class NoLLM:
        def chat(self, *a, **k):
            raise RuntimeError("quota")

    result = DiscrepancyAgent(conn, llm=NoLLM(), index=index).ask("who is really doing payments?")
    assert "Aman is doing the work" in result["answer"] and result["provider"] == "template"
    assert result["cited"] and all(1 <= n <= len(result["evidence"]) for n in result["cited"])
    result = DiscrepancyAgent(conn, llm=PlanAwareLLM(), index=index).ask("where do our sources disagree?")
    assert result["cited"] == [1, 2] and "[42]" not in result["answer"]


def test_supervisor_routes_to_discrepancies(demo):
    conn, index, _ = demo

    class Router(PlanAwareLLM):
        def chat(self, messages, json_mode=False, temperature=0.0):
            if "supervisor" in messages[0]["content"]:
                assert "- discrepancies:" in messages[0]["content"]
                return LLMReply(json.dumps({"standalone_question": "q", "parts": [
                    {"question": "is notion up to date?", "specialist": "discrepancies"}]}), "fake")
            return super().chat(messages, json_mode, temperature)

    llm = Router()
    agent = SupervisorAgent(conn, retrieval=RetrievalAgent(conn, index=index, llm=llm), llm=llm)
    result = agent.ask("kya notion updated hai?")
    assert result["trace"][1]["step"] == "specialist:discrepancies" and result["evidence"]


def test_evaluation(demo, tmp_path):
    conn, index, key = demo
    path = tmp_path / "key.json"
    path.write_text(json.dumps(key))
    results, report = evaluation.run_with_key(conn, index, PlanAwareLLM(), str(path))
    assert results["rules only"]["recall"] == 1.0 and results["rules + AI"]["recall"] == 1.0
    assert results["rules + AI"]["precision"] == 1.0 and "| rules + AI |" in report
    assert conn.execute("SELECT COUNT(*) FROM discrepancies").fetchone()[0] == 0     # ran on copies only
    disc.run(conn, index, None)
    items = disc.open_items(conn)
    disc.set_verdict(conn, items[0]["id"], "real")
    disc.set_verdict(conn, items[1]["id"], "false_alarm")
    summary, _ = evaluation.run_with_verdicts(conn)
    assert summary["precision"] == 0.5


def test_discrepancies_page(tmp_path, monkeypatch):
    path = str(tmp_path / "demo.db")
    db.upsert_events(db.connect(path), build()[0])
    for k, v in {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "groq"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    import teampulse.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", path)
    page = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "views", "discrepancies.py"),
                             default_timeout=60)
    page.run()
    assert not page.exception
    text = lambda: " ".join(m.value for m in page.markdown)
    assert "2 serious, 1 moderate, 2 minor." in text()
    next(b for b in page.button if b.label == "False alarm").click().run()
    assert not page.exception and "1 serious, 1 moderate, 2 minor." in text()
