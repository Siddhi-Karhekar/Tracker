"""Shared pieces for the project pages (My work, Project board, Team dashboard): task cards and the task panel.

Every value a person typed (titles, descriptions, comments, blocker reasons) is escaped with ui.e before it goes
into HTML, and every change goes through teampulse.pm, which checks permission on the server. Hiding a button
here is only a convenience.
"""
from __future__ import annotations

from datetime import date

import streamlit as st

from teampulse import auth, pm, ui
from teampulse.auth import Actor

STATUS_TONE = {"backlog": "quiet", "todo": "quiet", "in_progress": "info", "review": "info", "blocked": "bad",
               "done": "ok"}
PRIORITY_TONE = {"low": "quiet", "medium": "quiet", "high": "warn", "urgent": "bad"}
OPEN_KEY = "pm_open"


def status_pill(status: str) -> str:
    return ui.pill(pm.STATUS_LABEL.get(status, status), STATUS_TONE.get(status, "quiet"))


def priority_pill(priority: str) -> str:
    return ui.pill(pm.PRIORITY_LABEL.get(priority, priority), PRIORITY_TONE.get(priority, "quiet"),
                   filled=priority == "urgent")


def due_text(t: dict) -> str:
    if not t.get("due_date"):
        return "no deadline"
    left = t.get("days_left")
    if t["status"] == "done":
        return f"was due {t['due_date']}"
    if left is None:
        return f"due {t['due_date']}"
    if left < 0:
        return f"{-left} day{'s' if left != -1 else ''} late ({t['due_date']})"
    if left == 0:
        return "due today"
    return f"due in {left} day{'s' if left != 1 else ''} ({t['due_date']})"


def progress_text(t: dict) -> str:
    measured = t.get("measured_progress")
    out = f"reported {t['reported_progress']}%"
    if measured is not None:
        sub = f", {t['subtasks_done']}/{t['subtasks_total']} subtasks" if t.get("subtasks_total") else ""
        out += f" · measured {measured}%{sub}"
    return out


def tone(t: dict) -> str:
    if t["status"] == "done":
        return "ok"
    if t.get("overdue") or t["status"] == "blocked":
        return "bad"
    return "warn" if t.get("risks") else "quiet"


def card_html(t: dict, show_people: bool = True) -> str:
    title = f"#{t['id']} {ui.e(t['title'])}"
    pills = " ".join([status_pill(t["status"]), priority_pill(t["priority"])])
    people = ", ".join(t["assignees"]) or "unassigned"
    meta = " · ".join(filter(None, [ui.e(due_text(t)), ui.e(progress_text(t)),
                                    ui.e(people) if show_people else ""]))
    body = pills + (f"<br>{ui.e('; '.join(t['risks']))}" if t.get("risks") else "")
    return ui.row(title, body, tone(t), meta)


def task_list(tasks: list[dict], key: str, show_people: bool = True, empty: str = "Nothing here.") -> None:
    if not tasks:
        st.caption(empty)
        return
    for t in tasks:
        st.markdown(card_html(t, show_people), unsafe_allow_html=True)
        if st.button(f"Open #{t['id']}", key=f"{key}-open-{t['id']}", type="tertiary"):
            st.session_state[OPEN_KEY] = t["id"]
            st.rerun()


def _do(fn, done: str) -> None:
    """Run a change; show a friendly message for refusals and rule violations, then reload."""
    try:
        fn()
    except (auth.AuthError, auth.PermissionDenied) as exc:
        st.error(str(exc))
        return
    st.session_state["pm_flash"] = done
    st.rerun()


def flash() -> None:
    message = st.session_state.pop("pm_flash", None)
    if message:
        st.success(message)


def _date_value(iso: str | None) -> date | None:
    return date.fromisoformat(iso) if iso else None


