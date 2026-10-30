"""Project management rules, enforced on the server: who may do what, the workflow, dependencies, deadline requests,
notifications, two people editing at once, and input validation."""
import threading
from datetime import date, timedelta

import pytest

from teampulse import auth, db, pm
from teampulse.auth import Actor, PermissionDenied
from teampulse.seed_demo import PEOPLE

LEADER = Actor("u-neha", "neha", "Neha Sharma", "leader", "Neha")
KABIR = Actor("u-kabir", "kabir", "Kabir Rao", "member", "Kabir")
AMAN = Actor("u-aman", "aman", "Aman", "member", "Aman")
RIYA = Actor("u-riya", "riya", "Riya", "member", "Riya")
UNLINKED = Actor("u-x", "x", "X", "member", None)


def day(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "pm.db"))
    with c:
        c.executemany("INSERT INTO members (display_name, consent) VALUES (?, 1)", [(n,) for n in PEOPLE])
    return c


@pytest.fixture()
def project(conn):
    return pm.create_project(conn, LEADER, "App", "", day(-5), day(30))


def new(conn, project, title="Task", who=("Kabir",), **kw):
    return pm.create_task(conn, LEADER, project, title, assign_to=list(who), **kw)


def status(conn, tid):
    return conn.execute("SELECT status FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0]


def denied(conn, action=None):
    sql = "SELECT COUNT(*) FROM audit_log WHERE outcome = 'denied'" + (" AND action = ?" if action else "")
    return conn.execute(sql, (action,) if action else ()).fetchone()[0]


# ---- permissions ------------------------------------------------------------------------------------------

def test_members_cannot_plan(conn, project):
    tid = new(conn, project)
    for attempt in (lambda: pm.create_project(conn, KABIR, "Mine"),
                    lambda: pm.create_task(conn, KABIR, project, "Sneaky"),
                    lambda: pm.add_milestone(conn, KABIR, project, "M"),
                    lambda: pm.update_task(conn, KABIR, tid, due_date=day(60)),
                    lambda: pm.update_task(conn, KABIR, tid, assignees=["Kabir", "Riya"]),
                    lambda: pm.archive_task(conn, KABIR, tid)):
        with pytest.raises(PermissionDenied):
            attempt()
    assert denied(conn) == 6
    assert conn.execute("SELECT COUNT(*) FROM pm_tasks").fetchone()[0] == 1


def test_member_updates_only_their_own_tasks(conn, project):
    mine, riyas = new(conn, project, "Mine"), new(conn, project, "Riya's", ["Riya"])
    pm.set_status(conn, KABIR, mine, "in_progress")
    pm.set_progress(conn, KABIR, mine, 40)
    for attempt in (lambda: pm.set_status(conn, KABIR, riyas, "in_progress"),
                    lambda: pm.set_progress(conn, KABIR, riyas, 90),
                    lambda: pm.report_blocker(conn, KABIR, riyas, "x"),
                    lambda: pm.request_deadline(conn, KABIR, riyas, day(40), "x"),
                    lambda: pm.create_task(conn, KABIR, project, "sub", parent_id=riyas)):
        with pytest.raises(PermissionDenied, match="assigned to you"):
            attempt()
    assert status(conn, riyas) == "todo" and denied(conn) == 5
    pm.add_comment(conn, KABIR, riyas, "Need a hand?")          # comments are open to the whole team


def test_member_cannot_complete_or_reopen_but_leader_can(conn, project):
    tid = new(conn, project)
    pm.set_status(conn, KABIR, tid, "review")
    with pytest.raises(PermissionDenied, match="Under review"):
        pm.set_status(conn, KABIR, tid, "done")
    pm.set_status(conn, LEADER, tid, "done")
    row = conn.execute("SELECT status, reported_progress, completed_at FROM pm_tasks WHERE id = ?", (tid,)).fetchone()
    assert row[0] == "done" and row[1] == 100 and row[2]
    with pytest.raises(PermissionDenied, match="reopen"):
        pm.set_status(conn, KABIR, tid, "in_progress")
    pm.set_status(conn, LEADER, tid, "in_progress")
    assert conn.execute("SELECT completed_at FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0] is None


def test_unlinked_and_signed_out_accounts_cannot_work(conn, project):
    tid = new(conn, project)
    with pytest.raises(PermissionDenied):
        pm.set_progress(conn, UNLINKED, tid, 10)
    with pytest.raises(PermissionDenied):
        pm.add_comment(conn, None, tid, "hi")
    changing = Actor("u-k", "kabir", "Kabir", "member", "Kabir", must_change=True)
    with pytest.raises(PermissionDenied):
        pm.set_progress(conn, changing, tid, 10)


def test_member_can_break_down_their_own_task(conn, project):
    tid = new(conn, project)
    sub = pm.create_task(conn, KABIR, project, "Step 1", parent_id=tid, assign_to=["Riya"], due=day(1))
    row = conn.execute("SELECT parent_id, due_date FROM pm_tasks WHERE id = ?", (sub,)).fetchone()
    assert row[0] == tid and row[1] is None                         # members can't set deadlines or assign
    assert conn.execute("SELECT COUNT(*) FROM pm_assignees WHERE task_id = ?", (sub,)).fetchone()[0] == 0
    pm.set_status(conn, KABIR, sub, "done")                          # members tick their own subtasks
    detail = pm.task_detail(conn, tid)
    assert detail["subtasks_total"] == 1 and detail["measured_progress"] == 100


def test_shared_task_both_assignees_can_work(conn, project):
    tid = new(conn, project, who=["Kabir", "Aman"])
    pm.set_progress(conn, KABIR, tid, 30)
    pm.set_progress(conn, AMAN, tid, 50)
    with pytest.raises(PermissionDenied):
        pm.set_progress(conn, RIYA, tid, 70)


# ---- workflow rules ------------------------------------------------------------------------------------------

def test_blocked_needs_a_reason_and_tells_the_leader(conn, project):
    tid = new(conn, project)
    with pytest.raises(pm.PMError, match="blocking"):
        pm.set_status(conn, KABIR, tid, "blocked")
    pm.report_blocker(conn, KABIR, tid, "No dataset yet")
    assert conn.execute("SELECT blocked_reason FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0] == "No dataset yet"
    assert conn.execute("SELECT kind FROM pm_comments WHERE task_id = ?", (tid,)).fetchone()[0] == "blocker"
    assert [n["text"] for n in pm.notifications(conn, LEADER)][0].startswith("Kabir is blocked")
    pm.set_status(conn, KABIR, tid, "in_progress")
    assert conn.execute("SELECT blocked_reason FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0] is None


def test_dependencies_gate_starting_and_completing(conn, project):
    first = new(conn, project, "API")
    second = new(conn, project, "Screen", depends_on=[first])
    with pytest.raises(pm.PMError, match="waiting for"):
        pm.set_status(conn, KABIR, second, "in_progress")
    pm.set_status(conn, LEADER, first, "done")
    pm.set_status(conn, KABIR, second, "in_progress")


def test_open_subtasks_block_completion(conn, project):
    tid = new(conn, project)
    pm.create_task(conn, KABIR, project, "Step", parent_id=tid)
    with pytest.raises(pm.PMError, match="subtasks"):
        pm.set_status(conn, LEADER, tid, "done")


def test_dependency_rules(conn, project):
    a, b = new(conn, project, "A"), new(conn, project, "B")
    pm.update_task(conn, LEADER, b, depends_on=[a])
    with pytest.raises(pm.PMError, match="circle"):
        pm.update_task(conn, LEADER, a, depends_on=[b])
    with pytest.raises(pm.PMError, match="itself"):
        pm.update_task(conn, LEADER, a, depends_on=[a])
    other = pm.create_task(conn, LEADER, pm.create_project(conn, LEADER, "Other"), "X")
    with pytest.raises(pm.PMError, match="same project"):
        pm.update_task(conn, LEADER, a, depends_on=[other])


def test_validation(conn, project):
    for bad in (lambda: new(conn, project, ""), lambda: new(conn, project, "x" * 201),
                lambda: new(conn, project, priority="critical"), lambda: new(conn, project, due="next friday"),
                lambda: new(conn, project, who=["Mallory"]), lambda: new(conn, project, status="done"),
                lambda: new(conn, project, due=day(1), start=day(5)),
                lambda: pm.create_project(conn, LEADER, "P", start=day(5), end=day(1))):
        with pytest.raises(pm.PMError):
            bad()
    tid = new(conn, project)
    for value in (-1, 101, "lots"):
        with pytest.raises(pm.PMError):
            pm.set_progress(conn, KABIR, tid, value)
    assert "\x00" not in pm.task_detail(conn, new(conn, project, "ok\x00title"))["title"]


# ---- deadline requests and notifications -----------------------------------------------------------------------

def test_deadline_request_flow(conn, project):
    tid = new(conn, project, due=day(3))
    with pytest.raises(pm.PMError, match="reason"):
        pm.request_deadline(conn, KABIR, tid, day(9), "")
    rid = pm.request_deadline(conn, KABIR, tid, day(9), "Data arrives late")
    with pytest.raises(pm.PMError, match="already"):
        pm.request_deadline(conn, KABIR, tid, day(10), "again")
    with pytest.raises(PermissionDenied):
        pm.decide_request(conn, KABIR, rid, True)               # can't approve your own request
    pm.decide_request(conn, LEADER, rid, True)
    assert conn.execute("SELECT due_date FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0] == day(9)
    with pytest.raises(pm.PMError, match="already decided"):
        pm.decide_request(conn, LEADER, rid, False)
    assert any("approved your new deadline" in n["text"] for n in pm.notifications(conn, KABIR))


def test_notifications_are_private_and_skip_yourself(conn, project):
    tid = new(conn, project, who=["Kabir"])
    pm.add_comment(conn, KABIR, tid, "note to self, cc @Riya")
    assert not any("commented" in n["text"] for n in pm.notifications(conn, KABIR))   # no echo of your own action
    assert any("commented" in n["text"] for n in pm.notifications(conn, RIYA))        # @mention
    assert all(n["recipient"] != pm.LEADERS for n in pm.notifications(conn, KABIR))  # leaders' inbox stays theirs
    assert pm.mark_read(conn, RIYA) >= 1 and not pm.notifications(conn, RIYA)
    assert pm.notifications(conn, KABIR)                                            # Riya reading doesn't touch Kabir's


def test_assignment_change_notifies_both_sides(conn, project):
    tid = new(conn, project, who=["Kabir"])
    pm.update_task(conn, LEADER, tid, assignees=["Riya"])
    assert any("no longer assigned" in n["text"] for n in pm.notifications(conn, KABIR))
    assert any("assigned you" in n["text"] for n in pm.notifications(conn, RIYA))
    hist = conn.execute("SELECT old_value, new_value FROM pm_history WHERE field = 'assignees'").fetchone()
    assert tuple(hist) == ("Kabir", "Riya")


# ---- two people at once -----------------------------------------------------------------------------------------

def test_stale_edit_is_refused_not_silently_overwritten(conn, project):
    tid = new(conn, project, who=["Kabir", "Aman"])
    opened = pm.task_detail(conn, tid)["version"]               # both open the task
    pm.set_progress(conn, AMAN, tid, 50, opened)                 # Aman saves first
    with pytest.raises(pm.StaleTask):
        pm.set_progress(conn, KABIR, tid, 20, opened)            # Kabir's view is out of date
    with pytest.raises(pm.StaleTask):
        pm.update_task(conn, LEADER, tid, opened, title="Renamed")
    assert conn.execute("SELECT reported_progress, title FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0] == 50


def test_parallel_updates_from_threads_all_land(tmp_path):
    c = db.connect(str(tmp_path / "par.db"))
    with c:
        c.executemany("INSERT INTO members (display_name, consent) VALUES (?, 1)", [(n,) for n in PEOPLE])
    pid = pm.create_project(c, LEADER, "P")
    ids = [pm.create_task(c, LEADER, pid, f"T{i}", assign_to=["Kabir"]) for i in range(8)]
    errors = []

    def work(tid):
        try:
            for p in (10, 20, 30):
                pm.set_progress(c, KABIR, tid, p)
            pm.add_comment(c, AMAN, tid, "ok")
        except Exception as exc:   # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(t,)) for t in ids]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert c.execute("SELECT COUNT(*) FROM pm_tasks WHERE reported_progress = 30").fetchone()[0] == 8
    assert c.execute("SELECT COUNT(*) FROM pm_comments").fetchone()[0] == 8


def test_rate_limit(conn, project, monkeypatch):
    from teampulse.actions import RateLimited
    monkeypatch.setattr(pm, "WRITES_PER_10_MIN", 3)
    tid = new(conn, project)
    pm.add_comment(conn, KABIR, tid, "1")
    pm.add_comment(conn, KABIR, tid, "2")
    pm.add_comment(conn, KABIR, tid, "3")
    with pytest.raises(RateLimited):
        pm.add_comment(conn, KABIR, tid, "4")


# ---- reading: workspace and dashboard ---------------------------------------------------------------------------

def test_workspace_and_dashboard(conn, project):
    late = new(conn, project, "Late", due=day(-2))
    soon = new(conn, project, "Soon", due=day(2), priority="urgent")
    new(conn, project, "Riya's", ["Riya"], due=day(1))
    pm.create_task(conn, KABIR, project, "s1", parent_id=soon)
    pm.set_progress(conn, KABIR, soon, 90)
    w = pm.workspace(conn, "Kabir")
    assert [t["title"] for t in w["sections"]["overdue"]] == ["Late"]
    assert [t["title"] for t in w["sections"]["due_soon"]] == ["Soon"]
    assert [t["title"] for t in w["sections"]["high_priority"]] == ["Soon"]
    assert w["metrics"]["assigned"] == 2 and w["metrics"]["overdue"] == 1
    assert "Riya's" not in {t["title"] for s in w["sections"].values() for t in s}
    soon_task = next(t for t in pm.tasks(conn) if t["id"] == soon)
    assert soon_task["measured_progress"] == 0 and any("only 0% of subtasks" in r for r in soon_task["risks"])
    d = pm.dashboard(conn, project)
    assert d["total"] == 3 and d["overdue"] == 1
    assert [w["member"] for w in d["workload"]] == sorted(PEOPLE, key=str.lower)    # alphabetical, not ranked
    assert {t["title"] for t in d["at_risk"]} >= {"Late", "Soon"}


def test_risk_wording_is_about_the_work_not_the_person():
    t = {"status": "in_progress", "days_left": -3, "overdue": True, "reported_progress": 90, "measured_progress": 20,
         "blocked_reason": None, "waiting_on": [], "assignees": ["Kabir"], "parent_id": None}
    text = " ".join(pm.risks(t)).lower()
    for word in ("lazy", "slow", "poor", "bad", "kabir"):
        assert word not in text


def test_archived_tasks_disappear(conn, project):
    tid = new(conn, project)
    sub = pm.create_task(conn, KABIR, project, "s", parent_id=tid)
    pm.archive_task(conn, LEADER, tid)
    assert not pm.tasks(conn, include_subtasks=True)
    with pytest.raises(pm.PMError):
        pm.set_status(conn, KABIR, sub, "done")


def test_pages_are_personal_or_leader_only(conn):
    for page in ("workspace", "board"):
        assert auth.page_allowed(conn, KABIR, page) and auth.page_allowed(conn, LEADER, page)
    assert not auth.page_allowed(conn, KABIR, "dashboard") and auth.page_allowed(conn, LEADER, "dashboard")
    from teampulse import actions
    actions.set_member_pages(conn, LEADER, ["dashboard", "home"])        # can't be granted to members
    assert not auth.page_allowed(conn, KABIR, "dashboard")


# ---- progress evidence, verified progress, weighted completion --------------------------------------------------

def test_progress_update_keeps_note_and_evidence(conn, project):
    tid = new(conn, project)
    pm.set_progress(conn, KABIR, tid, 40, note="API done", evidence_url="https://github.com/a/b/pull/3")
    pm.set_progress(conn, KABIR, tid, 40, note="Tests added")                  # same number, new evidence of work
    updates = pm.task_detail(conn, tid)["updates"]
    assert [(u["kind"], u["progress"], u["note"]) for u in updates] == [("reported", 40, "Tests added"),
                                                                        ("reported", 40, "API done")]
    assert updates[1]["evidence_url"] == "https://github.com/a/b/pull/3"
    for bad in ("javascript:alert(1)", "data:text/html,x", "github.com/no-scheme", "https://x.y/" + "a" * 500):
        with pytest.raises(pm.PMError, match="evidence link"):
            pm.set_progress(conn, KABIR, tid, 50, evidence_url=bad)


def test_only_leaders_verify_and_it_stays_separate(conn, project):
    tid = new(conn, project)
    pm.set_progress(conn, KABIR, tid, 90)
    with pytest.raises(PermissionDenied):
        pm.verify_progress(conn, KABIR, tid, 90)                                  # no verifying your own work
    pm.verify_progress(conn, LEADER, tid, 30, "Only the UI exists")
    t = pm.task_detail(conn, tid)
    assert (t["reported_progress"], t["verified_progress"], t["verified_by"]) == (90, 30, "Neha Sharma")
    assert t["status"] != "done"                                                   # verifying never completes a task
    assert any("leader verified 30%" in r for r in t["risks"])
    assert any("30% verified" in n["text"] for n in pm.notifications(conn, KABIR))


def test_completing_verifies_and_reopening_clears_it(conn, project):
    tid = new(conn, project)
    pm.set_status(conn, LEADER, tid, "done")
    assert pm.task_detail(conn, tid)["verified_progress"] == 100
    pm.set_status(conn, LEADER, tid, "in_progress")
    assert pm.task_detail(conn, tid)["verified_progress"] is None


def test_completion_counts_finished_size_not_average_percentages(conn, project):
    big = new(conn, project, "Big", weight=8)
    small = new(conn, project, "Small", weight=2)
    pm.set_progress(conn, KABIR, big, 90)                                        # 90% reported adds nothing
    pm.set_status(conn, LEADER, small, "done")
    assert pm.dashboard(conn, project)["completion"] == 20                       # 2 of 10 size units finished
    assert pm.workspace(conn, "Kabir")["metrics"]["progress"] == 20
    with pytest.raises(pm.PMError, match="Size"):
        new(conn, project, "Odd", weight=4)
    pm.update_task(conn, LEADER, big, weight=2)
    assert pm.dashboard(conn, project)["completion"] == 50


# ---- reminders and calendar ----------------------------------------------------------------------------------

def test_reminders_once_a_day_and_never_for_done_work(conn, project):
    tomorrow = new(conn, project, "Tomorrow", due=day(1))
    late = new(conn, project, "Late", due=day(-2), who=["Kabir", "Riya"])
    done = new(conn, project, "Done", due=day(-1))
    new(conn, project, "Later", due=day(9))
    pm.set_status(conn, LEADER, done, "done")
    assert pm.send_reminders(conn) == 4          # Kabir: tomorrow + late; Riya: late; leaders: late
    assert pm.send_reminders(conn) == 0          # same day: nothing new
    texts = [n["text"] for n in pm.notifications(conn, KABIR)]
    assert any("“Tomorrow” is due tomorrow" in x for x in texts) and any("2 days past" in x for x in texts)
    assert not any("“Done” is" in x for x in texts)
    assert pm.send_reminders(conn, on=date.today() + timedelta(days=1)) == 3   # next day: members again, not leaders
    del tomorrow, late


def test_calendar_month(conn, project):
    due = date.today().replace(day=15)
    tid = new(conn, project, "Mid-month", due=due.isoformat())
    pm.add_milestone(conn, LEADER, project, "Demo", due.isoformat())
    days = pm.calendar(conn, due.year, due.month, project_id=project)
    assert [t["id"] for t in days[15]["tasks"]] == [tid] and days[15]["milestones"][0]["title"] == "Demo"
    assert 15 not in pm.calendar(conn, due.year, due.month, member="Riya")
    assert 15 in pm.calendar(conn, due.year, due.month, member="Kabir")


def test_phase1_database_upgrades_in_place(tmp_path):
    """A database created before verified progress and weights existed gets the new columns on start."""
    import sqlite3
    path = str(tmp_path / "old.db")
    db.connect(path)
    raw = sqlite3.connect(path)
    raw.execute("DROP INDEX idx_pm_notifications_dedupe")
    for col in ("weight", "verified_progress", "verified_by", "verified_at"):
        raw.execute(f"ALTER TABLE pm_tasks DROP COLUMN {col}")
    raw.execute("ALTER TABLE pm_notifications DROP COLUMN dedupe")
    raw.commit()
    raw.close()
    c = db.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(pm_tasks)")}
    assert {"weight", "verified_progress"} <= cols
