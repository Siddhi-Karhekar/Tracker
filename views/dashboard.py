"""Team dashboard (team leader): progress, workload, risks and everything waiting for a decision."""
import pandas as pd
import streamlit as st

from teampulse import pm, pm_ui, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("dashboard")
conn = get_agent().conn
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

decide, risk, people, plan = st.tabs([f"Needs your decision ({len(d['review']) + len(d['requests'])})",
                                      f"At risk ({len(d['at_risk'])})", "Workload", "Milestones & activity"])
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
with risk:
    if not d["at_risk"]:
        ui.empty("Nothing looks at risk.", "Overdue, blocked, nearly-due and unassigned tasks appear here.")
    pm_ui.task_list(d["at_risk"], "risk")
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
