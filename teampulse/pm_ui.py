"""Shared pieces for the project pages (My work, Project board, Team dashboard): task cards and the task panel.

Every value a person typed (titles, descriptions, comments, blocker reasons) is escaped with ui.e before it goes
into HTML, and every change goes through teampulse.pm, which checks permission on the server. Hiding a button
here is only a convenience.
"""
from __future__ import annotations

import calendar as calendar_lib
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from teampulse import auth, pm, pm_ai, pm_monitor, pm_sync, ui
from teampulse.auth import Actor

STATUS_TONE = {"backlog": "quiet", "todo": "quiet", "in_progress": "info", "review": "info", "blocked": "bad",
               "done": "ok"}
PRIORITY_TONE = {"low": "quiet", "medium": "quiet", "high": "warn", "urgent": "bad"}
OPEN_KEY = "pm_open"
PAGE_SIZE = 15


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
    out = f"reported {t['reported_progress']}%"
    if t.get("subtasks_total"):
        out += f" · {t['subtasks_done']}/{t['subtasks_total']} subtasks"
    if t.get("verified_progress") is not None:
        out += f" · verified {t['verified_progress']}%"
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


def paginate(items: list, key: str, size: int = PAGE_SIZE) -> list:
    """Show long lists a page at a time; returns the items on the current page."""
    if len(items) <= size:
        return items
    pages = (len(items) + size - 1) // size
    page = st.number_input(f"Page (of {pages})", 1, pages, 1, key=f"{key}-page", width=140)
    st.caption(f"Showing {(page - 1) * size + 1}–{min(page * size, len(items))} of {len(items)}")
    return items[(page - 1) * size: page * size]


def task_list(tasks: list[dict], key: str, show_people: bool = True, empty: str = "Nothing here.",
              size: int = PAGE_SIZE) -> None:
    if not tasks:
        st.caption(empty)
        return
    for t in paginate(tasks, key, size):
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

        a, b, c = st.columns(3)
        a.progress(t["reported_progress"] / 100, text=f"Self-reported: {t['reported_progress']}%")
        if t["subtasks_total"]:
            sub = round(100 * t["subtasks_done"] / t["subtasks_total"])
            b.progress(sub / 100, text=f"Subtasks done: {t['subtasks_done']}/{t['subtasks_total']}")
        else:
            b.caption("Subtasks: none yet")
        if t["verified_progress"] is None:
            c.caption("Verified: not checked yet. Only the team leader verifies progress.")
        else:
            c.progress(t["verified_progress"] / 100, text=f"Verified: {t['verified_progress']}%")
            c.caption(f"Checked by {t['verified_by']}, {ui.ago(t['verified_at'])}.")
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

        if not t["parent_id"]:
            _sources(conn, actor, t, work, manage)
        _subtasks(conn, actor, t, work)
        if manage and t["status"] == "review":
            _review(conn, actor, t)
        if manage and not t["parent_id"] and t["status"] != "done":
            _verify_form(conn, actor, t)
        if work:
            _update_form(conn, actor, t, manage)
        if manage:
            _requests(conn, actor, t)
            _edit_form(conn, actor, t)
        elif not work:
            st.caption("This task isn't assigned to you, so you can read and comment but not change it.")
        _updates(t)
        _comments(conn, actor, t, commenter)
        with st.expander(f"History ({len(t['history'])})"):
            for h in t["history"]:
                st.markdown(f"<span class='tp-meta'>{ui.e(h['at'][:16].replace('T', ' '))}</span> "
                            f"<b>{ui.e(h['actor'] or '')}</b> {ui.e(_history_text(h))}", unsafe_allow_html=True)


ROLE_WORD = {"pr": "pull request", "commit": "commit", "review": "review", "comment": "comment", "chat": "chat",
             "issue": "issue", "notion_task": "Notion page"}


