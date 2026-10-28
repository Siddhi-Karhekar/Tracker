"""Data sources: what is connected, how fresh it is, who opted in, and a check that says what to fix."""
import pandas as pd
import streamlit as st

from teampulse import actions, auth, config, health, ui
from teampulse.webapp import get_agent, public_demo, require_page

actor = require_page("sources")
agent = get_agent()
conn = agent.conn

ui.header("Data sources", "Where TeamPulse reads from, how fresh each source is, and who has opted in.")

SOURCES = [("github", "GitHub", "GITHUB_TOKEN", "Issues, pull requests, commits and reviews, every few minutes."),
           ("notion", "Notion", "NOTION_TOKEN", "Task database and plan pages, every few minutes."),
           ("discord", "Discord", "DISCORD_BOT_TOKEN", "Team chat as it happens, from members who opted in.")]


PROCESS = {"github": "poller", "notion": "poller", "discord": "discord"}
running = health.process_status(conn)
cols = st.columns(3, gap="medium")
for col, (key, name, env, what) in zip(cols, SOURCES):
    count, latest = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM events WHERE source = ?", (key,)).fetchone()
    configured = bool(config.get(env)) and not public_demo()
    process = running[PROCESS[key]]
    if configured and process["state"] == "ok":
        dot, state = ui.AGREE, "Connected"
    elif configured:   # a token alone doesn't mean data is flowing: the poller or bot must be running
        how = "the bot" if key == "discord" else "the poller"
        dot, state = ui.STALE, (
            f"Not running: {how} hasn't checked in" + (f" since {ui.ago(process['last'])}" if process["last"] else " yet"))
    elif count:
        dot, state = ui.ACCENT, "Demo data" if public_demo() or "demo" in config.DB_PATH else "Stored data only"
    else:
        dot, state = ui.LINE, "Not set up"
    col.markdown(f"<div class='tp-source-card'><h4><span class='tp-dot' style='background:{dot}'></span>{name}</h4>"
                 f"<div class='tp-meta'>{state}</div><p style='margin:.6rem 0 .2rem 0'>{count} items stored, "
                 f"newest {ui.ago(latest)}.</p><div class='tp-meta'>{what}</div></div>", unsafe_allow_html=True)

st.subheader("Check connections")
if public_demo():
    st.caption("This public demo uses a fictional team, so there is nothing to connect. Run TeamPulse on your own "
               "machine to connect your team.")
elif not auth.can(actor, "sources.check"):
    st.caption("Your team leader can check the connections from here.")
else:
    st.caption("Signs in to each service with your tokens (read-only) and checks every permission TeamPulse needs.")
    if st.button("Run check", icon=":material/fact_check:"):
        with st.spinner("Checking GitHub, Notion and Discord…"):
            st.session_state.health = actions.check_connections(conn, actor)
    results = st.session_state.get("health")
    if results:
        tone = {True: "ok", False: "bad", None: "quiet"}
        word = {True: "OK", False: "Needs a fix", None: "Not set up"}
        html = "".join(ui.row(f"{ui.pill(word[c.ok], tone[c.ok])} &nbsp;{ui.e(ui.SOURCE_LABEL.get(c.source, c.source))}: "
                              f"{ui.e(c.name)}", ui.e(c.detail), tone[c.ok],
                              ui.e(c.fix) if c.fix and c.ok is not True else "") for c in results)
        st.markdown(html, unsafe_allow_html=True)

st.subheader("AI model")
available = agent.llm.available()
cached = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
st.markdown(f"Tried in this order: **{', '.join(available) or 'none set up'}**. {cached} answers cached, so repeated "
            "questions cost nothing.")
if available and auth.can(actor, "sources.check") and st.button("Send a test message", icon=":material/bolt:"):
    try:
        reply = actions.test_ai(conn, actor, agent.llm)
        st.success(f"{reply.provider} replied: {reply.text.strip()[:40]}")
    except Exception as exc:
        st.error(f"No AI provider answered. {exc}")
if not available:
    st.caption("Without AI, TeamPulse still works using rules and keyword matching, with fewer chat messages linked.")

if not actor.is_leader:
    st.stop()   # the team list with linked accounts is for leaders

st.subheader("Team members")
members = conn.execute("SELECT m.*, r.status AS review FROM members m LEFT JOIN contribution_reviews r "
                       "ON r.member = m.display_name ORDER BY m.display_name").fetchall()
if members:
    frame = pd.DataFrame([{
        "Name": m["display_name"], "Opted in": bool(m["consent"]), "Discord": bool(m["discord_id"]),
        "GitHub": m["github_login"] or "", "Notion": bool(m["notion_user_id"]),
        "Report": {"approved": "Approved", "disputed": "Asked for changes"}.get(m["review"], "Not reviewed"),
    } for m in members])
    st.dataframe(frame, hide_index=True, width="stretch")
    st.caption("Link someone's accounts with `python -m teampulse.cli link --name Riya --discord <id> --github <login>`.")
else:
    ui.empty("No members yet.", "Teammates type !teampulse join in Discord to opt in.")

with st.expander("How to keep data flowing"):
    st.code("python -m teampulse.ingest.discord_bot          # keeps reading chat\n"
            "python -m teampulse.ingest.poll --scan-chat     # GitHub + Notion every 5 min, then compares sources\n"
            "python -m teampulse.health                      # this check, from the terminal", language="bash")
