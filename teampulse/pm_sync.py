"""Phase 3: link planned tasks to their GitHub issue or Notion page, and bring the evidence in.

The project database is the source of truth for assignments, deadlines and status. GitHub, Notion and chat are
evidence: what they say is shown next to the planned task, and where they disagree TeamPulse says so, but nothing
here ever changes a planned task by itself. Copying a value across (a deadline) or importing source tasks is an
explicit action by a person, checked and logged like every other change.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from teampulse import claims, pm
from teampulse.auth import Actor, audit, iso, now, require
from teampulse.tasks import title_similarity

SOURCES = {"github": "GitHub", "notion": "Notion", "chat": "Chat"}
FINISHED = {"done", "dropped"}
WORD = {"todo": "to do", "in_progress": "in progress", "in_review": "in review", "blocked": "blocked",
        "done": "done", "dropped": "dropped", "open": "open"}
QUIET_DAYS = 7
EVIDENCE_ROLES = ("pr", "commit", "review", "comment", "chat", "issue", "notion_task")


# ---- linking ---------------------------------------------------------------------------------------------

def source_tasks(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, title, url, github_number FROM tasks "
                                          "ORDER BY github_number IS NULL, github_number, title")]


def linked_source(conn, task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM pm_source_links WHERE task_id = ?", (int(task_id),)).fetchone()
    return dict(row) if row else None


def link(conn, actor: Actor, task_id: int, source_task: str) -> None:
    """Leaders, or the task's assignees (they know which issue is theirs)."""
    task = pm._row(conn, task_id)
    pm._require_work(conn, actor, task, "pm.link")
    if task["parent_id"]:
        raise pm.PMError("Link the main task, not a subtask.")
    source = conn.execute("SELECT id, title FROM tasks WHERE id = ?", (str(source_task),)).fetchone()
    if source is None:
        raise pm.PMError("That GitHub issue or Notion task isn't in TeamPulse. Rebuild source tasks and try again.")
    taken = conn.execute("SELECT task_id FROM pm_source_links WHERE source_task = ? AND task_id != ?",
                         (source["id"], task["id"])).fetchone()
    if taken:
        raise pm.PMError(f"That one is already linked to task #{taken[0]}.")
    old = linked_source(conn, task["id"])
    with conn:
        conn.execute("INSERT OR REPLACE INTO pm_source_links (task_id, source_task, source_title, linked_by, linked_at) "
                     "VALUES (?, ?, ?, ?, ?)", (task["id"], source["id"], source["title"], actor.display_name,
                                                iso(now())))
        pm._history(conn, task["id"], actor, "linked_to", old["source_title"] if old else None, source["title"])
    audit(conn, actor, "pm.link", f"task {task['id']}", detail=source["id"])


def unlink(conn, actor: Actor, task_id: int) -> None:
    task = pm._row(conn, task_id)
    pm._require_work(conn, actor, task, "pm.unlink")
    old = linked_source(conn, task["id"])
    if not old:
        return
    with conn:
        conn.execute("DELETE FROM pm_source_links WHERE task_id = ?", (task["id"],))
        pm._history(conn, task["id"], actor, "linked_to", old["source_title"], None)
    audit(conn, actor, "pm.unlink", f"task {task['id']}", detail=old["source_task"])


def suggestions(conn, task: dict, k: int = 3) -> list[dict]:
    """Unlinked source tasks whose title looks like this one's."""
    taken = {r[0] for r in conn.execute("SELECT source_task FROM pm_source_links")}
    scored = [(title_similarity(task["title"], s["title"] or ""), s) for s in source_tasks(conn) if s["id"] not in taken]
    return [s for score, s in sorted(scored, key=lambda x: -x[0]) if score >= 0.5][:k]


# ---- what the sources say ----------------------------------------------------------------------------------

def _card(conn, source_task: str) -> dict | None:
    return next((c for c in claims.task_cards(conn) if c["id"] == source_task), None)


def _names(value: str | None) -> set[str]:
    return {n.strip().lower() for n in (value or "").split(",") if n.strip()}


def differences(task: dict, card: dict) -> list[dict]:
    """Where GitHub or Notion disagree with the plan. Facts with both sides, never a judgement about a person."""
    out = []
    done_here = task["status"] == "done"
    here = pm.STATUS_LABEL[task["status"]]
    for source in ("github", "notion"):
        name = SOURCES[source]
        status = (card["status"].get(source) or {}).get("value")
        if status:
            finished = status in FINISHED
            if done_here and not finished:
                out.append({"kind": "status", "source": source,
                            "text": f"Completed here, but {name} still says “{WORD.get(status, status)}”."})
            elif finished and not done_here:
                out.append({"kind": "status", "source": source,
                            "text": f"{name} says “{WORD.get(status, status)}”, but here it's {here}. If the work "
                                    f"really is finished, send it for review."})
        owner = (card["owner"].get(source) or {}).get("value")
        if owner and task["assignees"] and _names(owner) != {a.lower() for a in task["assignees"]}:
            out.append({"kind": "owner", "source": source,
                        "text": f"{name} has it assigned to {owner}; here it's {', '.join(task['assignees'])}."})
        deadline = (card["deadline"].get(source) or {}).get("value")
        if deadline and deadline[:10] != (task["due_date"] or ""):
            out.append({"kind": "deadline", "source": source, "value": deadline[:10],
                        "text": f"{name}'s deadline is {deadline[:10]}; here it's {task['due_date'] or 'not set'}."})
    return out