def _sources(conn, actor: Actor, t: dict, work: bool, manage: bool) -> None:
    """What GitHub, Notion and chat say about this task. Shown, compared, never applied by itself."""
    v = pm_sync.view(conn, t)
    label = "GitHub & Notion" + (f" ({len(v['differences'])} to check)" if v and not v["missing"] and v["differences"]
                                 else "" if v else " (not linked)")
    with st.expander(label, icon=":material/sync_alt:", expanded=bool(v and not v["missing"] and v["differences"])):
        if v is None:
            st.caption("Link this task to its GitHub issue or Notion page to see commits, pull requests and chat "
                       "about it here, and to be told when they disagree with the plan.")
            if work:
                _link_picker(conn, actor, t)
            return
        if v["missing"]:
            st.warning(f"The linked item “{v['link']['source_title']}” is no longer in GitHub or Notion. Rebuild "
                       "source tasks, or link another one.")
        else:
            src = v["source"]
            st.markdown(f"Linked to <a href='{ui.url(src['url'])}' target='_blank' rel='noopener noreferrer'>"
                        f"{ui.e(src['title'])}</a> <span class='tp-meta'>by {ui.e(v['link']['linked_by'] or '')}</span>",
                        unsafe_allow_html=True)
            for d in v["differences"]:
                st.markdown(ui.row(ui.e(d["text"]), tone="warn"), unsafe_allow_html=True)
                if manage and d["kind"] == "deadline":
                    if st.button(f"Use {pm_sync.SOURCES[d['source']]}'s deadline ({d['value']})",
                                 key=f"use-due-{t['id']}-{d['source']}"):
                        _do(lambda d=d: pm_sync.use_source_deadline(conn, actor, t["id"], d["source"], t["version"]),
                            "Deadline updated.")
            if v["differences"]:
                st.caption("TeamPulse's plan is the source of truth: these are shown, never applied by themselves.")
            rows = []
            for source in pm_sync.SOURCES:
                cells = [v["card"][a].get(source) for a in ("status", "owner", "deadline")]
                if any(cells):
                    rows.append(f"<tr><td>{ui.source_mark(source)}</td><td>{ui.status_pill((cells[0] or {}).get('value'))}"
                                f"</td><td>{ui.e((cells[1] or {}).get('value') or '—')}</td>"
                                f"<td>{ui.e(((cells[2] or {}).get('value') or '—')[:10])}</td></tr>")
            if rows:
                st.markdown("<table class='tp-strip'><tr><th>Source</th><th>Status</th><th>Owner</th><th>Deadline</th>"
                            "</tr>" + "".join(rows) + "</table>", unsafe_allow_html=True)
            counts = ", ".join(f"{n} {ROLE_WORD[r]}{'s' if n != 1 else ''}" for r, n in v["counts"].items()
                               if n and r in ("pr", "commit", "review", "chat"))
            who = ", ".join(f"{w} ({n})" for w, n in sorted(v["workers"].items()))
            st.caption("Evidence: " + (counts or "nothing linked yet") + (f" · work by {who}" if who else "")
                       + f" · last activity {ui.ago(v['last_activity'])}. Evidence is not verification: the leader "
                       "verifies progress.")
            for ev in v["evidence"][:8]:
                link = (f"<a href='{ui.url(ev['url'])}' target='_blank' rel='noopener noreferrer'>open</a>"
                        if ev["url"] else "")
                st.markdown(f"<span class='tp-meta'>{ui.e(ROLE_WORD.get(ev['role'], ev['role']))} · "
                            f"{ui.e(ev['who'])} · {ui.e(ui.ago(ev['at']))}</span> {ui.e(ev['title'][:100])} {link}",
                            unsafe_allow_html=True)
        if work and st.button("Unlink", key=f"unlink-{t['id']}", icon=":material/link_off:"):
            _do(lambda: pm_sync.unlink(conn, actor, t["id"]), "Unlinked.")


def _link_picker(conn, actor: Actor, t: dict) -> None:
    for s in pm_sync.suggestions(conn, t):
        if st.button(f"Link to “{s['title']}”" + (f" (#{s['github_number']})" if s["github_number"] else ""),
                     key=f"suggest-{t['id']}-{s['id']}", icon=":material/link:"):
            _do(lambda s=s: pm_sync.link(conn, actor, t["id"], s["id"]), "Linked.")
    taken = {r[0] for r in conn.execute("SELECT source_task FROM pm_source_links")}
    options = {s["id"]: (f"#{s['github_number']} " if s["github_number"] else "Notion: ") + (s["title"] or s["id"])
               for s in pm_sync.source_tasks(conn) if s["id"] not in taken}
    if not options:
        st.caption("No unlinked GitHub issues or Notion tasks. They appear after the sources are read (Source tasks → "
                   "Rebuild tasks).")
        return
    c1, c2 = st.columns([4, 1])
    choice = c1.selectbox("Or pick one", [None, *options], format_func=lambda i: "Choose…" if i is None else options[i],
                          key=f"link-pick-{t['id']}")
    if c2.button("Link", key=f"link-go-{t['id']}", disabled=choice is None):
        _do(lambda: pm_sync.link(conn, actor, t["id"], choice), "Linked.")


