"""Offline tests: mappers use sample API JSON / fake Discord objects, DB uses in-memory SQLite."""
from datetime import datetime, timezone
from types import SimpleNamespace

from teampulse import db
from teampulse.ingest.discord_bot import message_to_event
from teampulse.ingest.github_poller import comment_to_event, commit_to_event, issue_to_event, review_to_event
from teampulse.ingest.notion_poller import blocks_to_text, page_to_event
from teampulse.models import issue_refs

REPO = "team/capstone"


def test_issue_refs():
    assert issue_refs("fixes #12, see #3 and abc#9 and https://x/#7") == [3, 12]


def test_open_issue():
    ev = issue_to_event({
        "number": 12, "title": "Login page", "body": "blocked by #4", "state": "open",
        "user": {"login": "riya"}, "assignees": [{"login": "aman"}], "labels": [{"name": "frontend"}],
        "html_url": "u", "created_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-20T10:00:00Z",
    }, REPO)
    assert ev.id == "github:team/capstone#12" and ev.kind == "issue" and ev.status == "open"
    assert ev.assignees == ["aman"] and ev.extra["labels"] == ["frontend"] and ev.extra["issue_refs"] == [4]


def test_merged_pr():
    ev = issue_to_event({
        "number": 15, "title": "Add API", "state": "closed", "user": {"login": "aman"},
        "pull_request": {"merged_at": "2026-10-21T08:00:00Z"},
        "created_at": "2026-10-19T08:00:00Z", "updated_at": "2026-10-21T08:00:00Z",
    }, REPO)
    assert ev.kind == "pr" and ev.status == "merged"


def test_commit_comment_review():
    c = commit_to_event({"sha": "abc", "html_url": "u", "author": None,
                         "commit": {"message": "Fix auth (#12)\n\nmore", "author": {"name": "Riya", "email": "r@x", "date": "2026-10-20T10:00:00Z"}}}, REPO)
    assert c.title == "Fix auth (#12)" and c.author == "r@x" and c.extra["issue_refs"] == [12]
    m = comment_to_event({"id": 9, "body": "still failing", "user": {"login": "riya"}, "html_url": "u",
                          "issue_url": "https://api.github.com/repos/team/capstone/issues/12",
                          "created_at": "2026-10-20T10:00:00Z", "updated_at": "2026-10-20T10:00:00Z"}, REPO)
    assert m.parent_id == "github:team/capstone#12"
    r = review_to_event({"id": 5, "state": "CHANGES_REQUESTED", "user": {"login": "riya"},
                         "submitted_at": "2026-10-21T09:00:00Z"}, REPO, 15)
    assert r.status == "changes_requested" and r.parent_id == "github:team/capstone#15"


def test_notion_page():
    page = {
        "id": "p1", "url": "u", "created_time": "2026-10-01T00:00:00.000Z",
        "last_edited_time": "2026-10-22T05:00:00.000Z",
        "created_by": {"id": "u1"}, "last_edited_by": {"id": "u2"},
        "properties": {
            "Task": {"type": "title", "title": [{"plain_text": "Login page"}]},
            "Status": {"type": "status", "status": {"name": "Done"}},
            "Owner": {"type": "people", "people": [{"id": "u2", "name": "Aman"}]},
            "Due": {"type": "date", "date": {"start": "2026-10-25"}},
        },
    }
    body = blocks_to_text([
        {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Checklist"}]}},
        {"type": "to_do", "to_do": {"checked": True, "rich_text": [{"plain_text": "UI"}]}},
        {"type": "to_do", "to_do": {"checked": False, "rich_text": [{"plain_text": "OTP (#12)"}]}},
    ])
    ev = page_to_event(page, body, "Tasks")
    assert ev.title == "Login page" and ev.status == "Done" and ev.assignees == ["Aman"]
    assert ev.extra["due"] == "2026-10-25" and ev.extra["issue_refs"] == [12]
    assert "[x] UI" in ev.body and "[ ] OTP" in ev.body


def _fake_message(mid, author_id, text, thread=False, reply_to=None):
    parent = SimpleNamespace(id=100, name="backend")
    channel = SimpleNamespace(id=200, name="login-bug", parent=parent) if thread else SimpleNamespace(id=100, name="backend", parent=None)
    return SimpleNamespace(
        id=mid, channel=channel, clean_content=text, content=text, jump_url="j",
        author=SimpleNamespace(id=author_id, display_name="Riya", bot=False),
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
        created_at=datetime(2026, 10, 20, 10, tzinfo=timezone.utc), edited_at=None, attachments=[],
    )


def test_discord_mapping():
    ev = message_to_event(_fake_message(1, 42, "bhai login API nahi chal raha #12", thread=True, reply_to=7))
    assert ev.id == "discord:1" and ev.author == "42" and ev.channel == "backend/login-bug"
    assert ev.parent_id == "discord:7" and ev.extra["thread_id"] == "200" and ev.extra["issue_refs"] == [12]


def test_db_upsert_consent_and_optout():
    conn = db.connect(":memory:")
    db.set_discord_consent(conn, "42", "Riya", True)
    assert db.consented_discord_ids(conn) == {"42"}
    db.upsert_events(conn, [message_to_event(_fake_message(1, 42, "hi")), message_to_event(_fake_message(1, 42, "hi edited"))])
    assert conn.execute("SELECT body FROM events WHERE id='discord:1'").fetchone()[0] == "hi edited"
    db.set_discord_consent(conn, "42", "Riya", False)
    assert db.delete_discord_messages_of(conn, "42") == 1
    assert db.consented_discord_ids(conn) == set()
    db.set_state(conn, "k", "v1"); db.set_state(conn, "k", "v2")
    assert db.get_state(conn, "k") == "v2"
