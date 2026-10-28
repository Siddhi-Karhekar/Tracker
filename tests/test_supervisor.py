"""Supervisor tests: routing, follow-up rewriting, multi-part merge with renumbered citations, fallbacks."""
import json

import pytest

from teampulse import db
from teampulse.agents.retrieval import RetrievalAgent
from teampulse.agents.supervisor import Specialist, SupervisorAgent, merge_results
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply
from teampulse.seed_demo import build


class ScriptedLLM:
    """Replies by prompt type. `route` is the router's JSON; answers cite [1] by default."""

    def __init__(self, route: dict, synth: str = "Combined: [1] and [2]."):
        self.route, self.synth, self.calls = route, synth, []

    def chat(self, messages, json_mode=False, temperature=0.0):
        system = messages[0]["content"]
        self.calls.append(("router" if "supervisor" in system else "planner" if "plan searches" in system
                           else "verify" if "check an AI assistant" in system
                           else "synth" if "partial answers" in system.lower() else "answer", messages))
        if "check an AI assistant" in system:
            return LLMReply(json.dumps({"items": [{"statement": 1, "verdict": "supported"}]}), "fake")
        if "supervisor" in system:
            return LLMReply(json.dumps(self.route), "fake")
        if json_mode:   # retrieval planner: search the question as-is
            question = messages[1]["content"]
            return LLMReply(json.dumps({"search_queries": [question], "sources": []}), "fake")
        if "partial answers" in system.lower():
            return LLMReply(self.synth, "fake")
        return LLMReply("Answer from evidence [1].", "fake")


@pytest.fixture()
def demo(tmp_path):
    conn = db.connect(str(tmp_path / "demo.db"))
    db.upsert_events(conn, build()[0])
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    index.sync()
    return conn, index


def make(conn, index, llm):
    return SupervisorAgent(conn, retrieval=RetrievalAgent(conn, index=index, llm=llm), llm=llm)


def test_single_part_passes_through_without_extra_llm_call(demo):
    llm = ScriptedLLM({"standalone_question": "Why is login stuck?",
                       "parts": [{"question": "Why is login stuck?", "specialist": "retrieval"}]})
    result = make(*demo, llm).ask("Why is login stuck?")
    assert [kind for kind, _ in llm.calls] == ["router", "planner", "answer", "verify"]   # no synthesis call
    assert result["cited"] == [1] and result["evidence"]
    assert [t["step"] for t in result["trace"]] == ["route", "specialist:retrieval", "verify"]
    assert result["verification"]["faithfulness"] == 1.0 and result["verification"]["method"] == "llm"


def test_follow_up_is_rewritten_and_history_is_sent(demo):
    llm = ScriptedLLM({"standalone_question": "Who owns the payments task?",
                       "parts": [{"question": "Who owns the payments task?", "specialist": "retrieval"}]})
    history = [{"question": "What's the status of payments?", "answer": "In progress [2]."}]
    result = make(*demo, llm).ask("and who owns it?", history=history)
    router_input = llm.calls[0][1][1]["content"]
    assert "status of payments" in router_input and "[2]" not in router_input   # citations stripped
    assert result["route"]["standalone_question"] == "Who owns the payments task?"
    assert llm.calls[1][1][1]["content"] == "Who owns the payments task?"


def test_multi_part_merges_and_renumbers(demo):
    llm = ScriptedLLM({"standalone_question": "q", "parts": [
        {"question": "What happened with the menu API?", "specialist": "retrieval"},
        {"question": "Who is doing payments?", "specialist": "not-a-real-agent"}]},
        synth="Menu API done [1]. Payments by Aman [2]. Bogus [77].")
    result = make(*demo, llm).ask("Menu API status and who is doing payments?")
    assert [p["specialist"] for p in result["route"]["parts"]] == ["retrieval", "retrieval"]  # unknown → retrieval
    assert [kind for kind, _ in llm.calls][-2:] == ["synth", "verify"]
    assert result["cited"] == [1, 2] and "[77]" not in result["answer"]
    ids = [e["event_id"] for e in result["evidence"]]
    assert len(ids) == len(set(ids))   # merged evidence is de-duplicated
    assert [t["step"] for t in result["trace"]][-2:] == ["synthesize", "verify"]


def test_merge_results_renumbers():
    a = {"event_id": "a"}; b = {"event_id": "b"}; c = {"event_id": "c"}
    results = [{"question": "q1", "result": {"answer": "x [2]", "evidence": [a, b], "cited": [2]}},
               {"question": "q2", "result": {"answer": "y [1] [2] [9]", "evidence": [b, c], "cited": [1, 2]}}]
    merged, answers, shown = merge_results(results)
    assert [e["event_id"] for e in merged] == ["b", "c", "a"] and shown == 2
    assert answers == ["x [1]", "y [1] [2] "]


def test_router_failure_and_new_specialist(demo):
    conn, index = demo

    class NoRouter(ScriptedLLM):
        def chat(self, messages, json_mode=False, temperature=0.0):
            if "supervisor" in messages[0]["content"]:
                raise RuntimeError("rate limited")
            return super().chat(messages, json_mode, temperature)

    agent = make(conn, index, NoRouter({}))
    agent.register(Specialist("blockers", "Finds blockers", lambda q: {"answer": "none", "evidence": []}))
    result = agent.ask("payment status?")
    assert result["route"]["reason"] == "fallback" and result["evidence"]
    assert "blockers" in agent.specialists
