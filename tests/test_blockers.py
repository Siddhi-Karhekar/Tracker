"""Week 5 tests: windowing, classifiers, incremental scan, task linking, Blocker Agent, evaluation."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from teampulse import db, signals
from teampulse.agents.blockers import BlockerAgent, wanted_types, window_days
from teampulse.agents.supervisor import SupervisorAgent
from teampulse.agents.retrieval import RetrievalAgent
from teampulse.eval import blockers as evaluation
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply
from teampulse.seed_demo import build

T0 = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def msg(i, channel="dev", minutes=0, text="hi", reply=None, author="A"):
    return signals.Msg(f"discord:{i}", author, text, T0 + timedelta(minutes=minutes), channel,
                       f"discord:{reply}" if reply else None)


class FakeClassifierLLM:
    """Labels each m<N> line with the keyword rules, plus one bogus id and one invalid type."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, json_mode=False, temperature=0.0):
        self.calls += 1
        labels = [{"id": "m999", "type": "blocker"}]
        for i, (short, text) in enumerate(re.findall(r"^(m\d+) \[[^\]]*\] (.*)$", messages[1]["content"], re.M)):
            kind = "made_up_type" if i == 0 else signals.keyword_type(text)
            labels.append({"id": short, "type": kind, "summary": f"S:{text[:20]}", "task": "", "resolved": False})
        return LLMReply(json.dumps({"labels": labels}), "fake")


@pytest.fixture()
def demo(tmp_path):
    conn = db.connect(str(tmp_path / "demo.db"))
    events, key = build()
    db.upsert_events(conn, events)
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    index.sync()
    return conn, index, key


def test_windows_split_by_gap_channel_and_size():
    msgs = [msg(1), msg(2, minutes=10), msg(3, minutes=100), msg(4, channel="other", minutes=101)]
    msgs += [msg(10 + i, channel="busy", minutes=i) for i in range(15)]
    sizes = sorted(len(w) for w in signals.build_windows(msgs))
    assert sizes == [1, 1, 2, 3, 12]


def test_keyword_rules():
    assert signals.keyword_type("bhai API nahi chal raha") == "blocker"
    assert signals.keyword_type("PR daal diya, please review kar do") == "help_request"
    assert signals.keyword_type("final: Postgres use karenge") == "decision"
    assert signals.keyword_type("login UI push kar diya") == "progress"
    assert signals.keyword_type("ok 👍") == "noise"


def test_llm_classifier_maps_short_ids_and_falls_back():
    window = [msg(1, text="build fail ho raha hai"), msg(2, minutes=1, text="ok"),
              msg(3, minutes=2, text="please check kar do", reply=1)]
    llm = FakeClassifierLLM()
    out = signals.LLMClassifier(llm).classify([window], {"discord:1", "discord:3"})
    assert set(out) == {"discord:1", "discord:3"}                    # bogus m999 ignored
    assert out["discord:1"]["classifier"] == "keyword"               # invalid type → keyword fallback
    assert out["discord:3"]["type"] == "help_request" and out["discord:3"]["classifier"] == "llm:fake"
    text, back = signals.LLMClassifier.render([window], {"discord:1", "discord:3"}, {})
    assert "context [A" in text and "m2 [A" in text and "reply to m1" in text and back == {"m1": "discord:1", "m2": "discord:3"}


def test_scan_is_incremental_and_links_tasks(demo):
    conn, index, key = demo
    stats = signals.scan(conn, signals.KeywordClassifier(), index)
    assert stats["classified"] == 15
    assert signals.scan(conn, signals.KeywordClassifier(), index)["classified"] == 0
    login = key["blockers"][0]["event"]
    row = conn.execute("SELECT type, task_id, link_method FROM signals WHERE event_id = ?", (login,)).fetchone()
    assert tuple(row) == ("blocker", "github:campuseats/app#12", "ref")
    conn.execute("UPDATE events SET body='all good now', updated_at='2030-01-01T00:00:00+00:00' WHERE id = ?", (login,))
    conn.commit()
    assert signals.scan(conn, signals.KeywordClassifier(), index)["classified"] == 1
    db.delete_event(conn, login)                                      # deleting a message removes its signal
    assert conn.execute("SELECT COUNT(*) FROM signals WHERE event_id = ?", (login,)).fetchone()[0] == 0


