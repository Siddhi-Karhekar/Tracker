"""Project management (TeamPulse 2.0, phase 1): projects, tasks, subtasks, dependencies, comments, history,
deadline requests, notifications, each member's workspace and the leader's dashboard.

Every function that changes data checks permission here, on the server, before touching the database:

  team leader  (pm.manage)  create projects, milestones and tasks; assign and reassign; edit anything; approve
                            deadline requests; mark work complete (that is the "verified" step); archive tasks
  team member  (pm.work)    on tasks assigned to them: move status (not to Completed), update progress, add and tick
                            subtasks, report a blocker, ask for a new deadline. On any task: comment.

Nothing here edits the `tasks` table that TeamPulse rebuilds from GitHub and Notion; planned work lives in pm_*.
Two people editing the same task at once can't silently overwrite each other: edits carry the version they
started from and are refused (StaleTask) if someone else saved first.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from teampulse import auth
from teampulse.auth import Actor, AuthError, PermissionDenied, audit, iso, now, require

STATUSES = ["backlog", "todo", "in_progress", "review", "blocked", "done"]
STATUS_LABEL = {"backlog": "Backlog", "todo": "To do", "in_progress": "In progress", "review": "Under review",
                "blocked": "Blocked", "done": "Completed"}
STARTED = {"in_progress", "review", "done"}          # can't get here while a dependency is unfinished
PRIORITIES = ["low", "medium", "high", "urgent"]
PRIORITY_LABEL = {p: p.title() for p in PRIORITIES}
LEADERS = "@leaders"
WRITES_PER_10_MIN = 120          # per person; stops a runaway script or a public-demo visitor flooding the board
SOON_DAYS = 7
LEADER_FIELDS = {"title", "description", "priority", "due_date", "start_date", "milestone_id", "estimate_hours", "weight"}
WEIGHTS = (1, 2, 3, 5, 8)        # task size; completion % counts finished size, not an average of percentages


class PMError(AuthError):
    """A request that can't be done as asked. The message is safe to show."""


class StaleTask(PMError):
    """Someone else saved this task after you opened it."""


# ---- small helpers -----------------------------------------------------------------------------------

def today() -> date:
    return datetime.now(timezone.utc).date()


def _date(value, label: str = "date") -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise PMError(f"The {label} must be a date like 2026-11-30.") from None


def _text(value, label: str, limit: int, required: bool = False) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()
    if required and not text:
        raise PMError(f"Please enter a {label}.")
    if len(text) > limit:
        raise PMError(f"The {label} is too long (at most {limit} characters).")
    return text


def _url(value) -> str | None:
    """Evidence links: http(s) only, so nothing like javascript: is ever stored."""
    link = str(value or "").strip()
    if not link:
        return None
    if not re.match(r"^https?://[^\s<>\"']+$", link, re.IGNORECASE) or len(link) > 500:
        raise PMError("The evidence link must be a full web address starting with https:// (at most 500 characters).")
    return link


def _weight(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 0
    if value not in WEIGHTS:
        raise PMError("Size must be 1, 2, 3, 5 or 8.")
    return value


def members(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT display_name FROM members ORDER BY display_name COLLATE NOCASE")]


def _member_names(conn, names) -> list[str]:
    known = {m.lower(): m for m in members(conn)}
    out = []
    for name in names or []:
        name = str(name).strip()
        if not name:
            continue
        if name.lower() not in known:
            raise PMError(f"{name} isn't on the team. Add them on the Data sources page first.")
        if known[name.lower()] not in out:
            out.append(known[name.lower()])
    return out


def _rate(conn, actor: Actor) -> None:
    since = iso(now() - timedelta(minutes=10))
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_id = ? AND action LIKE 'pm.%' AND at >= ?",
                     (actor.id, since)).fetchone()[0]
    if n >= WRITES_PER_10_MIN:
        from teampulse.actions import RateLimited
        raise RateLimited("That's a lot of changes in a few minutes. Please wait a little and try again.")


def _row(conn, task_id) -> dict:
    row = conn.execute("SELECT * FROM pm_tasks WHERE id = ? AND archived = 0", (int(task_id),)).fetchone()
    if row is None:
        raise PMError("That task doesn't exist (or was archived).")
    task = dict(row)
    task["assignees"] = assignees(conn, task)
    return task


def assignees(conn, task: dict) -> list[str]:
    """A subtask without its own assignees belongs to whoever owns its parent."""
    names = [r[0] for r in conn.execute("SELECT member FROM pm_assignees WHERE task_id = ? ORDER BY member",
                                        (task["id"],))]
    if not names and task.get("parent_id"):
        names = [r[0] for r in conn.execute("SELECT member FROM pm_assignees WHERE task_id = ? ORDER BY member",
                                            (task["parent_id"],))]
    return names


def is_assignee(actor: Actor | None, task: dict) -> bool:
    mine = (actor.member_name or "").lower() if actor else ""
    return bool(mine) and mine in {a.lower() for a in task["assignees"]}


def can_manage(actor: Actor | None) -> bool:
    return auth.can(actor, "pm.manage")


def can_work_on(actor: Actor | None, task: dict) -> bool:
    """The central resource rule (auth.RESOURCE_RULES['pm.work']): leaders and co-leads, or an assignee."""
    return auth.authorize(None, actor, "pm.work", task)


def _require_work(conn, actor: Actor, task: dict, what: str) -> None:
    require(conn, actor, "pm.work", f"task {task['id']}")
    if not can_work_on(actor, task):
        audit(conn, actor, what, f"task {task['id']}", "denied", "not assigned")
        raise PermissionDenied("You can only change tasks assigned to you. You can still comment on this one.")
    _rate(conn, actor)


def _history(conn, task_id: int, actor: Actor, field: str, old, new) -> None:
    conn.execute("INSERT INTO pm_history (task_id, at, actor_id, actor, field, old_value, new_value) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (task_id, iso(now()), actor.id, actor.display_name, field,
                  None if old is None else str(old)[:300], None if new is None else str(new)[:300]))


