"""What an AI assistant connected over MCP can do, as plain functions (teampulse/mcp_server.py exposes them).

Each call runs as the person whose token was used, with exactly their permissions; the same functions the pages
use check them again. A 'read' token can only look. A 'write' token can also do what the person can do on their
own tasks (comment, report progress, report a blocker, send for review); a leader's can also propose tasks from notes,
which only creates a draft to confirm in the app. Nothing here assigns, reassigns, changes deadlines, approves,
completes or deletes: those stay in the app, where a person clicks them.
"""
from __future__ import annotations

from datetime import timedelta

from teampulse import auth, pm, pm_ai, pm_monitor, pm_sync
from teampulse.auth import Actor, PermissionDenied, audit, iso, now

CALLS_PER_10_MIN = 120
WRITE_TOOLS = {"add_comment", "report_progress", "report_blocker", "send_for_review", "propose_tasks"}


class ToolRefused(Exception):
    """A safe message for the assistant (and the person) to read."""


def _short(t: dict) -> dict:
    return {"id": t["id"], "title": t["title"], "project": t.get("project"), "status": pm.STATUS_LABEL[t["status"]],
            "priority": t["priority"], "assignees": t["assignees"], "deadline": t["due_date"],
            "reported_progress": t["reported_progress"], "verified_progress": t.get("verified_progress"),
            "watch_out": t.get("risks", [])}


def guard(conn, actor: Actor | None, scope: str, tool: str) -> Actor:
    """Checks every call: a valid token, the right scope, and not too many calls."""
    if actor is None:
        raise ToolRefused("This TeamPulse connection isn't valid any more (expired, removed, or the account changed). "
                          "Create a new one under My account.")
    if tool in WRITE_TOOLS and scope != "write":
        audit(conn, actor, f"mcp.{tool}", "", "denied", "read-only token")
        raise ToolRefused("This connection is read-only. To let the assistant update your tasks, create a "
                          "'read and update' connection under My account.")
    since = iso(now() - timedelta(minutes=10))
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_id = ? AND action LIKE 'mcp.%' AND at >= ?",
                     (actor.id, since)).fetchone()[0]
    if n >= CALLS_PER_10_MIN:
        raise ToolRefused("Too many requests in a few minutes. Please wait a little.")
    audit(conn, actor, f"mcp.{tool}")
    return actor


def _member(actor: Actor, member: str | None) -> str:
    if member and member.lower() != (actor.member_name or "").lower():
        if not pm.can_manage(actor):
            raise ToolRefused("You can only see your own workspace. Team leaders can see everyone's.")
        return member
    if not actor.member_name and not member:
        raise ToolRefused("Your account isn't linked to a team member, so there's no personal workspace. "
                          "Leaders can ask for a member's workspace by name.")
    return member or actor.member_name


# ---- read --------------------------------------------------------------------------------------------------

def my_tasks(conn, actor: Actor, member: str | None = None) -> dict:
    name = _member(actor, member)
    if name.lower() not in {m.lower() for m in pm.members(conn)}:
        raise ToolRefused(f"{name} isn't on the team.")
    w = pm.workspace(conn, name)
    sections = {k: [_short(t) for t in v] for k, v in w["sections"].items() if k not in ("recent", "subtasks") and v}
    return {"member": name, "summary": w["metrics"], "tasks": sections,
            "check_ins": [{"about": a["text"], "suggestion": a["suggestion"] if pm.can_manage(actor)
                           else pm_monitor.member_suggestion(a)} for a in pm_monitor.alerts(conn, member=name)],
            "deadline_requests": [{"task_id": r["task_id"], "task": r["title"], "proposed": r["proposed"],
                                   "status": r["status"]} for r in w["requests"]]}


def what_next(conn, actor: Actor) -> dict:
    name = _member(actor, None)
    return {"member": name, "next": [{**_short(s["task"]), "why": s["why"]} for s in pm_ai.next_steps(conn, name)]}


def get_task(conn, actor: Actor, task_id: int) -> dict:
    try:
        t = pm.task_detail(conn, int(task_id))
    except (pm.PMError, ValueError, TypeError):
        raise ToolRefused(f"There's no task #{task_id}.") from None
    out = {**_short(t), "description": t["description"], "milestone": t["milestone"],
           "subtasks": [{"title": s["title"], "done": s["status"] == "done"} for s in t["subtasks"]],
           "waits_for": [f"#{d['id']} {d['title']} ({pm.STATUS_LABEL[d['status']]})" for d in t["dependencies"]],
           "needed_by": [f"#{d['id']} {d['title']}" for d in t["dependents"]],
           "blocked_reason": t["blocked_reason"],
           "recent_comments": [{"by": c["author"], "at": c["created_at"][:16], "text": c["body"][:500]}
                               for c in t["comments"][-5:]],
           "you_can_update_it": pm.can_work_on(actor, t)}
    v = pm_sync.view(conn, t)
    if v and not v["missing"]:
        out["github_notion"] = {"linked_to": v["source"]["title"], "url": v["source"]["url"],
                                "disagreements": [d["text"] for d in v["differences"]],
                                "evidence": [{"kind": e["role"], "by": e["who"], "title": e["title"][:160],
                                              "url": e["url"]} for e in v["evidence"][:8]]}
    return out


