"""Phase 5: the daily check notices trouble early and suggests; it never changes a task. Each check, the alert
lifecycle (new, updated, resolved, reopened, dismissed, snoozed), notifications, privacy, wording and the pages."""
from datetime import date, datetime, timedelta, timezone

import pytest

from teampulse import auth, claims, db, guardrails, pm, pm_monitor
from teampulse.auth import Actor, PermissionDenied, iso
from teampulse.seed_demo import PEOPLE, seed
from tests.test_roles_ui import LEADER_PW, MEMBER_PW, page, team, text  # noqa: F401  (shared fixture)

LEADER = Actor("u-neha", "neha", "Neha Sharma", "leader", "Neha")
KABIR = Actor("u-kabir", "kabir", "Kabir Rao", "member", "Kabir")
NOW = datetime.now(timezone.utc)
TODAY = date.today()


def day(n):
    return (TODAY + timedelta(days=n)).isoformat()


def ago(days):
    return iso(NOW - timedelta(days=days))


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "m.db"))
    with c:
        c.executemany("INSERT INTO members (display_name, consent) VALUES (?, 1)", [(n,) for n in PEOPLE])
    return c


@pytest.fixture()
def pid(conn):
    return pm.create_project(conn, LEADER, "App")


def new(conn, pid, title="Task", who=("Kabir",), **kw):
    return pm.create_task(conn, LEADER, pid, title, assign_to=list(who), **kw)


def backdate(conn, task_id, days):
    with conn:
        for table, col in (("pm_history", "at"), ("pm_comments", "created_at"), ("pm_progress_updates", "created_at")):
            conn.execute(f"UPDATE {table} SET {col} = ? WHERE task_id = ?", (ago(days), task_id))


def kinds(conn, pid=None):
    return {(a["kind"], a["task_id"]) for a in pm_monitor.find(conn)}


def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM pm_tasks ORDER BY id")], \
        [tuple(r) for r in conn.execute("SELECT * FROM pm_assignees ORDER BY task_id, member")]


# ---- each check -----------------------------------------------------------------------------------------------

def test_pace_forecast(conn, pid):
    tid = new(conn, pid, due=day(3))
    with conn:
        for days_ago, p in ((10, 10), (5, 30), (0, 50)):
            conn.execute("INSERT INTO pm_progress_updates (task_id, kind, author, progress, created_at) "
                         "VALUES (?, 'reported', 'Kabir', ?, ?)", (tid, p, ago(days_ago)))
    [a] = [a for a in pm_monitor.find(conn) if a["kind"] == "pace"]
    assert "from 10% to 50% in 10 days" in a["text"] and "needs about 13 more days" in a["text"]
    assert a["severity"] == "high"
    fast = new(conn, pid, "Fast", due=day(10))
    with conn:
        for days_ago, p in ((4, 20), (0, 80)):
            conn.execute("INSERT INTO pm_progress_updates (task_id, kind, author, progress, created_at) "
                         "VALUES (?, 'reported', 'Kabir', ?, ?)", (fast, p, ago(days_ago)))
    assert ("pace", fast) not in kinds(conn)                    # on track: nothing to say


def test_behind_overdue_dependency_unassigned(conn, pid):
    behind = new(conn, pid, "Behind", due=day(2))
    late = new(conn, pid, "Late", due=day(-1))
    first = new(conn, pid, "First", due=day(9))
    second = new(conn, pid, "Second", due=day(5), depends_on=[first])
    nobody = new(conn, pid, "Nobody", who=(), due=day(10))
    found = kinds(conn)
    assert {("behind", behind), ("overdue", late), ("dependency", second), ("unassigned", nobody)} <= found
    pm.set_progress(conn, KABIR, behind, 60)
    assert ("behind", behind) not in kinds(conn)


def test_stale_blocker_and_silent_work(conn, pid):
    blocked = new(conn, pid, "Blocked", due=day(20))
    pm.report_blocker(conn, KABIR, blocked, "Waiting for the canteen data")
    quiet = new(conn, pid, "Quiet", due=day(20))
    pm.set_status(conn, KABIR, quiet, "in_progress")
    assert not {k for k, _ in kinds(conn)} & {"stale_blocker", "silent"}       # fresh: nothing yet
    backdate(conn, blocked, 4)
    backdate(conn, quiet, 8)
    assert {("stale_blocker", blocked), ("silent", quiet)} <= kinds(conn)
    pm.add_comment(conn, KABIR, quiet, "Still going, slower than planned")
    assert ("silent", quiet) not in kinds(conn)                               # any update clears it


def test_waiting_review_and_request(conn, pid):
    tid = new(conn, pid, due=day(20))
    pm.set_status(conn, KABIR, tid, "review")
    pm.request_deadline(conn, KABIR, tid, day(25), "More testing")
    with conn:
        conn.execute("UPDATE pm_history SET at = ? WHERE task_id = ?", (ago(3), tid))
        conn.execute("UPDATE pm_requests SET created_at = ?", (ago(3),))
    found = {k for k, _ in kinds(conn)}
    assert {"review_wait", "request_wait"} <= found


def test_overload_is_about_workload_not_people(conn, pid):
    for i in range(4):
        new(conn, pid, f"Due soon {i}", who=["Aman"], due=day(i + 1), weight=3)
    new(conn, pid, "Light", who=["Riya"], due=day(3))
    [a] = [a for a in pm_monitor.find(conn) if a["kind"] == "overload"]
    assert a["member"] == "Aman" and "4 open tasks due in the next 7 days (total size 12)" in a["text"]
    assert "Kabir has nothing due" in a["suggestion"]                          # the lightest week, alphabetically first
    assert pm_monitor.member_suggestion(a).startswith("If this week is too much")
    for kind in pm_monitor.KIND_LABEL:                         # members never get advice meant for the leader
        assert kind in pm_monitor.MEMBER_SUGGESTION, kind


