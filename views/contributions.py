"""Contributions: evidence of what each member did, reviewed by that member before the team sees it."""
import dataclasses

import streamlit as st

from teampulse import actions, auth, contributions as contrib, ui
from teampulse.webapp import auth_enabled, get_agent, require_page

actor = require_page("contributions")
conn = get_agent().conn

ui.header("Contributions", "What each member did, from GitHub, Notion and chat. Evidence, not scores: each member "
          "reads their own report first, can add context, and decides when the team sees it.")

everyone = contrib.members(conn)
if not everyone:
    ui.empty("No team members yet.", "Teammates opt in by typing !teampulse join in Discord.")
    st.stop()
days = st.sidebar.slider("Period in days", 7, 60, 14)

mine, team = st.tabs(["My report", "Team"])

with mine:
    if auth_enabled():
        me = actor.member_name      # you can only ever open your own report
        if not me:
            ui.empty("Your account isn't linked to the team data yet.",
                     "Ask your team leader to link it on the Team & access page.")
    else:   # local development without sign-in: no identity to go on
        me = st.selectbox("Whose report?", [m.name for m in everyone], index=None, placeholder="Choose your name")
        st.caption("Sign-in is off, so this relies on trust: open only your own report.")
        actor = dataclasses.replace(actor, member_name=me)
    if me and auth.can(actor, "view"):
        member = next((m for m in everyone if m.name == me), None)
        if member is None:
            ui.empty("Your team member record wasn't found.", "Ask your team leader to re-link your account.")
        elif not member.consent:
            ui.empty("You haven't opted in.", "Type !teampulse join in the team Discord to get a report.")
        else:
            report = contrib.build_report(conn, member, days)
            review = contrib.get_review(conn, me)
            if review:
                word = "shared it with the team" if review["status"] == "approved" else "asked to keep it hidden"
                st.markdown(ui.row(f"You {word} {ui.ago(review['updated_at'])}.", "", "ok" if review["status"] == "approved"
                                   else "warn"), unsafe_allow_html=True)
            with st.container(border=True):
                st.markdown(contrib.to_markdown(report))
            if auth.can(actor, "contrib.review_own"):
                note = st.text_area("Anything the data can't show?", value=(review or {}).get("comment") or "",
                                    max_chars=1000,
                                    placeholder="I designed the screens in Figma and pair-programmed the webhook with Aman.")
                a, b = st.columns(2)
                if a.button("Approve and share with the team", type="primary", icon=":material/visibility:"):
                    actions.review_own_report(conn, actor, "approved", note)
                    st.rerun()
                if b.button("Keep hidden, something's wrong", icon=":material/visibility_off:"):
                    actions.review_own_report(conn, actor, "disputed", note)
                    st.rerun()

with team:
    if not contrib.review_required():
        st.caption("Self-review is off (TEAMPULSE_CONTRIB_REVIEW=off), so every opted-in report is shown.")
    hidden = []
    for m in everyone:
        if not m.consent:
            hidden.append(f"{ui.e(m.name)} hasn't opted in.")
        elif not contrib.visible_to_team(conn, m):
            review = contrib.get_review(conn, m.name)
            why = "asked to keep it hidden" if review and review["status"] == "disputed" else "hasn't reviewed it yet"
            hidden.append(f"{ui.e(m.name)}'s report is hidden: {m.name} {why}.")
        else:
            with st.container(border=True):
                st.markdown(contrib.to_markdown(contrib.build_report(conn, m, days)))
    if hidden:
        st.markdown("".join(ui.row(h, "", "quiet") for h in hidden), unsafe_allow_html=True)
