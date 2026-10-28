"""`!teampulse leave` promises to delete everything stored from that person, including copies derived from
their messages (AI extractions, labels, task links, cached AI responses, conflicts quoting them)."""
from teampulse import db, discrepancies
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLM
from teampulse.seed_demo import seed

DERIVED = ["events", "signals", "claims", "extractions", "labels", "task_links", "link_feedback", "indexed"]


def _processed_team(tmp_path):
    path = str(tmp_path / "t.db")
    seed(path)
    conn = db.connect(path)
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp_path / "chroma"))
    discrepancies.run(conn, index, LLM(conn, providers=["none"]), refresh=True)   # rules only, no AI
    return conn, index


def _rows_about(conn, ids, table):
    marks = ",".join("?" * len(ids))
    column = "id" if table == "events" else "event_id"
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})", ids).fetchone()[0]


def test_opting_out_deletes_derived_copies_too(tmp_path):
    conn, index = _processed_team(tmp_path)
    author = conn.execute("SELECT author FROM events e JOIN extractions x ON x.event_id = e.id "
                          "WHERE e.source = 'discord' GROUP BY author ORDER BY COUNT(*) DESC").fetchone()[0]
    ids = [r[0] for r in conn.execute("SELECT id FROM events WHERE source='discord' AND author=?", (author,))]
    with conn:
        conn.execute("INSERT OR REPLACE INTO labels VALUES (?, 'blocker', 'neha', 'now')", (ids[0],))
        conn.execute("INSERT OR REPLACE INTO link_feedback VALUES (?, NULL, 'neha', 'now')", (ids[0],))
        conn.execute("INSERT INTO llm_cache VALUES ('k', 'summary quoting their message', 'groq', 'now')")
    assert _rows_about(conn, ids, "extractions") and _rows_about(conn, ids, "task_links")

    removed = db.delete_discord_messages_of(conn, author)

    assert removed == len(ids)
    for table in DERIVED:
        assert _rows_about(conn, ids, table) == 0 or table == "indexed", table
    assert conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0
    left = [d for d in discrepancies.open_items(conn, ("open", "dismissed", "resolved"))
            if any(s.get("event_id") in ids for s in d["sides"])]
    assert not left
    index.sync()                                             # the search index drops them on its next sync
    assert _rows_about(conn, ids, "indexed") == 0
    assert not index.collection.get(where={"event_id": {"$in": ids}})["ids"]


def test_deleting_one_message_in_discord_removes_its_copies(tmp_path):
    conn, _ = _processed_team(tmp_path)
    event_id = conn.execute("SELECT event_id FROM extractions LIMIT 1").fetchone()[0]
    db.delete_event(conn, event_id)
    for table in DERIVED[:-1]:
        assert _rows_about(conn, [event_id], table) == 0, table
