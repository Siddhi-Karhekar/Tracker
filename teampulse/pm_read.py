"""Reading the plan: task lists with their risks, a task's full detail, completion by size, each member's workspace,
the leader's dashboard and the calendar. Nothing here changes data; the rules for changing it are in pm.py.
All of these are also available as pm.<name>."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from teampulse.pm import SOON_DAYS, STATUSES, PMError, _open_dependencies, assignees, members, today


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
    act_sql = ("SELECT h.*, t.title FROM pm_history h JOIN pm_tasks t ON t.id = h.task_id"  # nosec B608  # fixed column names (TASK_COLUMNS) or ? placeholders
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
