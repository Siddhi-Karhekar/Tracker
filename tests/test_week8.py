"""Week 8 tests: contribution evidence and claim checks, consent + self-review, Contribution Agent, memory."""
import json
import os
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from teampulse import claims, contributions as contrib, db, memory
from teampulse.agents.contributions import ContributionAgent
from teampulse.agents.retrieval import RetrievalAgent
from teampulse.agents.supervisor import SupervisorAgent
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply
from teampulse.seed_demo import seed
from tests.test_tasks import FakeLLM

ROOT = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture()
def demo(tmp_path):
    path = str(tmp_path / "demo.db")
    seed(path)                                   # members, approvals and one memory included
    conn = db.connect(path)
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    claims.refresh(conn, index, FakeLLM(), scan_chat=True)   # AI links "80% done" to #18, report msg to the doc
    return conn, index, path


def member(conn, name):
    return next(m for m in contrib.members(conn) if m.name == name)


def test_reports_list_evidence_and_check_claims(demo):
    conn, _, _ = demo
    aman = contrib.build_report(conn, member(conn, "Aman"), 14)
    assert aman.counts["Code"] == 4 and aman.counts["Reviews & discussion"] == 2
    kabir = contrib.build_report(conn, member(conn, "Kabir"), 14)
    claim = next(c for c in kabir.claims if "80%" in c["text"])
    assert claim["task"].startswith("#18") and claim["verdict"].startswith("older evidence only")
    assert kabir.review["comment"].startswith("Most of my recommendation work")
    neha = contrib.build_report(conn, member(conn, "Neha"), 14)
    report_claim = next(c for c in neha.claims if "introduction" in c["text"])
    assert report_claim["verdict"] == "supported" and report_claim["evidence"][0]["label"] == "Notion edit"
    assert len(neha.items["Helping others"]) == 1 and "Kabir's blocker" in neha.items["Helping others"][0]["label"]
    text = contrib.to_markdown(kabir)
    assert "Kabir's note" in text and "older evidence only" in text and "false" not in text.lower()


def test_consent_and_self_review_gate(demo):
    conn, _, _ = demo
    riya = member(conn, "Riya")
    assert contrib.visible_to_team(conn, riya)
    contrib.set_review(conn, "Riya", "disputed", "My PR count is wrong")
    assert not contrib.visible_to_team(conn, riya)
    conn.execute("UPDATE members SET consent = 0 WHERE display_name = 'Aman'")
    conn.commit()
    result = ContributionAgent(conn, llm=FakeLLM()).ask("what did Riya and Aman do?")
    assert result["provider"] == "none"
    assert "Riya hasn't reviewed and approved" in result["answer"] and "Aman hasn't opted in" in result["answer"]


def test_contribution_agent(demo):
    conn, _, _ = demo

    class Summariser(FakeLLM):
        def chat(self, messages, json_mode=False, temperature=0.0):
            if messages[0]["content"].startswith("You summarise what members"):
                assert "Do NOT rank" in messages[0]["content"] and "Member's own note" in messages[1]["content"]
                return LLMReply("Kabir committed a baseline recommender [2] and reported a data blocker [5]. [99]", "fake")
            return super().chat(messages, json_mode, temperature)

    result = ContributionAgent(conn, llm=Summariser()).ask("what did Kabir do in the last 14 days?")
    assert result["cited"] == [2, 5] and "[99]" not in result["answer"]
    assert result["trace"][0]["members"] == ["Kabir"] and result["trace"][0]["days"] == 14

    class NoLLM:
        def chat(self, *a, **k):
            raise RuntimeError("quota")

    offline = ContributionAgent(conn, llm=NoLLM()).ask("contribution report")
    assert offline["provider"] == "template" and "Helping others" in offline["answer"] and offline["cited"]


def test_memory_store_replace_forget_verify(demo):
    conn, _, _ = demo
    seeded = memory.active(conn)
    assert len(seeded) == 1 and memory.verify(conn, seeded[0])["status"] == "confirmed"   # matches the plan page
    rec = memory.remember(conn, "remember that the final review is on 20 Dec", who="Riya")
    assert rec["kind"] == "deadline" and rec["replaced"] == [seeded[0]["fact"]]
    assert memory.verify(conn, {**rec, "fact_date": rec["date"]})["status"] == "differs"
    memory.remember(conn, "yaad rakhna ki payments is our top priority")
    assert {m["kind"] for m in memory.active(conn)} == {"deadline", "priority"}
    assert memory.relevant(conn, "when is the final review?")[0]["kind"] == "deadline"
    assert memory.forget(conn, "forget the priority") == ["payments is our top priority"]
    assert memory.parse_date("15/11", date(2026, 10, 1)) == "2026-11-15"
    assert memory.parse_date("Jan 3rd", date(2026, 10, 1)) == "2027-01-03"


def test_supervisor_memory_commands_and_recall(demo):
    conn, index, _ = demo

    class Router(FakeLLM):
        def chat(self, messages, json_mode=False, temperature=0.0):
            system = messages[0]["content"]
            if "supervisor" in system:
                return LLMReply(json.dumps({"standalone_question": "When is the final review?",
                                            "parts": [{"question": "When is the final review?", "specialist": "retrieval"}]}), "fake")
            if json_mode:
                return LLMReply(json.dumps({"search_queries": ["final review"]}), "fake")
            if system.startswith("Turn the user's request"):
                raise RuntimeError("use rules")
            return LLMReply("The plan lists the final review date [1].", "fake")

    llm = Router()
    agent = SupervisorAgent(conn, retrieval=RetrievalAgent(conn, index=index, llm=llm), llm=llm)
    assert {"contributions", "memory", "discrepancies", "blockers", "retrieval"} <= set(agent.specialists)
    before = len(llm.calls)
    stored = agent.ask("remember that our demo day is on 2 Dec")
    assert stored["route"]["parts"][0]["specialist"] == "memory" and "Remembered" in stored["answer"]
    assert not any("supervisor" in c for c in llm.calls[before:])                 # no router call for commands
    answer = agent.ask("how many days until the final review?")
    assert answer["memories"] and answer["memories"][0]["status"] == "confirmed"
    assert answer["trace"][-1]["step"] == "recall_memory"


def _page(path, monkeypatch, name):
    for k, v in {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "groq"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    import teampulse.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", path)
    page = AppTest.from_file(os.path.join(ROOT, "views", name), default_timeout=60)
    page.run()
    assert not page.exception
    return page


def test_contributions_page(demo, monkeypatch):
    conn, _, path = demo
    contrib.set_review(conn, "Riya", "disputed")
    page = _page(path, monkeypatch, "contributions.py")
    team_text = " ".join(m.value for m in page.tabs[1].markdown)
    assert "Riya's report is hidden" in team_text and "Kabir's note" in team_text
    page.selectbox[0].select("Riya").run()
    next(b for b in page.button if b.label.startswith("Approve")).click().run()
    assert contrib.get_review(conn, "Riya")["status"] == "approved"


def test_memory_page(demo, monkeypatch):
    conn, _, path = demo
    page = _page(path, monkeypatch, "memory.py")
    assert any("final review" in m.value.lower() for m in page.markdown)
    page.text_input[0].input("Payments is our top priority")
    page.button[0].click().run()
    assert not page.exception and len(memory.active(conn)) == 2