def _notify(conn, actor: Actor, recipients, task_id: int | None, text: str) -> None:
    me = (actor.member_name or "").lower()
    seen = set()
    for r in recipients:
        if not r or r.lower() == me or r.lower() in seen:
            continue
        seen.add(r.lower())
        conn.execute("INSERT INTO pm_notifications (recipient, task_id, text, created_at) VALUES (?, ?, ?, ?)",
                     (r, task_id, text[:300], iso(now())))


def _save(conn, task: dict, version: int | None, fields: dict) -> None:
    """Update a task, refusing if someone else saved it since `version` (None skips the check)."""
    sets = "".join(f"{k} = ?, " for k in fields)
    params = [*fields.values(), iso(now()), task["id"]]
    sql = f"UPDATE pm_tasks SET {sets}updated_at = ?, version = version + 1 WHERE id = ?"
    if version is not None:
        sql += " AND version = ?"
        params.append(int(version))
    if conn.execute(sql, params).rowcount == 0:
        raise StaleTask("Someone else changed this task while you were editing. Reload to see their change, then "
                        "try again.")


def _who(actor: Actor) -> str:
    return actor.member_name or actor.display_name


def _open_dependencies(conn, task_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT t.title FROM pm_dependencies d JOIN pm_tasks t ON t.id = d.depends_on "
        "WHERE d.task_id = ? AND t.status != 'done' AND t.archived = 0", (task_id,))]


def _open_subtasks(conn, task_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM pm_tasks WHERE parent_id = ? AND status != 'done' AND archived = 0",
                        (task_id,)).fetchone()[0]


def _check_dependencies(conn, task_id: int, project_id: int, depends_on) -> list[int]:
    ids = []
    for d in depends_on or []:
        d = int(d)
        row = conn.execute("SELECT project_id, parent_id FROM pm_tasks WHERE id = ? AND archived = 0", (d,)).fetchone()
        if row is None or row["project_id"] != project_id:
            raise PMError("A task can only depend on other tasks in the same project.")
        if d == task_id:
            raise PMError("A task can't depend on itself.")
        ids.append(d)
    # No cycles: walk everything the new dependencies depend on; reaching task_id would loop.
    seen, stack = set(), list(ids)
    while stack:
        current = stack.pop()
        if current == task_id:
            raise PMError("That would make a circle of tasks waiting for each other.")
        if current in seen:
            continue
        seen.add(current)
        stack += [r[0] for r in conn.execute("SELECT depends_on FROM pm_dependencies WHERE task_id = ?", (current,))]
    return sorted(set(ids))


# ---- projects and milestones (leader) -----------------------------------------------------------------