def _history_text(h: dict) -> str:
    field, old, new = h["field"], h["old_value"], h["new_value"]
    if field == "created":
        return "created the task"
    if field == "status":
        return f"moved it from {pm.STATUS_LABEL.get(old, old)} to {pm.STATUS_LABEL.get(new, new)}"
    if field == "reported_progress":
        return f"set progress {old}% → {new}%"
    if field == "linked_to":
        return f"linked it to “{new}”" if new else f"unlinked it from “{old}”"
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


def _draft_button(conn, actor: Actor, t: dict) -> None:
    """Writes a suggested note into the form from linked commits, PRs, chat and finished subtasks. Saves nothing."""
    if st.button("Draft my update from GitHub and subtasks", key=f"draft-{t['id']}", icon=":material/edit_note:",
                 help="Uses only what's linked to this task since your last update. You edit it and save it yourself."):
        from teampulse.webapp import get_agent
        try:
            draft = pm_ai.draft_update(conn, actor, t["id"], get_agent().llm)
        except (auth.AuthError, auth.PermissionDenied) as exc:
            st.info(str(exc))
            return
        st.session_state[f"note-{t['id']}"] = draft["text"]
        st.session_state[f"draft-src-{t['id']}"] = draft
        st.rerun()
    src = st.session_state.get(f"draft-src-{t['id']}")
    if src:
        how = "rules" if src["method"] == "rules" else f"AI ({src['method']})"
        st.caption(f"Draft by {how} from {len(src['evidence'])} item{'s' if len(src['evidence']) != 1 else ''} since "
                   f"{src['since'][:10]}. Check it, change it, then Save update. Your progress number is yours to set.")


def _update_form(conn, actor: Actor, t: dict, manage: bool) -> None:
    if not t["parent_id"]:
        _draft_button(conn, actor, t)
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
        note = st.text_input("What did you do? (for Blocked: what's blocking it)", max_chars=1000,
                             key=f"note-{t['id']}")
        evidence = st.text_input("Evidence link (optional): a pull request, commit, doc or design",
                                 max_chars=500, key=f"evidence-{t['id']}", placeholder="https://")
        if not manage and not t["parent_id"]:
            st.caption("When you've finished, choose Under review. The team leader marks it Completed.")
        if st.form_submit_button("Save update", type="primary"):
            def save():
                version = t["version"]
                blocking = status == "blocked" and status != t["status"]
                if progress != t["reported_progress"] or evidence.strip() or (note and not blocking
                                                                             and status == t["status"]):
                    pm.set_progress(conn, actor, t["id"], progress, version, "" if blocking else note, evidence)
                    version += 1
                    used_note = not blocking
                else:
                    used_note = False
                if status != t["status"]:
                    pm.set_status(conn, actor, t["id"], status, version, "" if used_note else note)
            _do(save, "Saved.")
    if not manage:
        with st.expander("Ask for a different deadline"):
            with st.form(f"deadline-{t['id']}", border=False):
                new = st.date_input("New deadline", value=_date_value(t["due_date"]), key=f"newdue-{t['id']}")
                reason = st.text_input("Why", max_chars=1000, key=f"why-{t['id']}")
                if st.form_submit_button("Send request"):
                    _do(lambda: pm.request_deadline(conn, actor, t["id"], new, reason),
                        "Request sent to the team leader.")


def _verify_form(conn, actor: Actor, t: dict) -> None:
    with st.expander("Verify progress (team leader)"):
        st.caption("After looking at the work (or its evidence links), record how far it really is. This is kept "
                   "separate from the assignee's own number. Approving the task in review sets it to 100%.")
        with st.form(f"verify-{t['id']}", border=False):
            start = t["verified_progress"] if t["verified_progress"] is not None else t["reported_progress"]
            value = st.slider("Verified progress", 0, 100, int(start), step=5, key=f"verified-{t['id']}")
            note = st.text_input("What you checked", max_chars=1000, key=f"verify-note-{t['id']}")
            if st.form_submit_button("Save verified progress"):
                _do(lambda: pm.verify_progress(conn, actor, t["id"], value, note, t["version"]),
                    "Verified progress saved.")


