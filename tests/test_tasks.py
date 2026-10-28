"""Week 6 tests: task registry, chat linking (LLM + human), claims extraction, linking evaluation, Tasks page."""
import json
import os
import re

import pytest
from streamlit.testing.v1 import AppTest

from teampulse import claims, db
from teampulse.eval import linking
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply
from teampulse.models import Event
from teampulse.seed_demo import REPO, build
from teampulse.tasks import Registry, link_chat, save_feedback, title_similarity

KEYWORDS = [("payment", "Payment"), ("recommend", "recommendation"), ("data", "recommendation"),
            ("order history", "Order history"), ("deploy", "Deploy"), ("report", "Report"), ("login", "Login")]


class FakeLLM:
    """Answers each Week 6 prompt the way a sensible model would, using keywords."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, json_mode=False, temperature=0.0):
        system, user = messages[0]["content"], messages[1]["content"]
        self.calls.append(system[:30])
        if system.startswith("You label messages"):
            raise RuntimeError("classifier: use keywords")
        if system.startswith("Decide whether each Notion task"):
            items = re.findall(r"Item (\d+)\. Notion task: (.*)\n((?:  [A-H]\) .*\n?)+)", user)
            out = []
            for n, title, cands in items:
                letters = re.findall(r"  ([A-H])\) GitHub issue: (.*)", cands)
                hit = next((l for l, t in letters if title_similarity(title, t) > 0.3), None)
                out.append({"item": int(n), "match": hit})
            return LLMReply(json.dumps({"matches": out}), "fake")
        if system.startswith("Each chat message below comes"):
            out = []
            for block in user.split("\n\nMessage ")[0:]:
                n = int(re.match(r"(?:Message )?(\d+)", block).group(1))
                body = block.split("\n")[1].lower()
                cands = re.findall(r"  ([A-H])\) (.*)", block)
                pick = None
                for word, title_word in KEYWORDS:
                    if word in body:
                        pick = next((l for l, t in cands if title_word.lower() in t.lower()), None)
                        if pick:
                            break
                out.append({"message": n, "task": pick})
            return LLMReply(json.dumps({"links": out}), "fake")
        if system.startswith("Each chat message below is from"):
            out = []
            for n, body in re.findall(r"Message (\d+) \[[^\]]*\]: (.*)", user):
                item = {"message": int(n), "status": None, "owner": None, "deadline": None}
                if "Aman dekh raha" in body or "payment main kar raha" in body:
                    item.update(status="in_progress", owner="Aman")
                if "M2 tak" in body:
                    item["deadline"] = "M2"
                if "nahi mila" in body:
                    item["status"] = "blocked"
                out.append(item)
            return LLMReply(json.dumps({"items": out}), "fake")
        raise AssertionError(system[:60])


@pytest.fixture()
def demo(tmp_path):
    conn = db.connect(str(tmp_path / "demo.db"))
    events, key = build()
    db.upsert_events(conn, events)
    conn.executemany("INSERT INTO members (display_name, discord_id, github_login, notion_user_id, consent) "
                     "VALUES (?, ?, ?, ?, 1)", [("Riya", "1001", "riya-dev", "n-riya"), ("Aman", "1002", "aman-codes", "n-aman"),
                                                ("Kabir", "1003", "kabir-ml", "n-kabir"), ("Neha", "1004", "neha-pm", "n-neha")])
    conn.commit()
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    index.sync()
    return conn, index, key


def test_title_similarity():
    assert title_similarity("Login with college email OTP (#12)", "Login with college email OTP") == 1.0
    assert title_similarity("Menu API", "Deploy on Render") < 0.45


def test_registry_merges_sources(demo):
    conn, _, _ = demo
    stats = Registry(conn).build()
    assert stats["tasks"] == 7 and stats["notion_merged"] == 6
    link = lambda e: tuple(conn.execute("SELECT task_id, role, method FROM task_links WHERE event_id = ?", (e,)).fetchone())
    assert link("notion:task-login") == (f"task:{REPO}#12", "notion_task", "ref")
    assert link(f"github:{REPO}#15") == (f"task:{REPO}#14", "pr", "ref")           # "fixes #14"
    assert link("github:review:7001") == (f"task:{REPO}#14", "review", "parent")    # review of PR #15
    assert link("github:commit:a1f9c2e") == (f"task:{REPO}#16", "commit", "ref")
    assert link("github:comment:5001") == (f"task:{REPO}#12", "comment", "parent")
    assert link("notion:doc-report") == ("task:notion:doc-report", "notion_task", "primary")
    plan = conn.execute("SELECT 1 FROM task_links WHERE event_id='notion:plan-campuseats'").fetchone()
    assert plan is None                                                             # plan page: not a task


def test_notion_page_without_ref_matched_by_title_or_llm(demo):
    conn, _, _ = demo
    conn.execute("UPDATE events SET title='Deploy on Render', extra=json_set(extra, '$.issue_refs', json('[]')) "
                 "WHERE id='notion:task-deploy'")
    conn.execute("UPDATE events SET title='Menu backend endpoints', extra=json_set(extra, '$.issue_refs', json('[]')) "
                 "WHERE id='notion:task-menu'")
    conn.commit()
    Registry(conn).build()
    method = lambda e: conn.execute("SELECT task_id, method FROM task_links WHERE event_id = ?", (e,)).fetchone()
    assert tuple(method("notion:task-deploy")) == (f"task:{REPO}#22", "title")
    assert method("notion:task-menu")["task_id"] == "task:notion:task-menu"         # no LLM: kept separate
    llm = FakeLLM()
    Registry(conn, llm).build()
    assert tuple(method("notion:task-menu")) == (f"task:{REPO}#14", "llm")


def test_full_refresh_links_chat_and_extracts_claims(demo):
    conn, index, key = demo
    llm = FakeLLM()
    stats = claims.refresh(conn, index, llm, scan_chat=True)
    assert stats["chat_links"]["llm_calls"] == 1 and stats["extraction"]["llm_calls"] == 1
    task_of = lambda e: (conn.execute("SELECT task_id FROM task_links WHERE event_id = ?", (e,)).fetchone() or [None])[0]
    for event_id, task in key["chat_links"].items():
        if event_id != key["decisions"][0]["event"]:
            assert task_of(event_id) == task, event_id
    cards = {c["id"]: c for c in claims.task_cards(conn)}
    pay = cards[f"task:{REPO}#16"]
    assert pay["owner"]["notion"]["value"] == "Kabir" and pay["owner"]["chat"]["value"] == "Aman"
    assert pay["deadline"]["notion"]["value"] != pay["deadline"]["github"]["value"]
    assert pay["deadline"]["chat"]["value"] == pay["deadline"]["github"]["value"]   # "M2 tak" → milestone date
    assert pay["workers"]["Aman"] == 2
    login = cards[f"task:{REPO}#12"]
    assert (login["status"]["notion"]["value"], login["status"]["github"]["value"], login["status"]["chat"]["value"]) \
        == ("done", "open", "blocked")
    assert cards[f"task:{REPO}#14"]["status"]["notion"]["value"] == "in_progress"
    assert cards[f"task:{REPO}#22"]["status"]["notion"]["value"] == "todo"
    # Second refresh reuses cached extractions and checked links: no new AI calls.
    again = claims.refresh(conn, index, llm)
    assert again["chat_links"]["llm_calls"] == 0 and again["extraction"]["llm_calls"] == 0


def test_human_correction_wins(demo):
    conn, index, key = demo
    llm = FakeLLM()
    claims.refresh(conn, index, llm, scan_chat=True)
    db_msg = key["decisions"][0]["event"]
    save_feedback(conn, db_msg, f"task:{REPO}#22", "tester")
    claims.refresh(conn, index, llm)
    row = conn.execute("SELECT task_id, method FROM task_links WHERE event_id = ?", (db_msg,)).fetchone()
    assert tuple(row) == (f"task:{REPO}#22", "human")
    save_feedback(conn, db_msg, None, "tester")                                       # "not about a task"
    claims.refresh(conn, index, llm)
    assert conn.execute("SELECT 1 FROM task_links WHERE event_id = ?", (db_msg,)).fetchone() is None


def test_keyword_extract_and_people(demo):
    conn, _, _ = demo
    people = claims.People(conn)
    assert people.resolve("aman-codes") == "Aman" and people.resolve("1003") == "Kabir"
    assert people.resolve("@Riya Sharma") == "Riya" and people.resolve("Somebody") == "Somebody"
    k = claims.keyword_extract
    assert k("payment Aman dekh raha hai", "Kabir", "progress", False, people)["owner"] == "Aman"
    assert k("haan main kar raha hu, M2 tak", "Aman", "progress", False, people) == \
        {"status": "in_progress", "owner": "Aman", "deadline": "M2"}
    assert k("API nahi chal raha", "Riya", "blocker", False, people)["status"] == "blocked"


def test_linking_evaluation_shows_llm_gain(demo, tmp_path):
    conn, index, key = demo
    path = tmp_path / "key.json"
    path.write_text(json.dumps(key))
    results, report = linking.run(conn, index, FakeLLM(), str(path))
    assert results["refs"]["accuracy"] < results["+ LLM"]["accuracy"] == 1.0
    assert "| + LLM | 10 |" in report


def test_tasks_page(tmp_path, monkeypatch):
    path = str(tmp_path / "demo.db")
    db.upsert_events(db.connect(path), build()[0])
    for k, v in {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "groq"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    import teampulse.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", path)
    page = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "views", "tasks.py"), default_timeout=60)
    page.run()
    assert not page.exception
    frame = page.dataframe[0].value
    assert len(frame) == 7 and "⚠" in frame.loc[0, "Status (Notion)"]       # login: Notion done vs GitHub open