def create_project(conn, actor: Actor, name: str, description: str = "", start=None, end=None) -> int:
    require(conn, actor, "pm.manage", "create project")
    _rate(conn, actor)
    name = _text(name, "project name", 120, required=True)
    start, end = _date(start, "start date"), _date(end, "end date")
    if start and end and end < start:
        raise PMError("The project can't end before it starts.")
    with conn:
        pid = conn.execute("INSERT INTO pm_projects (name, description, start_date, end_date, created_by, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (name, _text(description, "description", 2000), start, end, actor.id, iso(now()))).lastrowid
    audit(conn, actor, "pm.project.create", f"project {pid}")
    return pid


def add_milestone(conn, actor: Actor, project_id: int, title: str, due=None) -> int:
    require(conn, actor, "pm.manage", f"project {project_id}")
    _rate(conn, actor)
    _project(conn, project_id)
    with conn:
        mid = conn.execute("INSERT INTO pm_milestones (project_id, title, due_date, created_at) VALUES (?, ?, ?, ?)",
                           (project_id, _text(title, "milestone name", 120, required=True), _date(due, "due date"),
                            iso(now()))).lastrowid
    audit(conn, actor, "pm.milestone.create", f"project {project_id}")
    return mid


def _project(conn, project_id) -> dict:
    row = conn.execute("SELECT * FROM pm_projects WHERE id = ? AND archived = 0", (int(project_id),)).fetchone()
    if row is None:
        raise PMError("That project doesn't exist (or was archived).")
    return dict(row)


# ---- tasks -------------------------------------------------------------------------------------------------

def create_task(conn, actor: Actor, project_id: int, title: str, description: str = "", assign_to=(),
                priority: str = "medium", due=None, start=None, status: str = "todo", milestone_id=None,
                estimate_hours=None, depends_on=(), parent_id=None, weight: int = 1) -> int:
    """Leaders create tasks. A member may add a subtask (parent_id) to a task assigned to them."""
    project = _project(conn, project_id)
    if parent_id:
        parent = _row(conn, parent_id)
        if parent["project_id"] != project["id"] or parent["parent_id"]:
            raise PMError("Subtasks go one level deep, inside the same project.")
        _require_work(conn, actor, parent, "pm.subtask.create")
        if not can_manage(actor):                      # members can't assign other people or set deadlines
            assign_to, depends_on, milestone_id, due, start = (), (), None, None, None
    else:
        require(conn, actor, "pm.manage", "create task")
        _rate(conn, actor)
    if priority not in PRIORITIES:
        raise PMError("Priority must be low, medium, high or urgent.")
    if status not in STATUSES or status == "done":
        raise PMError("New tasks start in Backlog, To do, In progress, Under review or Blocked.")
    title = _text(title, "task title", 200, required=True)
    due, start = _date(due, "deadline"), _date(start, "start date")
    if due and start and due < start:
        raise PMError("The deadline can't be before the start date.")
    names = _member_names(conn, assign_to)
    if milestone_id and not conn.execute("SELECT 1 FROM pm_milestones WHERE id = ? AND project_id = ?",
                                         (int(milestone_id), project["id"])).fetchone():
        raise PMError("That milestone belongs to another project.")
    estimate = None if estimate_hours in (None, "") else float(estimate_hours)
    if estimate is not None and not 0 <= estimate <= 1000:
        raise PMError("The estimate must be between 0 and 1000 hours.")
    weight = _weight(weight)
    stamp = iso(now())
    with conn:
        tid = conn.execute(
            "INSERT INTO pm_tasks (project_id, parent_id, milestone_id, title, description, priority, status, "
            "estimate_hours, start_date, due_date, created_by, created_at, updated_at, weight) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project["id"], int(parent_id) if parent_id else None, int(milestone_id) if milestone_id else None, title,
             _text(description, "description", 4000), priority, status, estimate, start, due, actor.id, stamp,
             stamp, weight)).lastrowid
        conn.executemany("INSERT INTO pm_assignees (task_id, member) VALUES (?, ?)", [(tid, n) for n in names])
        deps = _check_dependencies(conn, tid, project["id"], depends_on)
        conn.executemany("INSERT INTO pm_dependencies (task_id, depends_on) VALUES (?, ?)", [(tid, d) for d in deps])
        _history(conn, tid, actor, "created", None, title)
        _notify(conn, actor, names, tid, f"{_who(actor)} assigned you “{title}”" + (f", due {due}" if due else ""))
    audit(conn, actor, "pm.subtask.create" if parent_id else "pm.task.create", f"task {tid}")
    return tid


def update_task(conn, actor: Actor, task_id: int, version: int | None = None, **changes) -> None:
    """Leader edits: title, description, priority, dates, milestone, estimate, assignees, depends_on."""
    require(conn, actor, "pm.manage", f"task {task_id}")
    _rate(conn, actor)
    task = _row(conn, task_id)
    unknown = set(changes) - LEADER_FIELDS - {"assignees", "depends_on"}
    if unknown:
        raise PMError(f"Can't change {', '.join(sorted(unknown))} here.")
    fields = {}
    for key, value in changes.items():
        if key == "title":
            value = _text(value, "task title", 200, required=True)
        elif key == "description":
            value = _text(value, "description", 4000)
        elif key == "priority" and value not in PRIORITIES:
            raise PMError("Priority must be low, medium, high or urgent.")
        elif key in ("due_date", "start_date"):
            value = _date(value, "deadline" if key == "due_date" else "start date")
        elif key == "milestone_id":
            value = int(value) if value else None
            if value and not conn.execute("SELECT 1 FROM pm_milestones WHERE id = ? AND project_id = ?",
                                          (value, task["project_id"])).fetchone():
                raise PMError("That milestone belongs to another project.")
        elif key == "estimate_hours":
            value = None if value in (None, "") else float(value)
        elif key == "weight":
            value = _weight(value)
        if key in LEADER_FIELDS and value != task[key]:
            fields[key] = value
    due, start = fields.get("due_date", task["due_date"]), fields.get("start_date", task["start_date"])
    if due and start and due < start:
        raise PMError("The deadline can't be before the start date.")
    with conn:
        if fields or version is not None:
            _save(conn, task, version, fields)
        for key, value in fields.items():
            _history(conn, task["id"], actor, key, task[key], value)
        if "due_date" in fields:
            _notify(conn, actor, task["assignees"], task["id"], f"Deadline for “{task['title']}” is now "
                                                                f"{fields['due_date'] or 'not set'}")
        if "assignees" in changes:
            new = _member_names(conn, changes["assignees"])
            old = [r[0] for r in conn.execute("SELECT member FROM pm_assignees WHERE task_id = ?", (task["id"],))]
            if sorted(n.lower() for n in new) != sorted(n.lower() for n in old):
                conn.execute("DELETE FROM pm_assignees WHERE task_id = ?", (task["id"],))
                conn.executemany("INSERT INTO pm_assignees (task_id, member) VALUES (?, ?)", [(task["id"], n) for n in new])
                _history(conn, task["id"], actor, "assignees", ", ".join(old), ", ".join(new))
                added = [n for n in new if n.lower() not in {o.lower() for o in old}]
                removed = [o for o in old if o.lower() not in {n.lower() for n in new}]
                _notify(conn, actor, added, task["id"], f"{_who(actor)} assigned you “{task['title']}”")
                _notify(conn, actor, removed, task["id"], f"You're no longer assigned to “{task['title']}”")
                if not fields and version is None:
                    _save(conn, task, None, {})
        if "depends_on" in changes:
            deps = _check_dependencies(conn, task["id"], task["project_id"], changes["depends_on"])
            old = sorted(r[0] for r in conn.execute("SELECT depends_on FROM pm_dependencies WHERE task_id = ?",
                                                    (task["id"],)))
            if deps != old:
                conn.execute("DELETE FROM pm_dependencies WHERE task_id = ?", (task["id"],))
                conn.executemany("INSERT INTO pm_dependencies (task_id, depends_on) VALUES (?, ?)",
                                 [(task["id"], d) for d in deps])
                _history(conn, task["id"], actor, "depends_on", ",".join(map(str, old)), ",".join(map(str, deps)))
    audit(conn, actor, "pm.task.edit", f"task {task['id']}", detail=",".join(sorted(changes)))


