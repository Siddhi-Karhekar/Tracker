"""Team dashboard (team leader): progress, workload, risks and everything waiting for a decision."""
import pandas as pd
import streamlit as st

from teampulse import pm, pm_ai, pm_monitor, pm_sync, pm_ui, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("dashboard")
conn = get_agent().conn
if not conn.execute("SELECT 1 FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone():
    from teampulse import actions   # first open of a new database: read GitHub/Notion tasks once (Home does too)
    with st.spinner("Reading GitHub and Notion tasks for the first time…"):
        actions.first_build(conn, get_agent().index, get_agent().llm)
ui.header("Team dashboard", "Where the project stands, what needs your decision, and what might slip.")
pm_ui.flash()

projects = pm.projects(conn)
if not projects:
    ui.empty("No projects yet.", "Create one on the Project board.")
    st.stop()
names = {0: "All projects", **{p["id"]: p["name"] for p in projects}}
with st.sidebar:
    project_id = st.selectbox("Project", list(names), format_func=names.get, index=1 if len(projects) == 1 else 0)
d = pm.dashboard(conn, project_id or None)

cols = st.columns(6)
for col, (label, value) in zip(cols, [("Tasks", d["total"]), ("Completed", f"{d['completion']}%"),
                                      ("In progress", d["by_status"]["in_progress"]), ("Under review", len(d["review"])),
                                      ("Blocked", d["by_status"]["blocked"]), ("Overdue", d["overdue"])]):
    col.metric(label, value, help=pm.COMPLETION_NOTE if label == "Completed" else None)

pm_ui.open_task_panel(conn, actor)

sync = pm_sync.overview(conn, project_id or None)
pm_monitor.run_checks(conn)   # cheap and idempotent; the poll loop runs it too
checkins = pm_monitor.alerts(conn, project_id or None)
decide, check, risk, sources, weekly, people, plan = st.tabs([
    f"Needs your decision ({len(d['review']) + len(d['requests'])})", f"Check-ins ({len(checkins)})",
    f"At risk ({len(d['at_risk'])})",
    f"GitHub & Notion ({len(sync['disagree'])})", "Weekly summary", "Workload", "Milestones & activity"])
with decide:
    if not (d["review"] or d["requests"]):
        ui.empty("Nothing waiting.", "Finished work and deadline requests appear here.")
    if d["review"]:
        st.subheader("Finished, waiting for your review")
        pm_ui.task_list(d["review"], "review")
    if d["requests"]:
        st.subheader("Deadline requests")
        for r in d["requests"]:
            st.markdown(ui.row(f"{ui.e(r['requested_by'])}: #{r['task_id']} {ui.e(r['title'])}",
                               ui.e(r["reason"]), "warn",
                               f"from {ui.e(r['due_date'] or 'no deadline')} to {ui.e(r['proposed'])}"),
                        unsafe_allow_html=True)
            pm_ui.request_buttons(conn, actor, r, "dash")
with check:
    pm_ui.checkins(conn, actor, checkins, manage=True)
with risk:
    if not d["at_risk"]:
        ui.empty("Nothing looks at risk.", "Overdue, blocked, nearly-due and unassigned tasks appear here.")
    pm_ui.task_list(d["at_risk"], "risk")
with sources:
    st.caption(f"{sync['linked']} planned task{'s' if sync['linked'] != 1 else ''} linked to GitHub or Notion. The plan "
               "here is the source of truth; GitHub, Notion and chat are evidence. Nothing is changed by itself.")
    if sync["disagree"]:
        st.subheader("Where the sources disagree with the plan")
        for item in sync["disagree"]:
            t = item["task"]
            st.markdown(ui.row(f"#{t['id']} {ui.e(t['title'])}",
                               "<br>".join(ui.e(x["text"]) for x in item["differences"]), "warn"),
                        unsafe_allow_html=True)
            if st.button(f"Open #{t['id']}", key=f"sync-open-{t['id']}", type="tertiary"):
                st.session_state[pm_ui.OPEN_KEY] = t["id"]
                st.rerun()
    elif sync["linked"]:
        ui.empty("Everything agrees.", "Linked tasks match what GitHub and Notion say.")
    st.subheader("In GitHub or Notion but not planned here")
    if not sync["unplanned"]:
        st.caption("Every GitHub issue and Notion task is planned here (or there are none yet: Source tasks → "
                   "Rebuild tasks reads them).")
    elif project_id:
        labels = {s["id"]: (f"#{s['github_number']} " if s["github_number"] else "Notion: ") + (s["title"] or s["id"])
                  for s in sync["unplanned"]}
        picked = st.multiselect("Add to this project", list(labels), format_func=labels.get, key="import-pick")
        st.caption("Each becomes a planned task, linked, with its title, owners who are on the team, deadline and "
                   "status filled in from GitHub and Notion. You can edit everything afterwards.")
        if st.button("Add to project", disabled=not picked, icon=":material/playlist_add:"):
            pm_ui._do(lambda: pm_sync.import_tasks(conn, actor, project_id, picked),
                      f"Added {len(picked)} task{'s' if len(picked) != 1 else ''}.")
    else:
        for s_ in sync["unplanned"]:
            st.markdown(ui.row(ui.e(s_["title"] or s_["id"]), meta=f"<a href='{ui.url(s_['url'])}' target='_blank' "
                                                                  "rel='noopener noreferrer'>open</a>"),
                        unsafe_allow_html=True)
        st.caption("Choose a project in the sidebar to add these to it.")
with weekly:
    if not project_id:
        st.caption("Choose a project in the sidebar to write its weekly summary.")
    else:
        st.caption("Written from this week's facts only (completed work, risks, reviews, requests, milestones); every "
                   "statement cites its task. If the AI judges people or mentions anything not in the facts, the "
                   "rule-based summary is used instead. Nothing is sent anywhere.")
        if st.button("Write this week's summary", icon=":material/summarize:"):
            pm_ui._do(lambda: pm_ai.weekly_summary(conn, actor, project_id, get_agent().llm), "Summary written.")
        latest = pm_ai.latest_summary(conn, project_id)
        if latest:
            how = "the rules (no AI answered)" if latest["method"] == "rules" else f"AI ({latest['method']})"
            st.markdown(f"<span class='tp-meta'>Written {ui.e(ui.ago(latest['created_at']))} by "
                        f"{ui.e(how)} for {ui.e(latest['created_by'])}</span>", unsafe_allow_html=True)
            for note in latest.get("notes") or []:
                st.info(note)
            with st.container(border=True):
                st.markdown(ui.md_safe(latest["text"]))
            with st.expander(f"The facts it was written from ({len(latest['facts'])})"):
                for f in latest["facts"]:
                    st.markdown("- " + ui.md_text(f))
with people:
    st.caption("Open work per person, in alphabetical order. It shows who may be overloaded; it is not a ranking "
               "and says nothing about effort. The Contributions page has the evidence-based picture.")
    st.dataframe(pd.DataFrame([{"Member": w["member"], "Open": w["open"], "In progress": w["in_progress"],
                                "Blocked": w["blocked"], "Overdue": w["overdue"], "Completed": w["completed"],
                                "Estimated hours open": w["open_hours"]} for w in d["workload"]]),
                 hide_index=True, width="stretch")
    status_counts = pd.DataFrame({"Tasks": [d["by_status"][s] for s in pm.STATUSES]},
                                 index=[pm.STATUS_LABEL[s] for s in pm.STATUSES])
    st.bar_chart(status_counts, horizontal=True, height=240)
with plan:
    st.subheader("Upcoming milestones")
    if not d["milestones"]:
        st.caption("No upcoming milestones.")
    for m in d["milestones"]:
        st.markdown(ui.row(ui.e(m["title"]), f"{m['completion']}% complete by size · {m['done']} of {m['tasks']} "
                                             f"tasks completed",
                           "ok" if m["tasks"] and m["done"] == m["tasks"] else "quiet",
                           f"{ui.e(m['project'])} · due {ui.e(m['due_date'] or 'not set')}"), unsafe_allow_html=True)
    st.subheader("Recent activity")
    for h in d["activity"]:
        st.markdown(f"<span class='tp-meta'>{ui.e(ui.ago(h['at']))}</span> <b>{ui.e(h['actor'] or '')}</b> "
                    f"{ui.e(pm_ui._history_text(h))} on #{h['task_id']} {ui.e(h['title'])}", unsafe_allow_html=True)