def view(conn, task: dict) -> dict | None:
    """Everything the sources say about a planned task, or None if it isn't linked."""
    link_ = linked_source(conn, task["id"])
    if not link_:
        return None
    source = conn.execute("SELECT * FROM tasks WHERE id = ?", (link_["source_task"],)).fetchone()
    if source is None:
        return {"link": link_, "missing": True}
    card = _card(conn, source["id"]) or {"status": {}, "owner": {}, "deadline": {}, "links": Counter(),
                                         "workers": Counter(), "last_activity": None}
    people = claims.People(conn)
    evidence = []
    for r in conn.execute(
            "SELECT tl.role, e.source, e.title, e.body, e.url, e.author, e.author_name, e.created_at, e.updated_at "
            "FROM task_links tl JOIN events e ON e.id = tl.event_id WHERE tl.task_id = ? "
            "ORDER BY COALESCE(e.updated_at, e.created_at) DESC LIMIT 12", (source["id"],)):
        evidence.append({"role": r["role"], "source": r["source"], "url": r["url"],
                         "title": r["title"] or (r["body"] or "")[:120],
                         "who": people.resolve(r["author_name"] or r["author"]) or "",
                         "at": r["updated_at"] or r["created_at"]})
    last = card["last_activity"]
    quiet = None
    if last:
        try:
            quiet = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
        except ValueError:
            quiet = None
    diffs = differences(task, card)
    if task["status"] == "in_progress" and quiet is not None and quiet >= QUIET_DAYS:
        diffs.append({"kind": "quiet", "source": "github",
                      "text": f"In progress here, but nothing linked to it has changed in {quiet} days."})
    return {"link": link_, "missing": False, "source": dict(source), "card": card, "evidence": evidence,
            "counts": {r: card["links"].get(r, 0) for r in EVIDENCE_ROLES}, "workers": dict(card["workers"]),
            "last_activity": last, "differences": diffs}


def overview(conn, project_id: int | None = None) -> dict:
    """For the leader: planned tasks whose sources disagree, and source tasks nobody has planned yet."""
    linked, disagree = 0, []
    for t in pm.tasks(conn, project_id=project_id):
        v = view(conn, t)
        if v is None:
            continue
        linked += 1
        if v["missing"]:
            disagree.append({"task": t, "differences": [{"kind": "missing", "source": "",
                             "text": f"The linked item “{v['link']['source_title']}” is no longer in GitHub or Notion."}]})
        elif v["differences"]:
            disagree.append({"task": t, "differences": v["differences"]})
    taken = {r[0] for r in conn.execute("SELECT source_task FROM pm_source_links")}
    unplanned = [s for s in source_tasks(conn) if s["id"] not in taken]
    return {"linked": linked, "disagree": disagree, "unplanned": unplanned}


# ---- deliberate changes ------------------------------------------------------------------------------------

def use_source_deadline(conn, actor: Actor, task_id: int, source: str, version: int | None = None) -> None:
    """The leader copies GitHub's or Notion's deadline into the plan (an ordinary, logged edit)."""
    require(conn, actor, "pm.manage", f"task {task_id}")
    task = pm.task_detail(conn, task_id)
    v = view(conn, task)
    if not v or v["missing"]:
        raise pm.PMError("This task isn't linked to GitHub or Notion.")
    value = ((v["card"]["deadline"].get(source) or {}).get("value") or "")[:10]
    if not value:
        raise pm.PMError(f"{SOURCES.get(source, source)} has no deadline for it.")
    pm.update_task(conn, actor, task["id"], version, due_date=value)


def _map_status(card: dict) -> str:
    values = [(card["status"].get(s) or {}).get("value") for s in ("github", "notion")]
    if any(v in FINISHED for v in values if v) and all(v in FINISHED for v in values if v):
        return "done"
    if "in_review" in values:
        return "review"
    if "in_progress" in values:
        return "in_progress"
    return "todo"


def import_tasks(conn, actor: Actor, project_id: int, source_ids: list[str]) -> list[int]:
    """Leader: create planned tasks from GitHub issues / Notion tasks, pre-filled with title, owners who are on the
    team, the deadline (Notion's, else GitHub's) and status, and linked. Every value can be edited afterwards."""
    require(conn, actor, "pm.manage", "import tasks")
    team = {m.lower(): m for m in pm.members(conn)}
    taken = {r[0] for r in conn.execute("SELECT source_task FROM pm_source_links")}
    created = []
    for sid in source_ids:
        if sid in taken:
            continue
        source = conn.execute("SELECT * FROM tasks WHERE id = ?", (sid,)).fetchone()
        if source is None:
            raise pm.PMError("One of those GitHub issues or Notion tasks isn't in TeamPulse any more.")
        card = _card(conn, sid) or {"status": {}, "owner": {}, "deadline": {}}
        owners = []
        for s in ("notion", "github"):
            owners += [team[n] for n in _names((card["owner"].get(s) or {}).get("value")) if n in team]
        due = next(((card["deadline"].get(s) or {}).get("value") or "")[:10] or None for s in ("notion", "github")
                    if (card["deadline"].get(s) or {}).get("value"))
        status = _map_status(card)
        number = f" (#{source['github_number']})" if source["github_number"] else ""
        tid = pm.create_task(conn, actor, project_id, source["title"] or sid, f"Imported from {source['url'] or sid}"
                             f"{number}.", list(dict.fromkeys(owners)), due=due,
                             status="todo" if status == "done" else status)
        with conn:
            conn.execute("INSERT INTO pm_source_links (task_id, source_task, source_title, linked_by, linked_at) "
                         "VALUES (?, ?, ?, ?, ?)", (tid, sid, source["title"], actor.display_name, iso(now())))
        if status == "done":
            pm.set_status(conn, actor, tid, "done", note="Already finished in GitHub/Notion when imported.")
        audit(conn, actor, "pm.import", f"task {tid}", detail=sid)
        created.append(tid)
    return created
