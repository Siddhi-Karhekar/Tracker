"""A configured source only shows as connected while its poller or bot is actually running."""
from datetime import datetime, timedelta, timezone

from teampulse import db, health


def test_process_status(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "t.db"))
    for k in ("GITHUB_TOKEN", "NOTION_TOKEN", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert {v["state"] for v in health.process_status(conn).values()} == {"not used"}

    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "y")
    monkeypatch.setenv("POLL_MINUTES", "5")
    status = health.process_status(conn)
    assert status["poller"]["state"] == "never" and status["discord"]["state"] == "never"

    db.beat(conn, "poller")
    db.set_state(conn, db.HEARTBEAT["discord"], (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat())
    status = health.process_status(conn)
    assert status["poller"]["state"] == "ok" and status["discord"]["state"] == "stale"


def test_sources_page_does_not_claim_connected_when_the_poller_is_stopped(tmp_path, monkeypatch):
    import os

    from streamlit.testing.v1 import AppTest

    from teampulse.seed_demo import seed
    path = str(tmp_path / "demo.db")
    seed(path)
    for k, v in {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "off",
                 "GITHUB_TOKEN": "x"}.items():
        monkeypatch.setenv(k, v)
    import teampulse.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", path)
    at = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "views", "sources.py"), default_timeout=90)
    at.run()
    page = " ".join(m.value for m in at.markdown)
    assert "Not running" in page and "Connected" not in page
    db.beat(db.connect(path), "poller")
    at.run()
    assert "Connected" in " ".join(m.value for m in at.markdown)
