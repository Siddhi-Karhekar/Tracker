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