def set_status(conn, actor: Actor, task_id: int, status: str, version: int | None = None, note: str = "") -> None:
    """Move a task along the workflow. Members can't mark their own work Completed: they send it for review and
    the leader completes it. Blocked needs a reason; started work needs its dependencies finished."""
    task = _row(conn, task_id)
    _require_work(conn, actor, task, "pm.task.status")
    if status not in STATUSES:
        raise PMError("Unknown status.")
    if status == task["status"]:
        return
    subtask = bool(task["parent_id"])
    if not subtask and not can_manage(actor) and "done" in (status, task["status"]):
        audit(conn, actor, "pm.task.status", f"task {task['id']}", "denied", f"member {task['status']}->{status}")
        raise PermissionDenied("Move it to Under review when you've finished; the team leader marks it Completed."
                               if status == "done" else "Only the team leader can reopen completed work.")
    note = _text(note, "note", 1000)
    if status == "blocked" and not note:
        raise PMError("Say what's blocking it, so the team knows how to help.")
    if status in STARTED:
        waiting = _open_dependencies(conn, task["id"])
        if waiting:
            raise PMError("This task is waiting for: " + "; ".join(f"“{t}”" for t in waiting) + ". Finish those first.")
    if status == "done" and _open_subtasks(conn, task["id"]):
        raise PMError("Some subtasks are still open. Finish or remove them first.")
    fields = {"status": status, "blocked_reason": note if status == "blocked" else None,
              "completed_at": iso(now()) if status == "done" else None}
    if status == "done":
        fields["reported_progress"] = 100
        if not subtask:            # the leader approving finished work is the verification
            fields.update(verified_progress=100, verified_by=actor.display_name, verified_at=iso(now()))
    elif task["status"] == "done" and not subtask:
        fields.update(verified_progress=None, verified_by=None, verified_at=None)   # reopened: not verified any more
    with conn:
        _save(conn, task, version, fields)
        _history(conn, task["id"], actor, "status", task["status"], status)
        title = task["title"]
        if status == "blocked":
            conn.execute("INSERT INTO pm_comments (task_id, author_id, author, body, kind, created_at) "
                         "VALUES (?, ?, ?, ?, 'blocker', ?)", (task["id"], actor.id, _who(actor), note, iso(now())))
            _notify(conn, actor, [LEADERS, *task["assignees"]], task["id"], f"{_who(actor)} is blocked on “{title}”: {note}")
        elif status == "review" and not subtask:
            _notify(conn, actor, [LEADERS], task["id"], f"“{title}” is ready for review ({_who(actor)})")
        elif status == "done" and not subtask:
            _notify(conn, actor, task["assignees"], task["id"], f"{_who(actor)} marked “{title}” completed")
        elif task["status"] == "review" and status != "done" and not subtask and can_manage(actor):
            _notify(conn, actor, task["assignees"], task["id"], f"“{title}” was sent back to {STATUS_LABEL[status]}"
                    + (f": {note}" if note else ""))
        if note and status != "blocked":
            conn.execute("INSERT INTO pm_comments (task_id, author_id, author, body, kind, created_at) "
                         "VALUES (?, ?, ?, ?, 'comment', ?)", (task["id"], actor.id, _who(actor), note, iso(now())))
    audit(conn, actor, "pm.task.status", f"task {task['id']}", detail=f"{task['status']}->{status}")


def report_blocker(conn, actor: Actor, task_id: int, reason: str, version: int | None = None) -> None:
    set_status(conn, actor, task_id, "blocked", version, reason)


def _percent(progress) -> int:
    try:
        progress = int(progress)
    except (TypeError, ValueError):
        raise PMError("Progress must be a whole number from 0 to 100.") from None
    if not 0 <= progress <= 100:
        raise PMError("Progress must be from 0 to 100.")
    return progress


