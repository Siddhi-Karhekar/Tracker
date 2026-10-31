"""Project board: every task in a project as a board or a list. Leaders plan here; everyone can open a task,
update their own, and comment."""
import pandas as pd
import streamlit as st

from teampulse import pm, pm_ui, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("board")
conn = get_agent().conn
if not conn.execute("SELECT 1 FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone():
    from teampulse import actions   # first open of a new database: read GitHub/Notion tasks once (Home does too)
    with st.spinner("Reading GitHub and Notion tasks for the first time…"):
        actions.first_build(conn, get_agent().index, get_agent().llm)
manage = pm.can_manage(actor)
ui.header("Project board", "Plan the work, see where every task stands, and open one to update it or comment.")
pm_ui.flash()

projects = pm.projects(conn)
if manage:
    with st.expander("New project", icon=":material/add:", expanded=not projects):
        with st.form("new-project", clear_on_submit=True, border=False):
            name = st.text_input("Project name", max_chars=120)
            description = st.text_area("What is it?", max_chars=2000)
            c1, c2 = st.columns(2)
            start, end = c1.date_input("Starts", value=None), c2.date_input("Ends", value=None)
            if st.form_submit_button("Create project", type="primary"):
                pm_ui._do(lambda: pm.create_project(conn, actor, name, description, start, end), "Project created.")
if not projects:
    ui.empty("No projects yet.", "The team leader creates the first project here." if not manage else
             "Create one above, then add tasks to it.")
    st.stop()

names = {p["id"]: p["name"] for p in projects}
with st.sidebar:
    project_id = st.selectbox("Project", list(names), format_func=names.get, key="pm_project")
    people = pm.members(conn)
    who = st.multiselect("Assigned to", people)
    only_mine = st.toggle("Only my tasks", value=False, disabled=not actor.member_name)
    priorities = st.multiselect("Priority", pm.PRIORITIES, format_func=pm.PRIORITY_LABEL.get)
    search = st.text_input("Search titles", max_chars=100)

stones = {m["id"]: m for m in pm.milestones(conn, project_id)}
everything = pm.tasks(conn, project_id=project_id)
if manage:
    with st.expander("New task", icon=":material/add_task:"):
        with st.form("new-task", clear_on_submit=True, border=False):
            title = st.text_input("Title", max_chars=200)
            description = st.text_area("Description", max_chars=4000)
            c1, c2, c3 = st.columns(3)
            assigned = c1.multiselect("Assign to", people)
            priority = c2.selectbox("Priority", pm.PRIORITIES, index=1, format_func=pm.PRIORITY_LABEL.get)
            status = c3.selectbox("Starts in", [s for s in pm.STATUSES if s != "done"], index=1,
                                  format_func=pm.STATUS_LABEL.get)
            c4, c5, c6, c7 = st.columns(4)
            due = c4.date_input("Deadline", value=None)
            milestone = c5.selectbox("Milestone", [None, *stones], format_func=lambda m: stones[m]["title"] if m else "None")
            estimate = c6.number_input("Estimate (hours)", 0.0, 1000.0, 0.0, step=1.0)
            weight = c7.selectbox("Size", pm.WEIGHTS, help="How big it is. Completion % counts finished tasks by size.")
            deps = st.multiselect("Waits for", [t["id"] for t in everything],
                                  format_func=lambda i: next(f"#{t['id']} {t['title']}" for t in everything if t["id"] == i))
            if st.form_submit_button("Create task", type="primary"):
                pm_ui._do(lambda: pm.create_task(conn, actor, project_id, title, description, assigned, priority, due,
                                                 status=status, milestone_id=milestone, estimate_hours=estimate or None,
                                                 depends_on=deps, weight=weight), "Task created.")
    with st.expander("New milestone", icon=":material/flag:"):
        with st.form("new-milestone", clear_on_submit=True, border=False):
            c1, c2 = st.columns([3, 1])
            mtitle, mdue = c1.text_input("Milestone", max_chars=120), c2.date_input("Due", value=None)
            if st.form_submit_button("Add milestone"):
                pm_ui._do(lambda: pm.add_milestone(conn, actor, project_id, mtitle, mdue), "Milestone added.")

if manage:
    pm_ui.ai_planner(conn, actor, project_id, get_agent().llm)

pm_ui.open_task_panel(conn, actor)

shown = everything
if who:
    shown = [t for t in shown if {a.lower() for a in t["assignees"]} & {w.lower() for w in who}]
if only_mine and actor.member_name:
    shown = [t for t in shown if pm.is_assignee(actor, t)]
if priorities:
    shown = [t for t in shown if t["priority"] in priorities]
if search.strip():
    shown = [t for t in shown if search.strip().lower() in t["title"].lower()]

done = sum(t["status"] == "done" for t in everything)
st.caption(f"{len(everything)} tasks · {done} completed"
           + (f" · showing {len(shown)} after filters" if len(shown) != len(everything) else ""))
board, table, month, timeline = st.tabs(["Board", "List", "Calendar", "Timeline"])
with board:
    columns = st.columns(len(pm.STATUSES))
    for col, status in zip(columns, pm.STATUSES):
        here = [t for t in shown if t["status"] == status]
        with col:
            st.markdown(f"**{pm.STATUS_LABEL[status]}** <span class='tp-meta'>{len(here)}</span>", unsafe_allow_html=True)
            pm_ui.task_list(here, f"board-{status}", empty="—", size=8)
with table:
    if shown:
        page = pm_ui.paginate(shown, "list", 50)
        st.dataframe(pd.DataFrame([{
            "#": t["id"], "Task": t["title"], "Status": pm.STATUS_LABEL[t["status"]],
            "Priority": pm.PRIORITY_LABEL[t["priority"]], "Assigned to": ", ".join(t["assignees"]),
            "Deadline": t["due_date"] or "", "Reported %": t["reported_progress"], "Milestone": stones.get(t["milestone_id"], {}).get("title", ""),
            "Size": t["weight"], "Verified %": t["verified_progress"],
            "Watch out": "; ".join(t["risks"])} for t in page]), hide_index=True, width="stretch")
        pick = st.selectbox("Open a task", [None, *[t["id"] for t in shown]],
                            format_func=lambda i: "Choose…" if i is None else
                            next(f"#{t['id']} {t['title']}" for t in shown if t["id"] == i), key="pm_pick")
        if pick and pick != st.session_state.get(pm_ui.OPEN_KEY):
            st.session_state[pm_ui.OPEN_KEY] = pick
            st.rerun()
    else:
        st.caption("No tasks match.")
with month:
    pm_ui.calendar_view(conn, "board-cal", project_id=project_id)
with timeline:
    pm_ui.timeline_view(shown, list(stones.values()))
