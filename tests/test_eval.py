"""Week 9 tests: Verifier, plain-RAG baseline, keyword routing fallback, QA scoring and the full comparison."""
import json
import re

import pytest

from teampulse import verifier
from teampulse.agents.supervisor import keyword_route
from teampulse.eval import qa
from teampulse.llm import LLM, LLMReply
from tests.test_discrepancies import PlanAwareLLM


class ExtractiveLLM(PlanAwareLLM):
    """A stand-in model: routes by keywords, plans with the raw question, and answers by quoting evidence."""

    def chat(self, messages, json_mode=False, temperature=0.0):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "supervisor" in system:
            q = user.split("New question:")[-1].strip()
            parts = [p.strip() + "?" for p in q.rstrip("?").split(", and ")] if ", and " in q else [q]
            names = re.findall(r"^- (\w+):", system, re.M)
            return LLMReply(json.dumps({"standalone_question": q, "parts": [
                {"question": p, "specialist": keyword_route(p, names)} for p in parts]}), "fake")
        if "plan searches" in system:
            week = "week" in user.lower()
            return LLMReply(json.dumps({"search_queries": [user], "sources": [], "since_days": 7 if week else None,
                                        "wants_recent_activity": week}), "fake")
        if system.startswith(("You check an AI", "Turn the user's request")):
            raise RuntimeError("not simulated")
        if system.startswith("Combine the partial answers"):
            return LLMReply(user.split("Partial answers:")[1].split("User's question:")[0], "fake")
        if "Evidence:" in user and not json_mode:
            blocks = re.findall(r"^\[(\d+)\] \([^\n]*\)[^\n]*\n(.*)$", user, re.M)
            return LLMReply("\n".join(f"- {text[:150]} [{n}]" for n, text in blocks[:12]), "fake")
        return super().chat(messages, json_mode, temperature)


def test_statements_and_lexical_verifier():
    answer = "- **Login** is blocked: OTP mails go to spam [1].\n- Payments are done [1]. Menu merged [2].\nAll good overall here."
    evidence = [{"text": "bhai login OTP abhi bhi nahi aa raha, spam me ja raha hai"}, {"text": "Menu API merged"}]
    parts = verifier.statements(answer)
    assert [p["cites"] for p in parts] == [[1], [1], [2], []] and parts[0]["text"] == "Login is blocked: OTP mails go to spam."
    result = verifier.verify(answer, evidence)
    assert [s["verdict"] for s in result["statements"]] == ["supported", "unsupported", "supported"]
    assert result["uncited"] == 1 and result["method"] == "lexical" and result["faithfulness"] == round(2 / 3, 3)


def test_ai_verifier_and_fallback():
    class Judge:
        def chat(self, messages, json_mode=False, temperature=0.0):
            assert "Statement 2:" in messages[1]["content"]
            return LLMReply(json.dumps({"items": [{"statement": 1, "verdict": "supported"},
                                                  {"statement": 2, "verdict": "partial"}]}), "fake")

    result = verifier.verify("Login broken [1]. Payments late [1].", [{"text": "login broken"}], Judge())
    assert result["method"] == "llm" and result["faithfulness"] == 0.75
    assert verifier.verify("No citations here at all.", [], Judge())["faithfulness"] is None


def test_scoring():
    q = {"must_mention": [["otp", "login"], ["{date:+3}"]], "must_cite": [["discord:1"], ["x", "y"]],
         "must_not": ["is false"], "specialists": ["blockers"], "min_parts": 1}
    from datetime import date, timedelta
    d = date.today() + timedelta(days=3)
    result = {"answer": f"Login OTP fails [1]; review on {d.day} {d:%B} [2].", "evidence": [{"event_id": "discord:1"},
              {"event_id": "z"}], "cited": [1, 2], "route": {"parts": [{"specialist": "blockers"}]}}
    s = qa.score(q, result)
    assert s["passed"] and s["correctness"] == 1.0 and s["citation_recall"] == 0.5 and s["routed_ok"]
    assert not qa.score({**q, "must_mention": [["pr"]]}, {"answer": "provider error", "provider": "error"})["passed"]
    assert not qa.score({**q, "must_mention": [["login"]]}, {**result, "answer": "Login: claim is false"})["passed"]


def test_llm_stats_and_reset():
    llm = LLM(None, providers=["off"])
    with pytest.raises(Exception):
        llm.chat([{"role": "user", "content": "hi"}])
    assert llm.reset_stats()["calls"] == 0 and llm.stats["failed"] == 0


def test_full_comparison_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAMPULSE_EMBEDDER", "hash")
    questions = qa.load_questions(only=["q01", "q04", "q09", "q12", "q15", "q17"])
    result = qa.run(questions, qa.SYSTEMS, make_llm=ExtractiveLLM, judge=None, db_path=tmp_path / "qa.db",
                    setup_llm=ExtractiveLLM())
    s = result["summary"]
    assert set(s) == set(qa.SYSTEMS) and len(result["rows"]) == 18 and all(not r["error"] for r in result["rows"])
    assert s["multi-agent"]["routing"] == 1.0                                  # incl. the 2-part question
    assert s["multi-agent"]["pass_rate"] >= s["plain RAG"]["pass_rate"]
    multi = {r["id"]: r for r in result["rows"] if r["system"] == "multi-agent"}
    assert multi["q01"]["passed"] and multi["q15"]["specialists"] == ["retrieval", "discrepancies"]
    report = qa.to_markdown(result)
    assert "| multi-agent |" in report and "Pass rate by question type" in report