def task_panel(conn, actor: Actor, task_id: int) -> None:
    """Everything about one task, with the actions this person is allowed to take."""
    try:
        t = pm.task_detail(conn, task_id)
    except pm.PMError as exc:
        st.session_state.pop(OPEN_KEY, None)
        st.info(str(exc))
        return
    manage, work = pm.can_manage(actor), pm.can_work_on(actor, t)
    commenter = auth.can(actor, "pm.work")
    with st.container(border=True):
        top, close = st.columns([6, 1])
        top.markdown(f"<div class='tp-meta'>{ui.e(t['project'])}"
                     + (f" · milestone {ui.e(t['milestone'])}" if t["milestone"] else "") + "</div>"
                     f"<h3 style='margin:.1rem 0 .4rem 0'>#{t['id']} {ui.e(t['title'])}</h3>"
                     f"{status_pill(t['status'])} {priority_pill(t['priority'])} "
                     f"<span class='tp-meta'>{ui.e(due_text(t))} · {ui.e(', '.join(t['assignees']) or 'unassigned')}"
                     + (f" · estimate {t['estimate_hours']:g} h" if t["estimate_hours"] else "") + "</span>",
                     unsafe_allow_html=True)
        if close.button("Close", key=f"close-{t['id']}", icon=":material/close:"):
            st.session_state.pop(OPEN_KEY, None)
            st.rerun()

        a, b = st.columns(2)
        a.progress(t["reported_progress"] / 100, text=f"Reported by assignee: {t['reported_progress']}%")
        if t["measured_progress"] is None:
            b.caption("Measured progress: add subtasks to measure it. It becomes 100% when the leader marks the "
                      "task Completed.")
        else:
            b.progress(t["measured_progress"] / 100, text=f"Measured (subtasks and approval): {t['measured_progress']}%")
        if t["description"]:
            st.markdown(ui.e(t["description"]).replace("\n", "<br>"), unsafe_allow_html=True)
        if t["risks"]:
            st.markdown(ui.row("Watch out", ui.e("; ".join(t["risks"])), "warn"), unsafe_allow_html=True)
        if t["dependencies"] or t["dependents"]:
            parts = []
            if t["dependencies"]:
                parts.append("Waits for: " + ", ".join(f"#{d['id']} {ui.e(d['title'])} {status_pill(d['status'])}"
                                                       for d in t["dependencies"]))
            if t["dependents"]:
                parts.append("Needed by: " + ", ".join(f"#{d['id']} {ui.e(d['title'])}" for d in t["dependents"]))
            st.markdown("<br>".join(parts), unsafe_allow_html=True)

        _subtasks(conn, actor, t, work)
        if manage and t["status"] == "review":
            _review(conn, actor, t)
        if work:
            _update_form(conn, actor, t, manage)
        if manage:
            _requests(conn, actor, t)
            _edit_form(conn, actor, t)
        elif not work:
            st.caption("This task isn't assigned to you, so you can read and comment but not change it.")
        _comments(conn, actor, t, commenter)
        with st.expander(f"History ({len(t['history'])})"):
            for h in t["history"]:
                st.markdown(f"<span class='tp-meta'>{ui.e(h['at'][:16].replace('T', ' '))}</span> "
                            f"<b>{ui.e(h['actor'] or '')}</b> {ui.e(_history_text(h))}", unsafe_allow_html=True)


def _history_text(h: dict) -> str:
    field, old, new = h["field"], h["old_value"], h["new_value"]
    if field == "created":
        return "created the task"
    if field == "status":
        return f"moved it from {pm.STATUS_LABEL.get(old, old)} to {pm.STATUS_LABEL.get(new, new)}"
    if field == "reported_progress":
        return f"set progress {old}% → {new}%"
    if field == "deadline_request":
        return f"asked to move the deadline {old or 'none'} → {new}"
    return f"changed {field.replace('_', ' ')}: {old or '—'} → {new or '—'}"