def test_question_parsing():
    assert wanted_types("what is blocking us?") == ["blocker", "help_request"]
    assert wanted_types("what did we decide about the DB?") == ["decision"]
    assert window_days("kya atka hua hai is hafte?") == 7 and window_days("last 3 days") == 3


def test_blocker_agent_open_and_possibly_resolved(demo):
    conn, index, key = demo

    class NoLLM:
        def chat(self, *a, **k):
            raise RuntimeError("quota")

    agent = BlockerAgent(conn, llm=NoLLM(), index=index, classifier=signals.KeywordClassifier())
    result = agent.ask("what is blocking us?")
    assert "OTP" in result["answer"] and "canteen" in result["answer"]
    assert any("unavailable" in w for w in result["warnings"]) and result["cited"]
    states = {i["summary"][:12]: i["state"] for i in agent.collect("blockers")["items"]}
    assert all(s == "open" for s in states.values())
    # Closing issue #12 after the chat message makes that blocker "possibly resolved".
    conn.execute("UPDATE events SET status='closed', updated_at=? WHERE id='github:campuseats/app#12'",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()
    items = agent.collect("blockers")["items"]
    login = next(i for i in items if "OTP" in i["summary"])
    assert login["state"].startswith("possibly resolved")


def test_supervisor_routes_to_blockers(demo):
    conn, index, _ = demo

    class RouteToBlockers:
        def chat(self, messages, json_mode=False, temperature=0.0):
            system = messages[0]["content"]
            if "supervisor" in system:
                assert "- blockers:" in system                       # the router is offered the new specialist
                return LLMReply(json.dumps({"standalone_question": "what is blocking us?", "parts": [
                    {"question": "what is blocking us?", "specialist": "blockers"}]}), "fake")
            if "You label messages" in system:
                raise RuntimeError("use keywords")
            return LLMReply("Login is blocked [1].", "fake")

    llm = RouteToBlockers()
    agent = SupervisorAgent(conn, retrieval=RetrievalAgent(conn, index=index, llm=llm), llm=llm)
    result = agent.ask("kya atka hua hai?")
    assert result["trace"][1]["step"] == "specialist:blockers" and result["cited"] == [1]


def test_metrics_and_dev_set_evaluation():
    m = evaluation.metrics({"a": "blocker", "b": "noise", "c": "blocker"}, {"a": "blocker", "b": "blocker"})
    assert m["per_class"]["blocker"] == {"precision": 0.5, "recall": 0.5, "f1": 0.5, "support": 2}
    assert m["confusion"]["blocker"]["noise"] == 1 and m["accuracy"] == round(1 / 3, 3)
    results, report = evaluation.run(str(evaluation.DEV_SET), "keyword")
    assert results["keyword baseline"]["n"] == 60 and "Confusion matrix" in report
    llm_results, _ = evaluation.run(str(evaluation.DEV_SET), "llm", llm=FakeClassifierLLM())
    assert llm_results["LLM few-shot"]["n"] == 60


def test_demo_gold_labels_in_db(demo, tmp_path):
    conn, _, key = demo
    conn.executemany("INSERT INTO labels VALUES (?, ?, 'test', 'now')", key["message_labels"].items())
    conn.execute("INSERT INTO labels VALUES ('discord:not-a-message', 'skip', 'test', 'now')")
    conn.commit()
    results, _ = evaluation.run("db", "keyword", conn=conn)
    assert results["keyword baseline"]["n"] == 15 and results["keyword baseline"]["accuracy"] >= 0.8