def _progress_update(conn, task: dict, actor: Actor, kind: str, progress: int, note: str, evidence: str | None) -> None:
    conn.execute("INSERT INTO pm_progress_updates (task_id, kind, author_id, author, progress, note, evidence_url, "
                 "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (task["id"], kind, actor.id, _who(actor), progress, note, evidence, iso(now())))


def set_progress(conn, actor: Actor, task_id: int, progress: int, version: int | None = None, note: str = "",
                 evidence_url: str = "") -> None:
    """The assignee's own progress number, optionally with what they did and a link to show it (a PR, a doc).
    This is "self-reported"; only verify_progress (leaders) sets verified progress."""
    task = _row(conn, task_id)
    _require_work(conn, actor, task, "pm.task.progress")
    progress = _percent(progress)
    note, evidence = _text(note, "progress note", 1000), _url(evidence_url)
    if task["status"] == "done" and progress != 100 and not can_manage(actor):
        raise PMError("This task is completed. Ask the team leader to reopen it.")
    if progress == task["reported_progress"] and not (note or evidence):
        return
    with conn:
        _save(conn, task, version, {"reported_progress": progress})
        if progress != task["reported_progress"]:
            _history(conn, task["id"], actor, "reported_progress", task["reported_progress"], progress)
        _progress_update(conn, task, actor, "reported", progress, note, evidence)
    audit(conn, actor, "pm.task.progress", f"task {task['id']}", detail=str(progress))


def verify_progress(conn, actor: Actor, task_id: int, progress: int, note: str = "", version: int | None = None,
                    evidence_url: str = "") -> None:
    """The team leader confirms how far the work really is, after looking at it. Separate from the assignee's own
    number, never set automatically. 100% here doesn't complete the task; approving it in review does."""
    require(conn, actor, "pm.manage", f"task {task_id}")
    _rate(conn, actor)
    task = _row(conn, task_id)
    progress = _percent(progress)
    note, evidence = _text(note, "note", 1000), _url(evidence_url)
    with conn:
        _save(conn, task, version, {"verified_progress": progress, "verified_by": actor.display_name,
                                    "verified_at": iso(now())})
        _history(conn, task["id"], actor, "verified_progress", task["verified_progress"], progress)
        _progress_update(conn, task, actor, "verified", progress, note, evidence)
        _notify(conn, actor, task["assignees"], task["id"],
                f"{_who(actor)} checked “{task['title']}”: {progress}% verified" + (f". {note}" if note else ""))
    audit(conn, actor, "pm.task.verify", f"task {task['id']}", detail=str(progress))


def add_comment(conn, actor: Actor, task_id: int, body: str) -> int:
    """Anyone on the team can comment on any task. @Name mentions notify that person."""
    task = _row(conn, task_id)
    require(conn, actor, "pm.work", f"task {task['id']}")
    _rate(conn, actor)
    body = _text(body, "comment", 2000, required=True)
    mentioned = [m for m in members(conn) if re.search(rf"@{re.escape(m)}\b", body, re.IGNORECASE)]
    with conn:
        cid = conn.execute("INSERT INTO pm_comments (task_id, author_id, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
                           (task["id"], actor.id, _who(actor), body, iso(now()))).lastrowid
        _notify(conn, actor, [*task["assignees"], *mentioned], task["id"],
                f"{_who(actor)} commented on “{task['title']}”: {body[:120]}")
    audit(conn, actor, "pm.comment", f"task {task['id']}")
    return cid


def request_deadline(conn, actor: Actor, task_id: int, new_date, reason: str) -> int:
    task = _row(conn, task_id)
    _require_work(conn, actor, task, "pm.request.deadline")
    proposed = _date(new_date, "new deadline")
    if not proposed:
        raise PMError("Pick the new deadline you'd like.")
    if proposed == task["due_date"]:
        raise PMError("That's already the deadline.")
    reason = _text(reason, "reason", 1000, required=True)
    who = _who(actor)
    if conn.execute("SELECT 1 FROM pm_requests WHERE task_id = ? AND requested_by = ? AND status = 'pending'",
                    (task["id"], who)).fetchone():
        raise PMError("You already have a deadline request waiting for this task.")
    with conn:
        rid = conn.execute("INSERT INTO pm_requests (task_id, requested_by, proposed, reason, created_at) "
                           "VALUES (?, ?, ?, ?, ?)", (task["id"], who, proposed, reason, iso(now()))).lastrowid
        _history(conn, task["id"], actor, "deadline_request", task["due_date"], proposed)
        _notify(conn, actor, [LEADERS], task["id"], f"{who} asks to move “{task['title']}” to {proposed}: {reason}")
    audit(conn, actor, "pm.request.deadline", f"task {task['id']}")
    return rid


def decide_request(conn, actor: Actor, request_id: int, approve: bool, note: str = "") -> None:
    require(conn, actor, "pm.manage", f"request {request_id}")
    _rate(conn, actor)
    req = conn.execute("SELECT * FROM pm_requests WHERE id = ?", (int(request_id),)).fetchone()
    if req is None or req["status"] != "pending":
        raise PMError("That request was already decided.")
    task = _row(conn, req["task_id"])
    note = _text(note, "note", 500)
    with conn:
        changed = conn.execute("UPDATE pm_requests SET status = ?, decided_by = ?, decided_at = ? "
                               "WHERE id = ? AND status = 'pending'",
                               ("approved" if approve else "rejected", actor.display_name, iso(now()), req["id"])).rowcount
        if not changed:
            raise PMError("That request was already decided.")
        if approve:
            _save(conn, task, None, {"due_date": req["proposed"]})
            _history(conn, task["id"], actor, "due_date", task["due_date"], req["proposed"])
        word = "approved" if approve else "declined"
        _notify(conn, actor, [req["requested_by"]], task["id"],
                f"{_who(actor)} {word} your new deadline ({req['proposed']}) for “{task['title']}”"
                + (f": {note}" if note else ""))
    audit(conn, actor, "pm.request.decide", f"request {req['id']}", detail=word)


def archive_task(conn, actor: Actor, task_id: int) -> None:
    require(conn, actor, "pm.manage", f"task {task_id}")
    _rate(conn, actor)
    task = _row(conn, task_id)
    with conn:
        conn.execute("UPDATE pm_tasks SET archived = 1, updated_at = ?, version = version + 1 WHERE id = ? OR parent_id = ?",
                     (iso(now()), task["id"], task["id"]))
        _history(conn, task["id"], actor, "archived", None, "yes")
    audit(conn, actor, "pm.task.archive", f"task {task['id']}")


# ---- notifications -----------------------------------------------------------------------------------------

def _inboxes(actor: Actor) -> list[str]:
    boxes = [actor.member_name] if actor.member_name else []
    return boxes + ([LEADERS] if can_manage(actor) else [])


def notifications(conn, actor: Actor, unread_only: bool = True, limit: int = 30) -> list[dict]:
    boxes = _inboxes(actor)
    if not boxes:
        return []
    marks = ",".join("?" * len(boxes))
    sql = f"SELECT * FROM pm_notifications WHERE recipient IN ({marks})"
    if unread_only:
        sql += " AND read_at IS NULL"
    return [dict(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*boxes, limit))]


