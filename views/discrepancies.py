"""Conflicts: where GitHub, Notion and chat disagree about a task, most serious first."""
import streamlit as st

from teampulse import actions, auth, discrepancies as disc, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("conflicts")
agent = get_agent()
conn = agent.conn

SEVERITY = {"high": "Serious", "medium": "Moderate", "low": "Minor"}
TYPE_LABEL = {"status": "Status", "stale_plan": "Notion is behind", "owner": "Owner", "deadline": "Deadline",
              "plan_outdated": "Plan page"}

ui.header("Where your sources disagree", "Tasks marked done that aren't, people assigned who aren't doing the "
          "work, dates that don't match, and plans that later decisions have overtaken.")

last = conn.execute("SELECT MAX(last_seen) FROM discrepancies").fetchone()[0]
can_edit = auth.can(actor, "data.edit")
with st.sidebar:
    st.caption(f"Last compared {ui.ago(last)}.")
    if auth.can(actor, "data.refresh"):
        use_ai = st.toggle("Use AI for the plan check", value=True, help="Off: free and offline, plan conflicts unverified")
        if st.button("Compare sources now", icon=":material/compare_arrows:", width="stretch"):
            with st.spinner("Reading new data and comparing sources…"):
                stats = actions.refresh_all(conn, actor, agent.index, agent.llm if use_ai else None)
            st.success(f"{stats['found']} conflicts, {stats['new']} new, {stats['resolved']} resolved.")
            last = "now"
if not last:
    with st.spinner("Comparing sources for the first time…"):
        actions.first_build(conn, agent.index, agent.llm)

c1, c2 = st.columns([2, 3], vertical_alignment="bottom")
view = c1.segmented_control("Show", ["Open", "Resolved", "Dismissed"], default="Open")
types = c2.pills("Kind", list(TYPE_LABEL), selection_mode="multi", format_func=TYPE_LABEL.get)
state = {"Open": "open", "Resolved": "resolved", "Dismissed": "dismissed"}[view or "Open"]
items = [d for d in disc.open_items(conn, (state,)) if not types or d["type"] in types]

if state == "open" and items:
    counts = [f"{sum(1 for d in items if d['severity'] == s)} {SEVERITY[s].lower()}" for s in SEVERITY]
    st.markdown(f"<p class='tp-meta'>{', '.join(counts)}.</p>", unsafe_allow_html=True)
if not items:
    if state == "open":
        ui.empty("No open conflicts.", "GitHub, Notion and chat agree on everything that was checked.")
    else:
        ui.empty("Nothing here.", f"No {state} conflicts yet.")

for d in items:
    tone = ui.SEVERITY_TONE[d["severity"]]
    with st.container(border=True):
        top, aside = st.columns([5, 2])
        badge = ui.pill(SEVERITY[d["severity"]], tone, filled=d["severity"] == "high")
        unverified = " <span class='tp-meta'>not checked by AI</span>" if not d["verified"] else ""
        top.markdown(f"{badge} <span class='tp-meta'>{TYPE_LABEL.get(d['type'], d['type'])}</span>{unverified}",
                     unsafe_allow_html=True)
        title = d["title"].split(" ", 1)[1] if d["title"].startswith("#") else d["title"]
        top.markdown(f"<h3 style='margin:.3rem 0 .6rem 0;font-size:1.2rem'>{ui.e(title)}</h3>", unsafe_allow_html=True)
        for col, side in zip(st.columns(len(d["sides"])), d["sides"]):
            link = f" <a href='{ui.url(side['url'])}' target='_blank'>open</a>" if side.get("url") else ""
            value = ui.STATUS_WORD.get(side["value"], side["value"])
            col.markdown(f"{ui.source_mark(side['source'])} <span class='tp-meta'>{ui.e(side['label'])}</span><br>"
                         f"<b>{ui.e(value)}</b><br><span class='tp-meta'>{ui.e((side.get('observed_at') or '')[:10])}</span>{link}",
                         unsafe_allow_html=True)
        st.markdown(f"<p style='margin:.6rem 0 0 0'><b>What to do:</b> {ui.e(d['suggestion'])}</p>", unsafe_allow_html=True)
        meta = f"Found {ui.ago(d['first_seen'])}"
        if d["resolved_at"]:
            meta += f", resolved {ui.ago(d['resolved_at'])}"
        if d["verdict"]:
            meta += f", marked {d['verdict'].replace('_', ' ')}" + (f" by {ui.e(d['dismissed_by'])}" if d["dismissed_by"] else "")
        aside.markdown(f"<p class='tp-meta' style='text-align:right'>{meta}.</p>", unsafe_allow_html=True)
        if state == "open" and can_edit:
            a, b = aside.columns(2)
            if a.button("Real", key=f"real-{d['id']}", icon=":material/check:", help="Confirm it's a real problem"):
                actions.set_verdict(conn, actor, d["id"], "real")
                st.rerun()
            if b.button("False alarm", key=f"fa-{d['id']}", icon=":material/close:",
                        help="Hide it while the values stay the same"):
                actions.set_verdict(conn, actor, d["id"], "false_alarm")
                st.rerun()