def _subtasks(conn, actor: Actor, t: dict, work: bool) -> None:
    st.markdown(f"**Subtasks** <span class='tp-meta'>{t['subtasks_done']}/{t['subtasks_total']} done</span>",
                unsafe_allow_html=True)
    for s in t["subtasks"]:
        done = s["status"] == "done"
        ticked = st.checkbox(s["title"], value=done, key=f"sub-{s['id']}-{s['version']}", disabled=not work)
        if ticked != done:
            _do(lambda s=s, ticked=ticked: pm.set_status(conn, actor, s["id"], "done" if ticked else "todo", s["version"]),
                "Subtask updated.")
    if work:
        with st.form(f"add-sub-{t['id']}", clear_on_submit=True, border=False):
            c1, c2 = st.columns([4, 1])
            title = c1.text_input("New subtask", max_chars=200, label_visibility="collapsed",
                                  placeholder="Add a subtask")
            if c2.form_submit_button("Add subtask"):
                _do(lambda: pm.create_task(conn, actor, t["project_id"], title, parent_id=t["id"]), "Subtask added.")


def _update_form(conn, actor: Actor, t: dict, manage: bool) -> None:
    choices = [s for s in pm.STATUSES if manage or t["parent_id"] or s != "done"]
    if t["status"] not in choices:
        choices.append(t["status"])
    with st.form(f"update-{t['id']}"):
        st.markdown("**Update your progress**" if not manage else "**Update status and progress**")
        c1, c2 = st.columns(2)
        status = c1.selectbox("Status", choices, index=choices.index(t["status"]),
                              format_func=pm.STATUS_LABEL.get, key=f"status-{t['id']}")
        progress = c2.slider("Progress you'd report", 0, 100, int(t["reported_progress"]), step=5,
                             key=f"progress-{t['id']}")
        note = st.text_input("Note (required when you choose Blocked)", max_chars=1000, key=f"note-{t['id']}")
        if not manage and not t["parent_id"]:
            st.caption("When you've finished, choose Under review. The team leader marks it Completed.")
        if st.form_submit_button("Save update", type="primary"):
            def save():
                version = t["version"]
                if progress != t["reported_progress"]:
                    pm.set_progress(conn, actor, t["id"], progress, version)
                    version += 1
                if status != t["status"]:
                    pm.set_status(conn, actor, t["id"], status, version, note)
                elif note:
                    pm.add_comment(conn, actor, t["id"], note)
            _do(save, "Saved.")
    if not manage:
        with st.expander("Ask for a different deadline"):
            with st.form(f"deadline-{t['id']}", border=False):
                new = st.date_input("New deadline", value=_date_value(t["due_date"]), key=f"newdue-{t['id']}")
                reason = st.text_input("Why", max_chars=1000, key=f"why-{t['id']}")
                if st.form_submit_button("Send request"):
                    _do(lambda: pm.request_deadline(conn, actor, t["id"], new, reason),
                        "Request sent to the team leader.")


def _review(conn, actor: Actor, t: dict) -> None:
    st.markdown("**Waiting for your review**")
    c1, c2 = st.columns(2)
    if c1.button("Approve and mark Completed", key=f"approve-{t['id']}", type="primary", icon=":material/task_alt:"):
        _do(lambda: pm.set_status(conn, actor, t["id"], "done", t["version"]), "Marked completed.")
    with c2.popover("Send back", icon=":material/undo:"):
        note = st.text_input("What still needs doing?", key=f"sendback-{t['id']}", max_chars=1000)
        if st.button("Send back to In progress", key=f"sendback-go-{t['id']}"):
            _do(lambda: pm.set_status(conn, actor, t["id"], "in_progress", t["version"], note), "Sent back.")


def _requests(conn, actor: Actor, t: dict) -> None:
    for r in [r for r in t["requests"] if r["status"] == "pending"]:
        st.markdown(ui.row(f"{ui.e(r['requested_by'])} asks to move the deadline to {ui.e(r['proposed'])}",
                           ui.e(r["reason"]), "warn"), unsafe_allow_html=True)
        request_buttons(conn, actor, r, f"task-{t['id']}")