def test_milestone_and_sources(conn, pid):
    m = pm.add_milestone(conn, LEADER, pid, "Beta", day(3))
    new(conn, pid, "A", milestone_id=m, due=day(3))
    new(conn, pid, "B", milestone_id=m, due=day(3))
    [a] = [a for a in pm_monitor.find(conn) if a["kind"] == "milestone"]
    assert "0% complete" in a["text"] and a["severity"] == "high"


def test_wording_never_judges_people(tmp_path):
    path = str(tmp_path / "demo.db")
    seed(path)
    c = db.connect(path)
    claims.refresh(c)
    found = pm_monitor.find(c)
    assert {a["kind"] for a in found} >= {"pace", "stale_blocker", "review_wait", "request_wait", "overdue", "sources"}
    for a in found:
        assert not guardrails.JUDGEMENT.search(a["text"] + " " + a["suggestion"]), a


# ---- lifecycle and safety --------------------------------------------------------------------------------------

def test_checks_never_change_tasks(tmp_path):
    path = str(tmp_path / "demo.db")
    seed(path)
    c = db.connect(path)
    claims.refresh(c)
    before = snapshot(c)
    pm_monitor.run_checks(c)
    pm_monitor.run_checks(c, at=NOW + timedelta(days=2))
    assert snapshot(c) == before


def test_lifecycle_new_resolved_reopened(conn, pid):
    tid = new(conn, pid, due=day(-1))
    assert pm_monitor.run_checks(conn)["new"] == 1
    assert pm_monitor.run_checks(conn)["new"] == 0                            # same situation: no repeat
    pm.update_task(conn, LEADER, tid, due_date=day(5))
    assert pm_monitor.run_checks(conn)["resolved"] == 1 and pm_monitor.alerts(conn) == []
    pm.update_task(conn, LEADER, tid, due_date=day(-2))
    assert pm_monitor.run_checks(conn)["new"] == 1                            # it came back


def test_dismiss_snooze_remind_are_leader_actions(conn, pid):
    tid = new(conn, pid, due=day(-1))
    pm_monitor.run_checks(conn)
    [a] = pm_monitor.alerts(conn)
    for attempt in (lambda: pm_monitor.dismiss(conn, KABIR, a["id"]), lambda: pm_monitor.snooze(conn, KABIR, a["id"]),
                    lambda: pm_monitor.remind(conn, KABIR, a["id"])):
        with pytest.raises(PermissionDenied):
            attempt()
    assert pm_monitor.remind(conn, LEADER, a["id"]) == 1
    assert any("asked for a quick update" in n["text"] for n in pm.notifications(conn, KABIR))
    pm_monitor.snooze(conn, LEADER, a["id"], 3)
    assert pm_monitor.alerts(conn) == [] and pm_monitor.alerts(conn, at=NOW + timedelta(days=4))
    pm_monitor.dismiss(conn, LEADER, a["id"], "Agreed with Kabir")
    pm_monitor.run_checks(conn)
    assert pm_monitor.alerts(conn, at=NOW + timedelta(days=4)) == []          # stays quiet while it lasts
    assert pm.task_detail(conn, tid)["due_date"] == day(-1)                    # and nothing changed the task


def test_notifications_once_a_day(conn, pid):
    blocked = new(conn, pid, "Blocked", due=day(20))
    pm.report_blocker(conn, KABIR, blocked, "No data")
    backdate(conn, blocked, 5)
    pm_monitor.run_checks(conn)
    pm_monitor.run_checks(conn)
    nudges = [n for n in pm.notifications(conn, KABIR) if n["text"].startswith("Daily check")]
    digests = [n for n in pm.notifications(conn, LEADER) if n["text"].startswith("Daily check")]
    assert len(nudges) == 1 and len(digests) == 1


def test_members_see_only_check_ins_about_their_own_work(conn, pid):
    mine = new(conn, pid, "Mine", due=day(-1))
    new(conn, pid, "Riya's", who=["Riya"], due=day(-1))
    pm_monitor.run_checks(conn)
    assert {a["task_id"] for a in pm_monitor.alerts(conn, member="Kabir")} == {mine}


# ---- pages ------------------------------------------------------------------------------------------------------

def test_dashboard_check_ins_and_member_view(team):  # noqa: F811
    claims.refresh(team)
    at = page("views/dashboard.py", auth.login(team, "neha", LEADER_PW)[0])
    tab = next(t for t in at.tabs if t.label.startswith("Check-ins ("))
    assert "blocked for 4 days" in " ".join(m.value for m in tab.markdown)
    before = len(pm_monitor.alerts(team))
    next(b for b in at.button if b.label == "Dismiss").click().run()
    assert not at.error and len(pm_monitor.alerts(team)) == before - 1

    member = page("views/workspace.py", auth.login(team, "kabir", MEMBER_PW)[0])
    body = text(member)
    assert "Check-ins about your work" in [s.value for s in member.subheader]
    assert "blocked for 4 days" in body
    assert "waiting 3 days for your review" not in body                        # Riya's check-in isn't Kabir's
    assert not any(b.label in ("Dismiss", "Remind", "Snooze 3 days") for b in member.button)
