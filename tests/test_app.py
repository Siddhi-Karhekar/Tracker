"""UI tests: every page renders on demo data without AI, and its main action works."""
import html
import os

from streamlit.testing.v1 import AppTest

from teampulse import db
from teampulse.seed_demo import build, seed

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _env(tmp_path, monkeypatch, full_seed: bool = False, empty: bool = False) -> str:
    path = str(tmp_path / "demo.db")
    if full_seed:
        seed(path)
    elif not empty:
        db.upsert_events(db.connect(path), build()[0])
    for k, v in {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "off",
                 "TEAMPULSE_PUBLIC_DEMO": "off"}.items():
        monkeypatch.setenv(k, v)
    import teampulse.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", path)
    return path


def _run(path_parts, timeout=90) -> AppTest:
    app = AppTest.from_file(os.path.join(ROOT, *path_parts), default_timeout=timeout)
    app.run()
    assert not app.exception, app.exception
    return app


def _text(app) -> str:
    return " ".join(m.value for m in app.markdown)


def test_router_opens_home_with_headline_and_sync_strip(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, full_seed=True)
    app = _run(["app.py"])
    text = _text(app)
    assert "tp-hero" in text and "marked done in Notion" in text            # the most serious conflict leads
    assert "class='tp-strip'" in text and "Payment integration" in text     # sync strip lists every task
    assert "Waiting for help" in text and "Blocked on" in text              # needs-attention list mixes both


def test_home_empty_state(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, empty=True)
    app = _run(["app.py"])                      # page links resolve only inside the full app
    assert "No project data yet." in _text(app)


def test_ask_page_answers_and_follows_up(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    app = _run(["views", "ask.py"])
    app.chat_input[0].set_value("login ka kya scene hai?").run()
    assert not app.exception and "login" in _text(app).lower()
    app.chat_input[0].set_value("and who owns it?").run()
    assert not app.exception
    assert len([m for m in app.chat_message if m.name == "assistant"]) == 2


def test_blockers_page_reads_chat_with_rules(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    app = _run(["views", "blockers.py"])
    assert "Chat hasn&#x27;t been read yet." in _text(app)
    app.sidebar.toggle[0].set_value(True).run()
    next(b for b in app.sidebar.button if b.label == "Read new messages").click().run()
    assert not app.exception
    text = _text(app)
    assert "OTP" in text and "2 blocked" in text


def test_label_page_labels_and_evaluates(tmp_path, monkeypatch):
    path = _env(tmp_path, monkeypatch)
    app = _run(["views", "label.py"])
    app.button(key="label-blocker").click().run()          # labeller = the signed-in user, no name to type
    assert db.connect(path).execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1
    next(b for b in app.button if b.label.startswith("Run evaluation")).click().run()
    assert not app.exception and any("Chat classifier evaluation" in m.value for m in app.markdown)


def test_sources_page_explains_what_is_missing(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, full_seed=True)
    for var in ("GITHUB_TOKEN", "NOTION_TOKEN", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    app = _run(["views", "sources.py"])
    text = _text(app)
    assert "items stored" in text and "Demo data" in text
    next(b for b in app.button if b.label == "Run check").click().run()
    text = _text(app)
    assert text.count("Not set up") >= 3 and "DISCORD_BOT_TOKEN" in html.unescape(text)   # as the browser shows it
    assert len(app.dataframe[0].value) == 4                                   # members table