def mark_read(conn, actor: Actor) -> int:
    boxes = _inboxes(actor)
    if not boxes:
        return 0
    marks = ",".join("?" * len(boxes))
    with conn:
        return conn.execute(f"UPDATE pm_notifications SET read_at = ? WHERE read_at IS NULL "
                            f"AND recipient IN ({marks})", (iso(now()), *boxes)).rowcount


# ---- reading ---------------------------------------------------------------------------------------------

def projects(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM pm_projects WHERE archived = 0 ORDER BY id")]


def milestones(conn, project_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM pm_milestones WHERE project_id = ? "
                                          "ORDER BY due_date IS NULL, due_date, id", (int(project_id),))]


def measured_progress(task: dict, subtasks_done: int, subtasks_total: int) -> int | None:
    """Progress TeamPulse can check, as opposed to what the assignee reports: 100 once the leader has marked it
    Completed, otherwise the share of its subtasks that are done. None when there's nothing to measure yet."""
    if task["status"] == "done":
        return 100
    if subtasks_total:
        return round(100 * subtasks_done / subtasks_total)
    return None


def _decorate(conn, task: dict, on: date) -> dict:
    task["assignees"] = assignees(conn, task)
    total, done = conn.execute("SELECT COUNT(*), COALESCE(SUM(status = 'done'), 0) FROM pm_tasks "
                               "WHERE parent_id = ? AND archived = 0", (task["id"],)).fetchone()
    task["subtasks_total"], task["subtasks_done"] = total, done
    task["measured_progress"] = measured_progress(task, done, total)
    task["waiting_on"] = _open_dependencies(conn, task["id"])
    due = date.fromisoformat(task["due_date"]) if task["due_date"] else None
    task["days_left"] = (due - on).days if due else None
    task["overdue"] = bool(due and due < on and task["status"] != "done")
    task["risks"] = risks(task)
    return task


def risks(task: dict) -> list[str]:
    """Plain reasons a task may slip. Facts about the task, never a judgement about the person."""
    if task["status"] == "done":
        return []
    out = []
    left = task.get("days_left")
    if task.get("overdue"):
        out.append(f"{-left} day{'s' if left != -1 else ''} past the deadline")
    elif left is not None and left <= 3 and max(task["reported_progress"], task.get("measured_progress") or 0) < 50:
        out.append(f"due in {left} day{'s' if left != 1 else ''} and under half done")
    if task["status"] == "blocked":
        out.append("blocked" + (f": {task['blocked_reason']}" if task.get("blocked_reason") else ""))
    if task.get("waiting_on") and left is not None and left <= SOON_DAYS:
        out.append("waiting for " + ", ".join(f"“{t}”" for t in task["waiting_on"]))
    verified, measured = task.get("verified_progress"), task.get("measured_progress")
    if verified is not None and task["reported_progress"] - verified >= 40:
        out.append(f"reported {task['reported_progress']}% but the leader verified {verified}%")
    elif verified is None and measured is not None and task["reported_progress"] - measured >= 40:
        out.append(f"reported {task['reported_progress']}% but only {measured}% of subtasks are done")
    if not task.get("assignees") and not task.get("parent_id"):
        out.append("nobody is assigned")
    return out


def tasks(conn, project_id: int | None = None, member: str | None = None, include_subtasks: bool = False,
          on: date | None = None) -> list[dict]:
    on = on or today()
    sql = ("SELECT t.*, p.name AS project FROM pm_tasks t JOIN pm_projects p ON p.id = t.project_id "
           "WHERE t.archived = 0 AND p.archived = 0")
    params: list = []
    if project_id:
        sql += " AND t.project_id = ?"
        params.append(int(project_id))
    if not include_subtasks:
        sql += " AND t.parent_id IS NULL"
    if member:   # tasks assigned to them, and subtasks of their tasks that have no assignee of their own
        sql += (" AND (t.id IN (SELECT task_id FROM pm_assignees WHERE member = ?) OR (t.parent_id IN "
                "(SELECT task_id FROM pm_assignees WHERE member = ?) AND NOT EXISTS "
                "(SELECT 1 FROM pm_assignees a WHERE a.task_id = t.id)))")
        params += [member, member]
    sql += " ORDER BY t.due_date IS NULL, t.due_date, t.id"
    return [_decorate(conn, dict(r), on) for r in conn.execute(sql, params)]


