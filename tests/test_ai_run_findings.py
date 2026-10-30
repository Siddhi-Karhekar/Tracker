"""Regressions for what the first real AI evaluation (Groq, openai/gpt-oss-120b, 30 Oct 2026) exposed."""
import json

import pytest

from teampulse import db, discrepancies
from teampulse.agents.retrieval import RetrievalAgent
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLM, LLMReply, normalise
from teampulse.seed_demo import seed
from teampulse.tasks import _letter_index, _reply_number, llm_choose


@pytest.fixture(scope="module")
def team(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ai")
    path = str(tmp / "t.db")
    seed(path)
    conn = db.connect(path)
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp / "chroma"))
    discrepancies.run(conn, index, LLM(conn, providers=["none"]), refresh=True)
    return conn, index


def test_model_specific_citation_and_hyphen_styles_are_normalised():
    """gpt-oss wrote “2026‑11‑17【1】”: the citation wasn't clickable and the right date failed the check."""
    assert normalise("The final review is on **2026‑11‑17**【1】.") == "The final review is on **2026-11-17**[1]."
    assert normalise("see 【3†L4-L9】 and 80 % done") == "see [3] and 80 % done"


@pytest.mark.parametrize("raw,idx", [("A", 0), ("a", 0), ("B)", 1), ("(C)", 2), ("Option D", 3), (None, -1),
                                     ("none", -1), ("Login task", -1)])
def test_task_letters_are_read_whatever_the_style(raw, idx):
    assert _letter_index(raw) == idx


def test_message_numbers_are_read_whatever_the_style():
    assert [_reply_number(x) for x in (2, "2", "Message 2", "m2")] == [2, 2, 2, 2] and _reply_number(None) is None


def test_chat_linking_survives_a_loosely_formatted_reply(team):
    conn, index = team

    class Loose:   # right answers, wrong format: the old parser threw every one of them away
        def chat(self, messages, json_mode=False, temperature=0.0):
            return LLMReply(json.dumps({"links": [{"message": "Message 1", "task": "a)"}]}), "fake")

    row = conn.execute("SELECT id, body, author_name, channel, created_at FROM events WHERE id='discord:900002'").fetchone()
    choices, _ = llm_choose(conn, index, Loose(), [{"event_id": row["id"], "body": row["body"], "task_hint": "",
                                                    "author_name": row["author_name"], "channel": row["channel"],
                                                    "created_at": row["created_at"]}])
    assert choices["discord:900002"] is not None


def test_retrieval_brings_in_replies_to_a_found_message(team):
    """“Who helped with Kabir's data problem?”: Neha's answer is a reply to Kabir's message."""
    conn, index = team

    class NoAI:
        def chat(self, *a, **k):
            raise RuntimeError("offline")

    result = RetrievalAgent(conn, index=index, llm=NoAI()).ask("canteen past orders data nahi mila")
    ids = [e["event_id"] for e in result["evidence"]]
    assert "discord:900004" in ids and "discord:900015" in ids
    assert ids.index("discord:900015") == ids.index("discord:900004") + 1      # right after the message it answers


def test_a_used_up_daily_quota_is_reported_once_and_skipped():
    """Real run: Groq's 200k tokens/day ran out mid-evaluation and every later call retried it."""
    from types import SimpleNamespace

    class Quota(Exception):
        status_code = 429

    calls = []

    def create(**kw):
        calls.append(kw)
        raise Quota("Rate limit reached for model `openai/gpt-oss-120b` ... on tokens per day (TPD): Limit 200000")

    llm = LLM(providers=["groq"])
    llm._clients = {"groq": (SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
                             "openai/gpt-oss-120b")}
    for _ in range(3):
        with pytest.raises(Exception):
            llm.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1 and "daily free quota" in llm._dead["groq"] and "gpt-oss-20b" in llm._dead["groq"]
    assert llm.stats["unanswered"] == 3


def test_an_evaluation_that_lost_the_ai_is_marked_invalid(tmp_path):
    from teampulse.eval import agents

    class Flaky(LLM):   # answers nothing: like a quota that ran out
        def __init__(self):
            super().__init__(None, providers=["none"])

    result = agents.run(offline=False, folder=tmp_path, only=["router", "memory"], embedder=HashEmbedder(), llm=Flaky())
    assert result["ai_failures"] and not result["passed"]
    md = agents.to_markdown(result)
    assert "Not a valid AI result" in md and "✅" not in md.split("<details>")[0]


def test_the_evaluation_stops_once_no_ai_provider_is_left(tmp_path):
    from teampulse.eval import agents

    class OutOfQuota(LLM):
        def __init__(self):
            super().__init__(None, providers=["groq"])
            self._dead["groq"] = "daily quota used up"

    result = agents.run(offline=False, folder=tmp_path, only=["router", "retrieval", "memory"],
                        embedder=HashEmbedder(), llm=OutOfQuota())
    assert list(result["results"]) == ["router"]                   # stopped after the first step
    assert result["ai_failures"]["retrieval"] == "not run" and not result["passed"]