def request_buttons(conn, actor: Actor, r: dict, key: str) -> None:
    c1, c2, _ = st.columns([1, 1, 3])
    if c1.button("Approve", key=f"{key}-req-ok-{r['id']}"):
        _do(lambda: pm.decide_request(conn, actor, r["id"], True), "Deadline moved.")
    if c2.button("Decline", key=f"{key}-req-no-{r['id']}"):
        _do(lambda: pm.decide_request(conn, actor, r["id"], False), "Request declined.")


def _edit_form(conn, actor: Actor, t: dict) -> None:
    with st.expander("Edit task (team leader)"):
        people = pm.members(conn)
        stones = {m["id"]: m["title"] for m in pm.milestones(conn, t["project_id"])}
        others = {o["id"]: f"#{o['id']} {o['title']}" for o in pm.tasks(conn, project_id=t["project_id"])
                  if o["id"] != t["id"]}
        with st.form(f"edit-{t['id']}", border=False):
            title = st.text_input("Title", t["title"], max_chars=200)
            description = st.text_area("Description", t["description"], max_chars=4000)
            c1, c2, c3 = st.columns(3)
            priority = c1.selectbox("Priority", pm.PRIORITIES, index=pm.PRIORITIES.index(t["priority"]),
                                    format_func=pm.PRIORITY_LABEL.get)
            due = c2.date_input("Deadline", value=_date_value(t["due_date"]))
            estimate = c3.number_input("Estimate (hours)", 0.0, 1000.0, float(t["estimate_hours"] or 0), step=1.0)
            if t["parent_id"]:
                assigned, milestone, deps = None, t["milestone_id"], None
            else:
                assigned = st.multiselect("Assigned to", people, default=[a for a in t["assignees"] if a in people])
                milestone = st.selectbox("Milestone", [None, *stones], format_func=lambda m: stones.get(m, "None"),
                                         index=([None, *stones].index(t["milestone_id"])
                                                if t["milestone_id"] in stones else 0))
                deps = st.multiselect("Waits for", list(others), format_func=others.get,
                                      default=[d["id"] for d in t["dependencies"] if d["id"] in others])
            if st.form_submit_button("Save changes", type="primary"):
                changes = {"title": title, "description": description, "priority": priority, "due_date": due,
                           "estimate_hours": estimate or None, "milestone_id": milestone}
                if assigned is not None:
                    changes.update(assignees=assigned, depends_on=deps)
                _do(lambda: pm.update_task(conn, actor, t["id"], t["version"], **changes), "Task saved.")
        if st.button("Archive this task", key=f"archive-{t['id']}", icon=":material/archive:"):
            def archive():
                pm.archive_task(conn, actor, t["id"])
                st.session_state.pop(OPEN_KEY, None)
            _do(archive, "Task archived.")


def _comments(conn, actor: Actor, t: dict, can_comment: bool) -> None:
    st.markdown(f"**Comments** <span class='tp-meta'>{len(t['comments'])}</span>", unsafe_allow_html=True)
    for c in t["comments"]:
        kind = " " + ui.pill("blocker", "bad") if c["kind"] == "blocker" else ""
        st.markdown(ui.row(f"{ui.e(c['author'])}{kind}", ui.e(c["body"]).replace("\n", "<br>"),
                           "bad" if c["kind"] == "blocker" else "quiet", ui.e(ui.ago(c["created_at"]))),
                    unsafe_allow_html=True)
    if can_comment:
        with st.form(f"comment-{t['id']}", clear_on_submit=True, border=False):
            body = st.text_area("Add a comment", max_chars=2000, placeholder="Write @Name to notify someone",
                                key=f"comment-body-{t['id']}")
            if st.form_submit_button("Comment"):
                _do(lambda: pm.add_comment(conn, actor, t["id"], body), "Comment added.")


def open_task_panel(conn, actor: Actor) -> None:
    """Show the task someone opened, at the top of the page."""
    task_id = st.session_state.get(OPEN_KEY)
    if task_id:
        task_panel(conn, actor, task_id)