def task_detail(conn, task_id: int, on: date | None = None) -> dict:
    on = on or today()
    row = conn.execute("SELECT t.*, p.name AS project FROM pm_tasks t JOIN pm_projects p ON p.id = t.project_id "
                       "WHERE t.id = ? AND t.archived = 0", (int(task_id),)).fetchone()
    if row is None:
        raise PMError("That task doesn't exist (or was archived).")
    task = _decorate(conn, dict(row), on)
    task["subtasks"] = [_decorate(conn, dict(r), on) for r in conn.execute(
        "SELECT * FROM pm_tasks WHERE parent_id = ? AND archived = 0 ORDER BY id", (task["id"],))]
    task["dependencies"] = [dict(r) for r in conn.execute(
        "SELECT t.id, t.title, t.status FROM pm_dependencies d JOIN pm_tasks t ON t.id = d.depends_on "
        "WHERE d.task_id = ? AND t.archived = 0 ORDER BY t.id", (task["id"],))]
    task["dependents"] = [dict(r) for r in conn.execute(
        "SELECT t.id, t.title, t.status FROM pm_dependencies d JOIN pm_tasks t ON t.id = d.task_id "
        "WHERE d.depends_on = ? AND t.archived = 0 ORDER BY t.id", (task["id"],))]
    task["comments"] = [dict(r) for r in conn.execute(
        "SELECT * FROM pm_comments WHERE task_id = ? ORDER BY id", (task["id"],))]
    task["history"] = [dict(r) for r in conn.execute(
        "SELECT * FROM pm_history WHERE task_id = ? ORDER BY id DESC LIMIT 50", (task["id"],))]
    task["requests"] = [dict(r) for r in conn.execute(
        "SELECT * FROM pm_requests WHERE task_id = ? ORDER BY id DESC", (task["id"],))]
    task["updates"] = [dict(r) for r in conn.execute(
        "SELECT * FROM pm_progress_updates WHERE task_id = ? ORDER BY id DESC LIMIT 30", (task["id"],))]
    task["milestone"] = (conn.execute("SELECT title FROM pm_milestones WHERE id = ?", (task["milestone_id"],))
                         .fetchone() or [None])[0] if task["milestone_id"] else None
    return task


def completion(tasks_: list[dict]) -> int:
    """Share of the work that is Completed, counting each task by its size (weight). Percentages people report are
    deliberately not averaged: three tasks "at 90%" are not 90% of a project."""
    total = sum(t.get("weight") or 1 for t in tasks_)
    done = sum(t.get("weight") or 1 for t in tasks_ if t["status"] == "done")
    return round(100 * done / total) if total else 0


COMPLETION_NOTE = ("Completion counts finished tasks by size (weight 1, 2, 3, 5 or 8, set by the leader). A task counts "
                   "only once the leader has marked it Completed; reported percentages are not averaged.")


def workspace(conn, member: str, on: date | None = None) -> dict:
    """One person's own view of their work: what's late, what's next, what's waiting on someone."""
    on = on or today()
    mine = tasks(conn, member=member, include_subtasks=True, on=on)
    top = [t for t in mine if not t["parent_id"]]
    open_ = [t for t in top if t["status"] != "done"]
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    sections = {
        "overdue": [t for t in open_ if t["overdue"]],
        "due_soon": [t for t in open_ if not t["overdue"] and t["days_left"] is not None and t["days_left"] <= SOON_DAYS],
        "blocked": [t for t in open_ if t["status"] == "blocked"],
        "high_priority": [t for t in open_ if t["priority"] in ("high", "urgent")],
        "in_progress": [t for t in open_ if t["status"] == "in_progress"],
        "to_do": [t for t in open_ if t["status"] in ("todo", "backlog")],
        "review": [t for t in open_ if t["status"] == "review"],
        "completed": [t for t in top if t["status"] == "done"],
        "recent": sorted(top, key=lambda t: t["updated_at"], reverse=True)[:5],
        "subtasks": [t for t in mine if t["parent_id"] and t["status"] != "done"],
    }
    pending = [dict(r) for r in conn.execute(
        "SELECT r.*, t.title FROM pm_requests r JOIN pm_tasks t ON t.id = r.task_id WHERE r.requested_by = ? "
        "ORDER BY r.id DESC LIMIT 10", (member,))]
    done_recent = [t for t in sections["completed"] if (t["completed_at"] or "") >= week_ago]
    progress = completion(top)
    return {
        "member": member, "sections": sections, "requests": pending,
        "metrics": {"assigned": len(top), "open": len(open_), "in_progress": len(sections["in_progress"]),
                    "overdue": len(sections["overdue"]), "due_soon": len(sections["due_soon"]),
                    "blocked": len(sections["blocked"]), "completed": len(sections["completed"]),
                    "completed_this_week": len(done_recent), "progress": progress,
                    "open_hours": round(sum(t["estimate_hours"] or 0 for t in open_), 1)},
    }