def search_tasks(conn, actor: Actor, query: str = "", status: str | None = None, assignee: str | None = None,
                 limit: int = 20) -> dict:
    wanted = None
    if status:
        wanted = next((k for k, v in pm.STATUS_LABEL.items() if status.lower() in (k, v.lower())), None)
        if wanted is None:
            raise ToolRefused("Status must be one of: " + ", ".join(pm.STATUS_LABEL.values()) + ".")
    hits = []
    for t in pm.tasks(conn):
        if query and query.lower() not in (t["title"] + " " + t["description"]).lower():
            continue
        if wanted and t["status"] != wanted:
            continue
        if assignee and assignee.lower() not in {a.lower() for a in t["assignees"]}:
            continue
        hits.append(_short(t))
    limit = max(1, min(int(limit or 20), 50))
    return {"count": len(hits), "tasks": hits[:limit]}


def project_status(conn, actor: Actor, project_id: int | None = None) -> dict:
    projects = pm.projects(conn)
    if project_id is None and len(projects) == 1:
        project_id = projects[0]["id"]
    if project_id is not None and not any(p["id"] == int(project_id) for p in projects):
        raise ToolRefused(f"There's no project {project_id}. Projects: "
                          + ", ".join(f"{p['id']} {p['name']}" for p in projects) + ".")
    d = pm.dashboard(conn, project_id)
    out = {"projects": [{"id": p["id"], "name": p["name"]} for p in projects], "project_id": project_id,
           "tasks": d["total"], "completion_percent": d["completion"], "completion_rule": pm.COMPLETION_NOTE,
           "by_status": {pm.STATUS_LABEL[k]: v for k, v in d["by_status"].items()}, "overdue": d["overdue"],
           "milestones": [{"title": m["title"], "due": m["due_date"], "completion_percent": m["completion"]}
                          for m in d["milestones"]]}
    if auth.page_allowed(conn, actor, "dashboard"):   # the leader's view adds what only the dashboard shows
        out.update(at_risk=[_short(t) for t in d["at_risk"]], waiting_for_review=[_short(t) for t in d["review"]],
                   deadline_requests=[{"task_id": r["task_id"], "task": r["title"], "by": r["requested_by"],
                                       "proposed": r["proposed"], "reason": r["reason"]} for r in d["requests"]],
                   workload_alphabetical=d["workload"],
                   check_ins=[{"severity": a["severity"], "about": a["text"], "suggestion": a["suggestion"],
                               "task_id": a["task_id"]} for a in pm_monitor.alerts(conn, project_id)])
    return out


def notifications(conn, actor: Actor) -> dict:
    return {"unread": [{"at": n["created_at"][:16], "task_id": n["task_id"], "text": n["text"]}
                       for n in pm.notifications(conn, actor)]}


# ---- write (a 'write' token, and only what the person may do anyway) -----------------------------------------------

def _run(fn, done: str) -> dict:
    try:
        fn()
    except PermissionDenied as exc:
        raise ToolRefused(str(exc)) from None
    except auth.AuthError as exc:    # rule violations: PMError, StaleTask, RateLimited
        raise ToolRefused(str(exc)) from None
    return {"ok": True, "done": done}


def add_comment(conn, actor: Actor, task_id: int, text: str) -> dict:
    return _run(lambda: pm.add_comment(conn, actor, int(task_id), text), f"Comment added to #{task_id}.")


def report_progress(conn, actor: Actor, task_id: int, progress: int, note: str = "", evidence_url: str = "") -> dict:
    return _run(lambda: pm.set_progress(conn, actor, int(task_id), progress, None, note, evidence_url),
                f"Progress on #{task_id} set to {progress}% (self-reported; only the leader verifies).")


def report_blocker(conn, actor: Actor, task_id: int, reason: str) -> dict:
    return _run(lambda: pm.report_blocker(conn, actor, int(task_id), reason),
                f"#{task_id} marked Blocked; the team leader was told.")


def send_for_review(conn, actor: Actor, task_id: int, note: str = "") -> dict:
    return _run(lambda: pm.set_status(conn, actor, int(task_id), "review", None, note),
                f"#{task_id} sent for review; the team leader decides whether it's Completed.")


def propose_tasks(conn, actor: Actor, notes: str, project_id: int | None = None, llm=None) -> dict:
    projects = pm.projects(conn)
    if project_id is None and len(projects) == 1:
        project_id = projects[0]["id"]
    if project_id is None:
        raise ToolRefused("Say which project: " + ", ".join(f"{p['id']} {p['name']}" for p in projects) + ".")
    holder: dict = {}

    def go():
        holder["id"] = pm_ai.propose_plan(conn, actor, int(project_id), notes, llm)
    _run(go, "")
    items = next(d for d in pm_ai.drafts(conn, int(project_id)) if d["id"] == holder["id"])["items"]
    return {"ok": True, "draft_id": holder["id"],
            "done": "Proposal saved. Nothing is created yet: open Project board → Plan with AI in TeamPulse to "
                    "review, edit and confirm it.",
            "proposed": [{"title": i["title"], "assignee": i["assignee"], "deadline": i["due"],
                          "ticked": i["selected"], "watch_out": i["flags"]} for i in items]}
