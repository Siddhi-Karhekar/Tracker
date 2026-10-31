"""Phase 5: proactive monitoring. A daily look at the plan that notices trouble early and suggests what a person
might do. It never changes a task: every alert is facts plus a suggestion, and acting on it is up to people.

Checks (all from the data, no AI):
  pace          at the recent rate of progress, the task would finish after its deadline
  behind        due within 3 days and under half done
  overdue       past the deadline
  dependency    a task waits for another that's due later than it is
  stale_blocker blocked for 3+ days with no update
  silent        in progress, nothing new for 7+ days (TeamPulse or linked GitHub/Notion/chat)
  review_wait   finished work waiting 2+ days for the leader's review
  request_wait  a deadline request waiting 2+ days for an answer
  overload      someone has a lot due in the next 7 days (workload, never performance)
  milestone     a milestone due within 7 days is under half complete
  unassigned    due within 14 days and nobody assigned
  sources       planned tasks that GitHub or Notion disagree with

run_checks() is safe to call any number of times (the poll loop and the dashboard both do). Each situation is one
alert: it's updated while it lasts, marked resolved when it ends, and reopened if it comes back. Dismissed alerts stay
quiet until their situation ends. New alerts produce one daily note for the leaders and, for "blocked with no update"
and "no update", one gentle nudge to the people on the task. Members can see the alerts about their own work.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from teampulse import pm, pm_sync
from teampulse.auth import Actor, audit, iso, require

PACE_LATE_DAYS = 2
STALE_BLOCKER_DAYS = 3
SILENT_DAYS = 7
WAIT_DAYS = 2
OVERLOAD_SIZE = 10            # total size of open work due within 7 days
OVERLOAD_COUNT = 4
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
NUDGE_KINDS = {"stale_blocker", "silent"}
KIND_LABEL = {"pace": "Pace", "behind": "Behind", "overdue": "Overdue", "dependency": "Order", "stale_blocker": "Blocked",
              "silent": "No update", "review_wait": "Review waiting", "request_wait": "Request waiting",
              "overload": "Workload", "milestone": "Milestone", "unassigned": "Unassigned", "sources": "GitHub/Notion"}


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _days(stamp: str | None, at: datetime) -> int | None:
    dt = _parse(stamp)
    return None if dt is None else max(0, int((at - dt).total_seconds() // 86400))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _people(t: dict) -> str:
    return " and ".join(t["assignees"]) if t["assignees"] else "whoever picks it up"


def last_activity(conn, task_id: int) -> str | None:
    """The latest thing anyone did on a task in TeamPulse: a change, a comment or a progress update."""
    row = conn.execute(
        "SELECT MAX(at) FROM (SELECT MAX(at) AS at FROM pm_history WHERE task_id = :t "
        "UNION ALL SELECT MAX(created_at) FROM pm_comments WHERE task_id = :t "
        "UNION ALL SELECT MAX(created_at) FROM pm_progress_updates WHERE task_id = :t)", {"t": task_id}).fetchone()
    return row[0]


def pace(conn, t: dict, at: datetime) -> dict | None:
    """Rate of self-reported progress over the last three weeks, and when the task would finish at that rate."""
    since = iso(at - timedelta(days=21))
    points = [(_parse(r[0]), r[1]) for r in conn.execute(
        "SELECT created_at, progress FROM pm_progress_updates WHERE task_id = ? AND kind = 'reported' AND created_at >= ? "
        "ORDER BY created_at", (t["id"], since))]
    if len(points) < 2:
        return None
    (start, p0), (end, p1) = points[0], points[-1]
    span = (end - start).total_seconds() / 86400
    if span < 2 or p1 <= p0 or p1 >= 100:
        return None
    rate = (p1 - p0) / span
    return {"from": p0, "to": p1, "days": round(span), "per_day": rate, "needs": math.ceil((100 - p1) / rate)}


# ---- the checks ----------------------------------------------------------------------------------------------

def find(conn, on: date | None = None, at: datetime | None = None) -> list[dict]:
    on = on or pm.today()
    at = at or datetime.now(timezone.utc)
    found: list[dict] = []

    def add(kind, severity, text, suggestion, task=None, member=None, project_id=None, key=None):
        found.append({"key": key or f"{kind}:{task['id'] if task else member}", "kind": kind, "severity": severity,
                      "text": text, "suggestion": suggestion, "task_id": task["id"] if task else None,
                      "member": member, "project_id": task["project_id"] if task else project_id})

    tasks = pm.tasks(conn, on=on)
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        if t["status"] == "done":
            continue
        ref, left, who = f"#{t['id']} “{t['title']}”", t["days_left"], _people(t)
        flagged = False
        if t["overdue"]:
            add("overdue", "high", f"{ref} is {_plural(-left, 'day')} past its deadline ({t['due_date']}).",
                f"Agree a new date with {who}, or cut it down to what can still ship.", t)
            flagged = True
        elif left is not None and t["status"] not in ("review",):
            p = pace(conn, t, at) if t["status"] in ("in_progress", "blocked", "todo") else None
            if p and p["needs"] - left >= PACE_LATE_DAYS:
                add("pace", "high" if left <= 3 else "medium",
                    f"{ref}: self-reported progress went from {p['from']}% to {p['to']}% in {_plural(p['days'], 'day')} "
                    f"(about {p['per_day']:.0f}% a day). At that pace it needs about {_plural(p['needs'], 'more day')}, "
                    f"but it's due in {_plural(left, 'day')}.",
                    f"Check with {who} whether to cut scope, add help, or move the deadline.", t)
                flagged = True
            if not flagged and 0 <= left <= 3 and max(t["reported_progress"], t["verified_progress"] or 0) < 50:
                add("behind", "high", f"{ref} is due in {_plural(left, 'day')} and is "
                                      f"{t['reported_progress']}% done (self-reported).",
                    f"Ask {who} what's left and whether the date still works.", t)
        for d in conn.execute("SELECT depends_on FROM pm_dependencies WHERE task_id = ?", (t["id"],)):
            dep = by_id.get(d[0])
            if dep and dep["status"] != "done" and t["due_date"] and (not dep["due_date"] or dep["due_date"] > t["due_date"]):
                add("dependency", "medium",
                    f"{ref} is due {t['due_date']} but waits for #{dep['id']} “{dep['title']}”, "
                    + (f"which is due later ({dep['due_date']})." if dep["due_date"] else "which has no deadline."),
                    "Move one of the two deadlines, or split out the part that doesn't need the other task.", t,
                    key=f"dependency:{t['id']}:{dep['id']}")
        quiet = _days(last_activity(conn, t["id"]), at)
        if t["status"] == "blocked" and quiet is not None and quiet >= STALE_BLOCKER_DAYS:
            add("stale_blocker", "high", f"{ref} has been blocked for {_plural(quiet, 'day')} with no update"
                                         + (f": {t['blocked_reason']}" if t["blocked_reason"] else "."),
                f"Ask {who} what would unblock it. If it's waiting on someone outside the team, note who and by when.", t)
        if t["status"] == "in_progress" and quiet is not None and quiet >= SILENT_DAYS:
            v = pm_sync.view(conn, t)
            source_quiet = _days(v["last_activity"], at) if v and not v["missing"] else None
            if source_quiet is None or source_quiet >= SILENT_DAYS:
                add("silent", "medium", f"{ref} is in progress, but nothing about it has changed in "
                                        f"{_plural(quiet, 'day')}.",
                    f"A quick check-in with {who}: is it still on track, or is something in the way?", t)
        if t["status"] == "review":
            row = conn.execute("SELECT MAX(at) FROM pm_history WHERE task_id = ? AND field = 'status' AND new_value = 'review'",
                               (t["id"],)).fetchone()
            waited = _days(row[0], at)
            if waited is not None and waited >= WAIT_DAYS:
                add("review_wait", "medium", f"{ref} has been waiting {_plural(waited, 'day')} for your review.",
                    "Approve it, or send it back with a note on what's missing.", t)
        if not t["assignees"] and left is not None and left <= 14:
            add("unassigned", "medium", f"{ref} is due in {_plural(left, 'day')} and nobody is assigned.",
                "Assign it, or move it to the backlog if it can wait.", t)

    for r in conn.execute("SELECT r.*, t.title, t.project_id FROM pm_requests r JOIN pm_tasks t ON t.id = r.task_id "
                          "WHERE r.status = 'pending' AND t.archived = 0"):
        waited = _days(r["created_at"], at)
        if waited is not None and waited >= WAIT_DAYS:
            add("request_wait", "medium", f"{r['requested_by']} asked {_plural(waited, 'day')} ago to move "
                                          f"#{r['task_id']} “{r['title']}” to {r['proposed']}.",
                "Approve or decline it, so they can plan.", by_id.get(r["task_id"]) or {"id": r["task_id"],
                                                                                    "project_id": r["project_id"]},
                key=f"request_wait:{r['id']}")

    soon = (on + timedelta(days=7)).isoformat()
    load = {}
    for t in tasks:
        if t["status"] != "done" and t["due_date"] and t["due_date"] <= soon:
            for name in t["assignees"]:
                size, count = load.get(name, (0, 0))
                load[name] = (size + (t["weight"] or 1), count + 1)
    if load:
        lightest = min(pm.members(conn), key=lambda n: (load.get(n, (0, 0))[0], n.lower()))
        for name, (size, count) in sorted(load.items()):
            if size >= OVERLOAD_SIZE or count >= OVERLOAD_COUNT:
                other = load.get(lightest, (0, 0))[0]
                add("overload", "medium",
                    f"{name} has {_plural(count, 'open task')} due in the next 7 days (total size {size}).",
                    "See whether something can move to later, or to someone with less due this week"
                    + (f" ({lightest} has {'nothing' if not other else f'size {other}'} due)." if lightest != name else "."),
                    member=name, key=f"overload:{name.lower()}")

    for p in pm.projects(conn):
        for m in pm.dashboard(conn, p["id"], on)["milestones"]:
            if m["due_date"] and m["due_date"] <= soon and m["tasks"] and m["completion"] < 50:
                days_to = (date.fromisoformat(m["due_date"]) - on).days
                add("milestone", "high" if days_to <= 3 else "medium",
                    f"Milestone “{m['title']}” is due in {_plural(days_to, 'day')} and is {m['completion']}% complete "
                    f"by size ({m['done']} of {m['tasks']} tasks).",
                    "Decide what's essential for it, and move the rest to the next milestone.", project_id=p["id"],
                    key=f"milestone:{m['id']}")
        disagree = pm_sync.overview(conn, p["id"])["disagree"]
        if disagree:
            add("sources", "low", f"{_plural(len(disagree), 'planned task')} in “{p['name']}” "
                                  f"disagree{'s' if len(disagree) == 1 else ''} with GitHub or Notion.",
                "Look at Team dashboard → GitHub & Notion and fix whichever side is out of date.",
                project_id=p["id"], key=f"sources:{p['id']}")
    return found


# ---- keeping track ---------------------------------------------------------------------------------------------

def run_checks(conn, on: date | None = None, at: datetime | None = None) -> dict:
    at = at or datetime.now(timezone.utc)
    stamp = iso(at)
    current = {a["key"]: a for a in find(conn, on, at)}
    new = []
    with conn:
        existing = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM pm_alerts")}
        for key, a in current.items():
            old = existing.get(key)
            if old is None or old["status"] == "resolved":
                if old is None:
                    conn.execute("INSERT INTO pm_alerts (key, kind, project_id, task_id, member, severity, text, "
                                 "suggestion, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (key, a["kind"], a["project_id"], a["task_id"], a["member"], a["severity"], a["text"],
                                  a["suggestion"], stamp, stamp))
                else:
                    conn.execute("UPDATE pm_alerts SET status = 'open', severity = ?, text = ?, suggestion = ?, "
                                 "first_seen = ?, last_seen = ?, resolved_at = NULL, snoozed_until = NULL, "
                                 "decided_by = NULL, note = NULL WHERE key = ?",
                                 (a["severity"], a["text"], a["suggestion"], stamp, stamp, key))
                new.append(a)
            else:
                conn.execute("UPDATE pm_alerts SET severity = ?, text = ?, suggestion = ?, last_seen = ? WHERE key = ?",
                             (a["severity"], a["text"], a["suggestion"], stamp, key))
        gone = [k for k, r in existing.items() if r["status"] != "resolved" and k not in current]
        conn.executemany("UPDATE pm_alerts SET status = 'resolved', resolved_at = ? WHERE key = ?",
                         [(stamp, k) for k in gone])
        day = at.date().isoformat()
        for a in new:
            if a["kind"] in NUDGE_KINDS and a["task_id"]:
                t = pm.task_detail(conn, a["task_id"])
                for name in t["assignees"]:
                    conn.execute("INSERT OR IGNORE INTO pm_notifications (recipient, task_id, text, created_at, dedupe) "
                                 "VALUES (?, ?, ?, ?, ?)",
                                 (name, t["id"], f"Daily check: “{t['title']}” hasn't had an update in a while. "
                                                 "A short note helps the team plan.", stamp,
                                  f"nudge:{a['key']}:{name.lower()}:{day}"))
        if new:
            conn.execute("INSERT OR IGNORE INTO pm_notifications (recipient, task_id, text, created_at, dedupe) "
                         "VALUES (?, NULL, ?, ?, ?)",
                         (pm.LEADERS, f"Daily check: {_plural(len(new), 'new thing')} to look at (Team dashboard → "
                                      "Check-ins).", stamp, f"digest:{day}"))
    return {"new": len(new), "open": len(current), "resolved": len(gone)}


def alerts(conn, project_id: int | None = None, member: str | None = None, include_hidden: bool = False,
           at: datetime | None = None) -> list[dict]:
    at = at or datetime.now(timezone.utc)
    sql, params = "SELECT * FROM pm_alerts WHERE status != 'resolved'", []
    if not include_hidden:
        sql += " AND status = 'open' AND (snoozed_until IS NULL OR snoozed_until <= ?)"
        params.append(iso(at))
    if project_id:
        sql += " AND (project_id = ? OR project_id IS NULL)"
        params.append(int(project_id))
    rows = [dict(r) for r in conn.execute(sql, params)]
    if member:   # a member sees what's said about their own work, nothing about anyone else
        mine = {t["id"] for t in pm.tasks(conn, member=member)}
        rows = [r for r in rows if r["task_id"] in mine or (r["member"] or "").lower() == member.lower()]
    return sorted(rows, key=lambda r: (SEVERITY_ORDER[r["severity"]], r["first_seen"]))


MEMBER_SUGGESTION = {
    "overload": "If this week is too much, tell your team leader early: something can move.",
    "pace": "If the date won't work, say so early: ask for a new deadline on the task.",
    "behind": "Add a short update on what's left, or ask for a new deadline on the task.",
    "overdue": "Add an update, or ask for a new deadline on the task.",
    "stale_blocker": "Add a short update: what would unblock it, and who you're waiting for.",
    "silent": "A short update on the task helps the team plan.",
    "review_wait": "It's with the team leader for review; nothing needed from you unless they send it back.",
    "request_wait": "Your request is with the team leader; you'll get a notification when they decide.",
    "dependency": "If you can start the parts that don't need the other task, say so on the task.",
    "unassigned": "If you could take it, say so on the task.",
    "milestone": "If anything of yours for this milestone is at risk, add an update early.",
    "sources": "If your GitHub issue or Notion page is out of date, update it.",
}


def member_suggestion(a: dict) -> str:
    """What the person doing the work sees: about their own next step, never about anyone else."""
    return MEMBER_SUGGESTION.get(a["kind"], "Add a short update on the task if anything has changed.")


def _alert(conn, alert_id: int) -> dict:
    row = conn.execute("SELECT * FROM pm_alerts WHERE id = ? AND status != 'resolved'", (int(alert_id),)).fetchone()
    if row is None:
        raise pm.PMError("That check-in has already been resolved.")
    return dict(row)


def dismiss(conn, actor: Actor, alert_id: int, note: str = "") -> None:
    """Leader: "seen it, it's fine". It stays quiet until the situation ends."""
    require(conn, actor, "pm.manage", f"alert {alert_id}")
    a = _alert(conn, alert_id)
    with conn:
        conn.execute("UPDATE pm_alerts SET status = 'dismissed', decided_by = ?, note = ? WHERE id = ?",
                     (actor.display_name, pm._text(note, "note", 300), a["id"]))
    audit(conn, actor, "pm.alert.dismiss", a["key"])


def snooze(conn, actor: Actor, alert_id: int, days: int = 3, at: datetime | None = None) -> None:
    require(conn, actor, "pm.manage", f"alert {alert_id}")
    days = max(1, min(int(days), 14))
    a = _alert(conn, alert_id)
    until = (at or datetime.now(timezone.utc)) + timedelta(days=days)
    with conn:
        conn.execute("UPDATE pm_alerts SET snoozed_until = ?, decided_by = ? WHERE id = ?",
                     (iso(until), actor.display_name, a["id"]))
    audit(conn, actor, "pm.alert.snooze", a["key"], detail=str(days))


def remind(conn, actor: Actor, alert_id: int) -> int:
    """Leader: send the people on the task a short, friendly note. The only thing a check-in can trigger, and only
    when a person presses the button."""
    require(conn, actor, "pm.manage", f"alert {alert_id}")
    pm._rate(conn, actor)
    a = _alert(conn, alert_id)
    people = pm.task_detail(conn, a["task_id"])["assignees"] if a["task_id"] else [a["member"]] if a["member"] else []
    if not people:
        raise pm.PMError("Nobody to remind: assign the task first.")
    title = pm.task_detail(conn, a["task_id"])["title"] if a["task_id"] else None
    text = (f"{pm._who(actor)} asked for a quick update on “{title}”." if title else
            f"{pm._who(actor)} would like to talk about your workload this week.")
    with conn:
        pm._notify(conn, actor, people, a["task_id"], text)
    audit(conn, actor, "pm.alert.remind", a["key"])
    return len(people)