def dashboard(conn, project_id: int | None = None, on: date | None = None) -> dict:
    """The leader's overview. Workload is listed alphabetically, never ranked: it shows who may be overloaded,
    not who is 'best'."""
    on = on or today()
    top = tasks(conn, project_id=project_id, on=on)
    total = len(top)
    by_status = {s: sum(t["status"] == s for t in top) for s in STATUSES}
    workload = []
    for name in members(conn):
        theirs = [t for t in top if name.lower() in {a.lower() for a in t["assignees"]}]
        open_ = [t for t in theirs if t["status"] != "done"]
        workload.append({"member": name, "open": len(open_), "in_progress": sum(t["status"] == "in_progress" for t in open_),
                         "overdue": sum(t["overdue"] for t in open_), "blocked": sum(t["status"] == "blocked" for t in open_),
                         "completed": len(theirs) - len(open_),
                         "open_hours": round(sum(t["estimate_hours"] or 0 for t in open_), 1)})
    sql = ("SELECT m.*, p.name AS project FROM pm_milestones m JOIN pm_projects p ON p.id = m.project_id "
           "WHERE p.archived = 0 AND (m.due_date IS NULL OR m.due_date >= ?)")
    params: list = [on.isoformat()]
    if project_id:
        sql += " AND m.project_id = ?"
        params.append(int(project_id))
    upcoming = []
    for m in conn.execute(sql + " ORDER BY m.due_date IS NULL, m.due_date LIMIT 8", params):
        linked = [t for t in top if t["milestone_id"] == m["id"]]
        upcoming.append({**dict(m), "tasks": len(linked), "done": sum(t["status"] == "done" for t in linked),
                         "completion": completion(linked)})
    req_sql = ("SELECT r.*, t.title, t.due_date FROM pm_requests r JOIN pm_tasks t ON t.id = r.task_id "
               "WHERE r.status = 'pending' AND t.archived = 0")
    req_params: list = []
    if project_id:
        req_sql += " AND t.project_id = ?"
        req_params.append(int(project_id))
    act_sql = ("SELECT h.*, t.title FROM pm_history h JOIN pm_tasks t ON t.id = h.task_id"
               + (" WHERE t.project_id = ?" if project_id else "") + " ORDER BY h.id DESC LIMIT 15")
    return {
        "total": total, "by_status": by_status,
        "completion": completion(top),
        "overdue": sum(t["overdue"] for t in top),
        "at_risk": [t for t in top if t["risks"]],
        "review": [t for t in top if t["status"] == "review"],
        "requests": [dict(r) for r in conn.execute(req_sql + " ORDER BY r.id", req_params)],
        "workload": workload, "milestones": upcoming,
        "activity": [dict(r) for r in conn.execute(act_sql, [int(project_id)] if project_id else [])],
    }


# ---- reminders -------------------------------------------------------------------------------------------

def send_reminders(conn, on: date | None = None) -> int:
    """Deadline reminders: each assignee once a day for work due today or tomorrow, and for overdue work; the leaders
    once per overdue task. Safe to call as often as you like (the poll loop and page loads both do): a reminder
    already sent today is never sent again. Returns how many were created."""
    on = on or today()
    stamp, created = iso(now()), 0
    with conn:
        for t in tasks(conn, on=on):
            if t["status"] == "done" or t["days_left"] is None or t["days_left"] > 1:
                continue
            title, left = t["title"], t["days_left"]
            if left < 0:
                text = f"“{title}” is {-left} day{'s' if left != -1 else ''} past its deadline ({t['due_date']})"
                kind = "overdue"
            else:
                text = f"“{title}” is due {'today' if left == 0 else 'tomorrow'}"
                kind = "due"
            for member in t["assignees"]:
                created += conn.execute(
                    "INSERT OR IGNORE INTO pm_notifications (recipient, task_id, text, created_at, dedupe) "
                    "VALUES (?, ?, ?, ?, ?)", (member, t["id"], text, stamp,
                                               f"{kind}:{t['id']}:{member.lower()}:{on.isoformat()}")).rowcount
            if kind == "overdue":   # leaders hear once per task, not every day
                created += conn.execute(
                    "INSERT OR IGNORE INTO pm_notifications (recipient, task_id, text, created_at, dedupe) "
                    "VALUES (?, ?, ?, ?, ?)", (LEADERS, t["id"], f"{text}. Assigned to "
                                               f"{', '.join(t['assignees']) or 'nobody'}.", stamp,
                                               f"overdue-leaders:{t['id']}:{t['due_date']}")).rowcount
    return created


# ---- calendar ------------------------------------------------------------------------------------------

def calendar(conn, year: int, month: int, project_id: int | None = None, member: str | None = None) -> dict:
    """Deadlines and milestones in one month: {day: {"tasks": [...], "milestones": [...]}}."""
    first = date(year, month, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days: dict[int, dict] = {}
    for t in tasks(conn, project_id=project_id, member=member):
        if t["due_date"] and first.isoformat() <= t["due_date"] <= last.isoformat():
            days.setdefault(int(t["due_date"][8:10]), {"tasks": [], "milestones": []})["tasks"].append(t)
    sql = ("SELECT m.* FROM pm_milestones m JOIN pm_projects p ON p.id = m.project_id WHERE p.archived = 0 "
           "AND m.due_date BETWEEN ? AND ?")
    params: list = [first.isoformat(), last.isoformat()]
    if project_id:
        sql += " AND m.project_id = ?"
        params.append(int(project_id))
    if not member:
        for m in conn.execute(sql, params):
            days.setdefault(int(m["due_date"][8:10]), {"tasks": [], "milestones": []})["milestones"].append(dict(m))
    return days