def _updates(t: dict) -> None:
    if not t["updates"]:
        return
    with st.expander(f"Progress updates ({len(t['updates'])})", expanded=False):
        for u in t["updates"]:
            label = "verified" if u["kind"] == "verified" else "reported"
            link = (f" · <a href='{ui.url(u['evidence_url'])}' target='_blank' rel='noopener noreferrer'>evidence</a>"
                    if u["evidence_url"] else "")
            badge = ui.pill(f"{label} {u['progress']}%", "ok" if label == "verified" else "quiet")
            st.markdown(ui.row(f"{ui.e(u['author'])} {badge}", ui.e(u["note"]), "ok" if label == "verified" else "quiet",
                               ui.e(ui.ago(u["created_at"])) + link), unsafe_allow_html=True)


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
            c1, c2, c3, c4 = st.columns(4)
            priority = c1.selectbox("Priority", pm.PRIORITIES, index=pm.PRIORITIES.index(t["priority"]),
                                    format_func=pm.PRIORITY_LABEL.get)
            due = c2.date_input("Deadline", value=_date_value(t["due_date"]))
            estimate = c3.number_input("Estimate (hours)", 0.0, 1000.0, float(t["estimate_hours"] or 0), step=1.0)
            weight = c4.selectbox("Size", pm.WEIGHTS, index=pm.WEIGHTS.index(t["weight"] or 1),
                                  help="How big this task is. Completion % counts finished tasks by size.")
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
                           "estimate_hours": estimate or None, "milestone_id": milestone, "weight": weight}
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


# ---- calendar and timeline ---------------------------------------------------------------------------------

CAL_CSS = f"""<style>
.tp-cal {{ width: 100%; border-collapse: collapse; table-layout: fixed; font-size: .82rem; }}
.tp-cal th {{ color: {ui.MUTED}; font-weight: 400; padding: .2rem; text-align: left; }}
.tp-cal td {{ vertical-align: top; height: 5.4rem; border: 1px solid {ui.LINE}; padding: .25rem .3rem;
  background: {ui.CARD}; overflow: hidden; }}
.tp-cal td.tp-out {{ background: transparent; }}
.tp-cal td.tp-today {{ outline: 2px solid {ui.ACCENT}; outline-offset: -2px; }}
.tp-cal .tp-day {{ color: {ui.MUTED}; font-size: .75rem; }}
.tp-cal .tp-ev {{ display: block; border-left: 3px solid {ui.LINE}; padding-left: .3rem; margin-top: .15rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.tp-cal .tp-ms {{ font-weight: 700; color: {ui.ACCENT}; }}
</style>"""


def month_picker(key: str) -> tuple[int, int]:
    today = pm.today()
    offset = st.session_state.get(f"{key}-month", 0)
    c1, c2, c3, _ = st.columns([1, 2, 1, 4])
    if c1.button("◀", key=f"{key}-prev", help="Previous month"):
        offset -= 1
    if c3.button("▶", key=f"{key}-next", help="Next month"):
        offset += 1
    st.session_state[f"{key}-month"] = offset
    month0 = today.month - 1 + offset
    year, month = today.year + month0 // 12, month0 % 12 + 1
    c2.markdown(f"**{calendar_lib.month_name[month]} {year}**")
    return year, month


