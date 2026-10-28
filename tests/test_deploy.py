"""Week 10 tests: public demo mode, overview dashboard, FastEmbed wrapper, Streamlit secrets fallback."""
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import numpy as np
from streamlit.testing.v1 import AppTest

from teampulse import config, db, index as ix
from teampulse.webapp import ensure_demo

ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_demo_seeds_once_and_refreshes_when_old(tmp_path):
    path = str(tmp_path / "demo.db")
    assert ensure_demo(path) is True
    assert ensure_demo(path) is False                                          # fresh: keep it
    conn = db.connect(path)
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    db.set_state(conn, "demo:seeded_at", old)
    conn.execute("INSERT INTO labels VALUES ('x', 'noise', 'visitor', 'now')")
    conn.commit()
    assert ensure_demo(path) is True                                           # stale: rebuilt from scratch
    assert db.connect(path).execute("SELECT COUNT(*) FROM labels WHERE labeller='visitor'").fetchone()[0] == 0


def _app(tmp_path, monkeypatch, demo: bool, limit: int = 15):
    path = str(tmp_path / "demo.db")
    env = {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "off",
           "TEAMPULSE_PUBLIC_DEMO": "on" if demo else "off", "TEAMPULSE_DEMO_QUESTION_LIMIT": str(limit)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(config, "DB_PATH", path)
    if not demo:
        from teampulse.seed_demo import seed
        seed(path)
    app = AppTest.from_file(os.path.join(ROOT, "app.py"), default_timeout=90)
    app.run()
    assert not app.exception
    return app


def test_public_demo_banner_home_and_limit(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, demo=True, limit=1)
    assert any("Public demo" in i.value for i in app.info)
    home = " ".join(m.value for m in app.markdown)
    assert "marked done in Notion" in home and "Also: 4 more source conflicts, 2 open blockers" in home
    ask = AppTest.from_file(os.path.join(ROOT, "views", "ask.py"), default_timeout=90)
    ask.run()
    ask.chat_input[0].set_value("kya atka hua hai?").run()
    ask.chat_input[0].set_value("and what else?").run()
    assert any("answers 1 questions per visit" in w.value for w in ask.warning)
    assert len([m for m in ask.chat_message if m.name == "assistant"]) == 1


def test_no_banner_or_limit_by_default(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, demo=False)
    assert not any("Public demo" in i.value for i in app.info)


def test_fastembed_wrapper(monkeypatch):
    class FakeModel:
        def __init__(self, model_name):
            self.model_name = model_name

        def embed(self, texts, batch_size=32):
            for t in texts:
                yield np.array([3.0, 4.0]) if "query:" not in t else np.array([0.0, 2.0])

    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeModel))
    monkeypatch.setenv("TEAMPULSE_EMBEDDER", "fastembed:some/multilingual-model")
    emb = ix.make_embedder()
    assert isinstance(emb, ix.FastEmbedEmbedder) and emb.name == "fastembed:some/multilingual-model"
    assert emb.documents(["a"]) == [[0.6, 0.8]] and emb.link_threshold == 0.65
    monkeypatch.setenv("TEAMPULSE_EMBEDDER", "fastembed:intfloat/multilingual-e5-large")
    e5 = ix.make_embedder()
    assert e5.query("x") == [0.0, 1.0] and e5.link_threshold == 0.84          # e5 gets "query:" prefix
    monkeypatch.setenv("TEAMPULSE_EMBEDDER", "hash")
    assert isinstance(ix.make_embedder(), ix.HashEmbedder)


def test_streamlit_secrets_fallback(tmp_path, monkeypatch):
    (tmp_path / ".streamlit").mkdir()
    (tmp_path / ".streamlit" / "secrets.toml").write_text('GROQ_TEST_KEY = "from-secrets"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROQ_TEST_KEY", raising=False)
    import streamlit as st
    st.secrets._secrets = None                                                 # force a re-read in this test
    assert config.get("GROQ_TEST_KEY") == "from-secrets"
    monkeypatch.setenv("GROQ_TEST_KEY", "from-env")
    assert config.get("GROQ_TEST_KEY") == "from-env"                           # environment wins
