"""Retrieval Agent tests on the demo data, with a fake LLM and the offline hash embedder."""
import json

import pytest

from teampulse import db
from teampulse.agents.retrieval import RetrievalAgent, format_evidence
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply, extract_json
from teampulse.seed_demo import build


class FakeLLM:
    def __init__(self, plan: dict, answer: str):
        self.plan, self.answer_text, self.calls = plan, answer, []

    def chat(self, messages, json_mode=False, temperature=0.0):
        self.calls.append(messages)
        return LLMReply(json.dumps(self.plan) if json_mode else self.answer_text, "fake")


@pytest.fixture()
def demo(tmp_path):
    conn = db.connect(str(tmp_path / "demo.db"))
    events, key = build()
    db.upsert_events(conn, events)
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    index.sync()
    return conn, index, key


def test_index_is_incremental(demo):
    conn, index, _ = demo
    assert index.sync()["embedded"] == 0
    conn.execute("UPDATE events SET body = 'changed', updated_at = '2030-01-01T00:00:00+00:00' WHERE id = 'notion:task-menu'")
    conn.commit()
    assert index.sync()["embedded"] == 1
    conn.execute("DELETE FROM events WHERE id = 'notion:task-menu'"); conn.commit()
    assert index.sync()["removed"] == 1


def test_agent_finds_hinglish_blocker_and_strips_bad_citations(demo):
    conn, index, key = demo
    llm = FakeLLM({"search_queries": ["login OTP spam", "OTP nahi aa raha"], "sources": [],
                   "since_days": None, "wants_recent_activity": False},
                  "Login is blocked: OTP mails go to spam [1][99].")
    result = RetrievalAgent(conn, index=index, llm=llm).ask("Why is login stuck?")
    ids = [e["event_id"] for e in result["evidence"]]
    assert key["blockers"][0]["event"] in ids           # the Hinglish Discord message was retrieved
    assert "[99]" not in result["answer"] and result["cited"] == [1]
    assert any("non-existent" in w for w in result["warnings"])
    assert [t["step"] for t in result["trace"]] == ["plan", "retrieve", "answer", "check_citations"]
    assert "Evidence:" in llm.calls[1][1]["content"]


def test_time_window_pulls_recent_activity(demo):
    conn, index, _ = demo
    llm = FakeLLM({"search_queries": ["completed work"], "sources": ["github"], "since_days": 4,
                   "wants_recent_activity": True}, "Menu API merged [1].")
    result = RetrievalAgent(conn, index=index, llm=llm).ask("What did we finish this week?")
    assert result["evidence"] and all(e["source"] == "github" for e in result["evidence"])
    assert "github:campuseats/app#15" in [e["event_id"] for e in result["evidence"]]
    assert "github:commit:e90f1d3" not in [e["event_id"] for e in result["evidence"]]   # 9 days old


def test_planner_failure_falls_back(demo):
    conn, index, _ = demo

    class Broken(FakeLLM):
        def chat(self, messages, json_mode=False, temperature=0.0):
            if json_mode:
                raise RuntimeError("rate limited")
            return LLMReply("ok [1]", "fake")

    result = RetrievalAgent(conn, index=index, llm=Broken({}, "")).ask("payment status?")
    assert result["plan"]["search_queries"] == ["payment status?"] and result["evidence"]


def test_helpers():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    text = format_evidence([{"source": "notion", "kind": "notion_page", "date": "2026-10-20 10:00", "author": "Neha",
                             "channel": "Tasks", "status": "Done", "assignees": ["Aman"], "due": "2026-10-25",
                             "parent": None, "text": "Login"}])
    assert text.startswith("[1] (notion notion_page; 2026-10-20 10:00; by Neha") and "due=2026-10-25" in text