def calendar_view(conn, key: str, project_id: int | None = None, member: str | None = None) -> None:
    """A month of deadlines (and milestones, on the project calendar). Red: overdue or blocked; green: done."""
    year, month = month_picker(key)
    days = pm.calendar(conn, year, month, project_id=project_id, member=member)
    today = pm.today()
    rows = []
    for week in calendar_lib.Calendar(firstweekday=0).monthdayscalendar(year, month):
        cells = []
        for d in week:
            if not d:
                cells.append("<td class='tp-out'></td>")
                continue
            items = days.get(d, {"tasks": [], "milestones": []})
            body = "".join(f"<span class='tp-ev tp-ms'>⚑ {ui.e(m['title'])}</span>" for m in items["milestones"])
            body += "".join(f"<span class='tp-ev' style='border-left-color:{ui.TONE.get(tone(t), ui.LINE)}' "
                            f"title='{ui.e(t['title'])}'>#{t['id']} {ui.e(t['title'])}</span>" for t in items["tasks"])
            mark = " tp-today" if date(year, month, d) == today else ""
            cells.append(f"<td class='{mark.strip()}'><span class='tp-day'>{d}</span>{body}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    head = "".join(f"<th>{n}</th>" for n in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    st.markdown(CAL_CSS + f"<table class='tp-cal'><tr>{head}</tr>{''.join(rows)}</table>", unsafe_allow_html=True)
    count = sum(len(v["tasks"]) for v in days.values())
    st.caption(f"{count} deadline{'s' if count != 1 else ''} this month. Open a task from the Board or List tab.")


def timeline_view(tasks: list[dict], milestones: list[dict]) -> None:
    """Gantt-style bars from start (or creation) to deadline. Tasks without a deadline aren't shown."""
    rows = []
    for t in tasks:
        if not t["due_date"]:
            continue
        start = t["start_date"] or t["created_at"][:10]
        if start > t["due_date"]:
            start = t["due_date"]
        end = (date.fromisoformat(t["due_date"]) + timedelta(days=1)).isoformat()   # bars include the deadline day
        rows.append({"Task": f"#{t['id']} {t['title']}"[:60], "Start": start, "End": end, "Deadline": t["due_date"],
                     "Status": pm.STATUS_LABEL[t["status"]], "Assigned to": ", ".join(t["assignees"]) or "nobody",
                     "Progress": f"{t['reported_progress']}% reported"})
    if not rows:
        st.caption("No tasks with deadlines yet.")
        return
    colors = alt.Scale(domain=[pm.STATUS_LABEL[s] for s in pm.STATUSES],
                       range=[ui.MUTED, ui.LINE, ui.ACCENT, "#7C8CE0", ui.CONFLICT, ui.AGREE])
    frame = pd.DataFrame(rows)
    order = list(frame.sort_values("Start")["Task"])
    bars = alt.Chart(frame).mark_bar(cornerRadius=3, height=14).encode(
        x=alt.X("Start:T", title=None), x2="End:T", y=alt.Y("Task:N", sort=order, title=None, axis=alt.Axis(labelOverlap=False, labelLimit=240)),
        color=alt.Color("Status:N", scale=colors, legend=alt.Legend(orient="top", title=None)),
        tooltip=["Task", "Status", "Assigned to", "Start", "Deadline", "Progress"])
    today = alt.Chart(pd.DataFrame({"d": [pm.today().isoformat()]})).mark_rule(color=ui.CONFLICT, strokeDash=[4, 3]).encode(
        x="d:T")
    layers = [bars, today]
    dated = [m for m in milestones if m["due_date"]]
    if dated:
        stones = pd.DataFrame([{"Milestone": m["title"], "Due": m["due_date"]} for m in dated])
        layers.append(alt.Chart(stones).mark_rule(color=ui.ACCENT).encode(x="Due:T", tooltip=["Milestone", "Due"]))
    st.altair_chart(alt.layer(*layers).properties(height=max(160, 30 * len(rows))), width="stretch")
    st.caption("Bars run from the start date (or the day the task was created) to the deadline. The dashed red line is "
               "today; blue lines are milestones.")


# ---- AI planner (team leader) -----------------------------------------------------------------------------

def ai_planner(conn, actor: Actor, project_id: int, llm) -> None:
    """Paste a goal or meeting notes; review the proposed tasks; create only the ones you tick."""
    pending = pm_ai.drafts(conn, project_id)
    with st.expander("Plan with AI" + (f" ({len(pending)} proposal waiting)" if pending else ""),
                     icon=":material/auto_awesome:", expanded=bool(pending)):
        st.caption("Paste a goal or meeting notes. The AI proposes tasks with owners, priority, size and deadlines; "
                   "nothing is created until you tick items and press Create. Without an AI key, one task per "
                   "bullet line is suggested by rules (“- Riya: design the form by 8 Nov”).")
        with st.form("ai-plan", clear_on_submit=True, border=False):
            notes = st.text_area("Goal or meeting notes", max_chars=pm_ai.MAX_NOTES, height=140,
                                 placeholder="- Riya: design the feedback form by 8 Nov\n- Aman will set up error "
                                             "logging (urgent)\n- Write the demo script")
            if st.form_submit_button("Propose tasks", icon=":material/auto_awesome:"):
                _do(lambda: pm_ai.propose_plan(conn, actor, project_id, notes, llm), "Proposal ready below.")
        people = ["", *pm.members(conn)]
        for draft in pending:
            how = "the rules (no AI answered)" if draft["method"] == "rules" else f"AI ({draft['method']})"
            st.markdown(f"**Proposal from {ui.e(ui.ago(draft['created_at']))}** <span class='tp-meta'>by "
                        f"{ui.e(how)} · items with a warning start unticked</span>", unsafe_allow_html=True)
            frame = pd.DataFrame([{
                "Create": it["selected"], "Title": it["title"], "Assigned to": it["assignee"] or "",
                "Priority": it["priority"], "Size": it["size"],
                "Deadline": date.fromisoformat(it["due"]) if it["due"] else None,
                "Subtasks": "; ".join(it["subtasks"]),
                "Waits for": ", ".join(str(d + 1) for d in it["depends_on"]),
                "Watch out": "; ".join(it["flags"]), "From your notes": it["quote"]} for it in draft["items"]])
            frame.index = range(1, len(frame) + 1)
            edited = st.data_editor(frame, key=f"plan-{draft['id']}", width="stretch", num_rows="fixed",
                                    disabled=["Waits for", "Watch out", "From your notes"], column_config={
                                        "Assigned to": st.column_config.SelectboxColumn(options=people),
                                        "Priority": st.column_config.SelectboxColumn(options=pm.PRIORITIES),
                                        "Size": st.column_config.SelectboxColumn(options=list(pm.WEIGHTS)),
                                        "Deadline": st.column_config.DateColumn(format="YYYY-MM-DD"),
                                        "Title": st.column_config.TextColumn(max_chars=200)})
            c1, c2, _ = st.columns([1, 1, 3])
            if c1.button("Create selected tasks", key=f"plan-ok-{draft['id']}", type="primary"):
                items = []
                for (_, row), it in zip(edited.iterrows(), draft["items"]):
                    due = row["Deadline"]
                    items.append({**it, "selected": bool(row["Create"]), "title": str(row["Title"] or ""),
                                  "assignee": row["Assigned to"] or None, "priority": row["Priority"],
                                  "size": int(row["Size"]),
                                  "due": None if due is None or pd.isna(due) else pd.Timestamp(due).date().isoformat(),
                                  "subtasks": [x.strip() for x in str(row["Subtasks"] or "").split(";") if x.strip()]})
                _do(lambda items=items, d=draft: pm_ai.accept_plan(conn, actor, d["id"], items), "Tasks created.")
            if c2.button("Discard", key=f"plan-no-{draft['id']}"):
                _do(lambda d=draft: pm_ai.discard(conn, actor, d["id"]), "Proposal discarded.")


# ---- check-ins (daily monitoring) -----------------------------------------------------------------------------

def checkins(conn, actor: Actor, items: list[dict], manage: bool) -> None:
    """Facts plus a suggestion. Leaders can open the task, remind the people on it, snooze or dismiss; nothing here
    changes a task."""
    if not items:
        ui.empty("Nothing to check.", "The daily check looks for slipping pace, stale blockers, silent work, waiting "
                 "reviews and requests, heavy weeks, milestones and unassigned work.")
        return
    if manage:
        st.caption("From the daily check. Suggestions only: TeamPulse never changes a task by itself. Members see the "
                   "check-ins about their own work.")
    for a in items:
        label = pm_monitor.KIND_LABEL.get(a["kind"], a["kind"])
        suggestion = a["suggestion"] if manage else pm_monitor.member_suggestion(a)
        st.markdown(ui.row(f"{ui.pill(label, ui.SEVERITY_TONE[a['severity']])} {ui.e(a['text'])}",
                           ui.e(suggestion), ui.SEVERITY_TONE[a["severity"]],
                           f"first noticed {ui.e(ui.ago(a['first_seen']))}"), unsafe_allow_html=True)
        cols = st.columns([1, 1, 1, 1, 2])
        if a["task_id"] and cols[0].button("Open", key=f"chk-open-{a['id']}", type="tertiary"):
            st.session_state[OPEN_KEY] = a["task_id"]
            st.rerun()
        if manage:
            if (a["task_id"] or a["member"]) and cols[1].button("Remind", key=f"chk-remind-{a['id']}", type="tertiary",
                                                                help="Send the people on it a short, friendly note"):
                _do(lambda a=a: pm_monitor.remind(conn, actor, a["id"]), "Reminder sent.")
            if cols[2].button("Snooze 3 days", key=f"chk-snooze-{a['id']}", type="tertiary"):
                _do(lambda a=a: pm_monitor.snooze(conn, actor, a["id"], 3), "Snoozed for 3 days.")
            if cols[3].button("Dismiss", key=f"chk-dismiss-{a['id']}", type="tertiary",
                              help="It's fine as it is. It stays quiet until the situation changes."):
                _do(lambda a=a: pm_monitor.dismiss(conn, actor, a["id"]), "Dismissed.")
