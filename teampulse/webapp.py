"""Shared helpers for the Streamlit pages: one agent + index loaded once for all pages, plus public-demo mode.

Public demo (TEAMPULSE_PUBLIC_DEMO=on, for a deployed copy anyone can open):
  * uses only the fictional demo team, seeded automatically and re-seeded every 20 hours so its relative
    dates ("due in 2 days") stay true;
  * shows a banner on every page;
  * limits questions per visitor session (TEAMPULSE_DEMO_QUESTION_LIMIT, default 15), so strangers can't
    use up your free AI quota. Answers are cached, so repeated example questions are free.
Never deploy your real team's database publicly.
"""
from datetime import datetime, timezone

import streamlit as st

from teampulse import auth, config, db, tracing, ui
from teampulse.agents.supervisor import SupervisorAgent

SOURCE_ICON = {"github": "🐙", "notion": "📝", "discord": "💬"}
TYPE_BADGE = {"blocker": "🚧 Blocker", "help_request": "🙋 Help request", "decision": "✅ Decision",
              "progress": "📈 Progress", "noise": "💤 Noise"}
RESEED_HOURS = 20


public_demo = auth.public_demo   # one definition, used by the pages and the permission checks


def demo_question_limit() -> int:
    return int(config.get("TEAMPULSE_DEMO_QUESTION_LIMIT", "15"))


def _demo_db_path() -> str:
    return config.get("TEAMPULSE_DB", "data/demo.db") if public_demo() else config.DB_PATH


def ensure_demo(path: str) -> bool:
    """(Re)seed the fictional team when missing or older than RESEED_HOURS. Returns True if it seeded."""
    from pathlib import Path

    from teampulse.seed_demo import seed

    if Path(path).exists():
        row = db.connect(path).execute("SELECT value FROM sync_state WHERE key = 'demo:seeded_at'").fetchone()
        if row and (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() < RESEED_HOURS * 3600:
            return False
    for p in Path(path).parent.glob(Path(path).stem + "*"):   # db, wal files, chroma folder
        if p.is_file():
            p.unlink()
        else:
            import shutil
            shutil.rmtree(p, ignore_errors=True)
    seed(path)
    db.set_state(db.connect(path), "demo:seeded_at", datetime.now(timezone.utc).isoformat())
    return True


@st.cache_resource(show_spinner="Loading the search model and data (first start takes a minute)…", ttl=RESEED_HOURS * 3600)
def _agent(db_path: str, demo: bool) -> SupervisorAgent:
    tracing.setup()   # no-op unless TEAMPULSE_TRACING=phoenix
    seeded = ensure_demo(db_path) if demo else False
    agent = SupervisorAgent(db.connect(db_path))
    from teampulse import maintenance
    maintenance.purge(agent.conn)   # retention: old sessions, activity log and AI cache (also run by the poll loop)
    agent.index.sync()
    if seeded:   # prepare chat labels, tasks, claims and discrepancies so every page has content immediately
        from teampulse import discrepancies
        from teampulse.seed_demo import create_demo_accounts
        discrepancies.run(agent.conn, agent.index, agent.llm, refresh=True)
        create_demo_accounts(db_path)   # random passwords nobody needs: the demo signs in with one click
    return agent


def get_agent() -> SupervisorAgent:
    demo = public_demo()
    return _agent(_demo_db_path(), demo)


def banner():
    st.info("🎭 **Public demo** with a fictional student team (CampusEats). The data resets daily. "
            "Nothing here is real; your own team's data stays on your machine.", icon=None)


# ---- sign-in -----------------------------------------------------------------------------------------
# The session token lives only in Streamlit's server-side session state: never in the URL, a cookie the page
# can read, or the browser's storage. Every page load re-checks it against the database, so signing out,
# deactivation, a password reset or an expired session takes effect on the very next click.
TOKEN_KEY = "tp_session"


def auth_enabled() -> bool:
    """Sign-in is on by default. TEAMPULSE_AUTH=off is for local development only."""
    return (config.get("TEAMPULSE_AUTH", "on") or "on").lower() != "off"


def current_actor(conn=None) -> auth.Actor | None:
    if not auth_enabled():
        return auth.LOCAL_ADMIN
    conn = conn or get_agent_quiet().conn
    token = st.session_state.get(TOKEN_KEY)
    actor = auth.resolve(conn, token)
    if actor is None and token:
        _clear_session()
        st.session_state.signed_out_reason = "Your session ended. Please sign in again."
    return actor


def _clear_session() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def sign_in(conn, username: str, password: str) -> auth.Actor:
    token, actor = auth.login(conn, username, password)
    _clear_session()                      # nothing from a previous user's session carries over
    st.session_state[TOKEN_KEY] = token
    return actor


def demo_sign_in(conn, username: str) -> auth.Actor:
    token, actor = auth.demo_session(conn, username)
    _clear_session()
    st.session_state[TOKEN_KEY] = token
    return actor


def sign_out(conn) -> None:
    auth.logout(conn, st.session_state.get(TOKEN_KEY))
    _clear_session()
    st.session_state.signed_out_reason = "You've signed out."


def get_agent_quiet() -> SupervisorAgent:
    return _agent(_demo_db_path(), public_demo())


def require_page(page: str) -> auth.Actor:
    """First line of every page: who is signed in, and may they open this page? Stops the page if not."""
    agent = get_agent()
    actor = current_actor(agent.conn)
    if actor is None:
        ui.empty("Please sign in.", "Your session has ended or you haven't signed in yet.")
        st.stop()
    if public_demo():
        banner()   # here, once per page; get_agent() runs several times per page load
    if not auth.page_allowed(agent.conn, actor, page):
        auth.audit(agent.conn, actor, "page.open", page, "denied")
        ui.empty("You don't have access to this page.", "Your team leader decides which pages team members can open.")
        st.stop()
    return actor
