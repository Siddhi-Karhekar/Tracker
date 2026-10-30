"""My work: each person's own workspace. Leaders can open anyone's; members see only their own."""
import streamlit as st

from teampulse import pm, pm_ui, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("workspace")
conn = get_agent().conn
people = pm.members(conn)

if pm.can_manage(actor):
    mine = actor.member_name if actor.member_name in people else None
    with st.sidebar:
        member = st.selectbox("Whose workspace", people, index=people.index(mine) if mine else 0,
                              help="As team leader you can open anyone's workspace. Members see only their own.") \
            if people else None
else:
    member = actor.member_name
if not member:
    ui.header("My work")
    ui.empty("Your account isn't linked to a team member yet.",
             "Your team leader links accounts to names in the team data when creating them (Team & access). "
             "Ask them to recreate yours linked to your name; then your tasks appear here.")
    st.stop()

own = (actor.member_name or "").lower() == member.lower()
ui.header("My work" if own else f"{member}'s work",
          "Everything assigned to you, what's late, what's next, and what's waiting on someone." if own else
          f"What {member} sees in their own workspace.")
pm_ui.flash()
w = pm.workspace(conn, member)
m = w["metrics"]
cols = st.columns(6)
for col, (label, value, help_) in zip(cols, [
        ("Open tasks", m["open"], "Assigned to you and not completed"),
        ("In progress", m["in_progress"], None),
        ("Overdue", m["overdue"], "Past the deadline and not completed"),
        ("Due in 7 days", m["due_soon"], None),
        ("Blocked", m["blocked"], None),
        ("Completed", m["completed"], f"{m['completed_this_week']} this week")]):
    col.metric(label, value, help=help_)
st.progress(m["progress"] / 100, text=f"Overall progress on your tasks: {m['progress']}%"
            + (f" · about {m['open_hours']:g} hours of estimated work open" if m["open_hours"] else ""))

if own:
    notes = pm.notifications(conn, actor)
    with st.expander(f"Notifications ({len(notes)} new)", expanded=bool(notes), icon=":material/notifications:"):
        if not notes:
            st.caption("You're all caught up.")
        for n in notes:
            st.markdown(ui.row(ui.e(n["text"]), meta=ui.e(ui.ago(n["created_at"]))
                               + (f" · task #{n['task_id']}" if n["task_id"] else "")), unsafe_allow_html=True)
        if notes and st.button("Mark all as read", icon=":material/done_all:"):
            pm.mark_read(conn, actor)
            st.rerun()

pm_ui.open_task_panel(conn, actor)

s = w["sections"]
focus, everything, requests = st.tabs(["Focus", "All tasks", "Requests"])
with focus:
    if not (s["overdue"] or s["due_soon"] or s["blocked"] or s["high_priority"]):
        ui.empty("Nothing urgent.", "No overdue, blocked or high-priority work, and nothing due in the next 7 days.")
    for key, title in (("overdue", "Overdue"), ("blocked", "Blocked"), ("due_soon", "Due in the next 7 days"),
                       ("high_priority", "High priority")):
        if s[key]:
            st.subheader(title)
            pm_ui.task_list(s[key], f"focus-{key}", show_people=False)
    if s["subtasks"]:
        st.subheader("Open subtasks")
        for t in s["subtasks"]:
            st.markdown(ui.row(f"#{t['id']} {ui.e(t['title'])}", meta=f"part of #{t['parent_id']}"),
                        unsafe_allow_html=True)
with everything:
    for key, title in (("in_progress", "In progress"), ("to_do", "To do and backlog"), ("review", "Under review"),
                       ("completed", "Completed"), ("recent", "Recently updated")):
        st.subheader(title)
        pm_ui.task_list(s[key], f"all-{key}", show_people=False)
with requests:
    if not w["requests"]:
        st.caption("No deadline requests. Open a task and use “Ask for a different deadline”.")
    for r in w["requests"]:
        word = {"pending": "waiting", "approved": "approved", "rejected": "declined"}[r["status"]]
        st.markdown(ui.row(f"#{r['task_id']} {ui.e(r['title'])} → {ui.e(r['proposed'])}", ui.e(r["reason"]),
                           {"pending": "warn", "approved": "ok", "rejected": "bad"}[r["status"]],
                           f"{word} · {ui.e(ui.ago(r['created_at']))}"), unsafe_allow_html=True)
