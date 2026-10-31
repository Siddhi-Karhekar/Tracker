"""Phase 3: planned tasks linked to GitHub issues and Notion pages. The plan stays the source of truth; the sources
bring evidence and disagreements, and change nothing by themselves."""
import pytest

from teampulse import auth, claims, db, pm, pm_sync
from teampulse.auth import Actor, PermissionDenied
from teampulse.seed_demo import seed
from tests.test_roles_ui import LEADER_PW, MEMBER_PW, page, team, text  # noqa: F401  (shared fixture)

LEADER = Actor("u-neha", "neha", "Neha Sharma", "leader", "Neha")
KABIR = Actor("u-kabir", "kabir", "Kabir Rao", "member", "Kabir")
RIYA = Actor("u-riya", "riya", "Riya", "member", "Riya")


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(str(tmp_path / "demo.db"))
    seed(str(tmp_path / "demo.db"))
    claims.refresh(c)                  # build the GitHub/Notion task registry and what each source says
    return c


def tid(conn, title):
    return conn.execute("SELECT id FROM pm_tasks WHERE title = ?", (title,)).fetchone()[0]


def snapshot(conn, task_id):
    return dict(conn.execute("SELECT status, due_date, reported_progress, verified_progress FROM pm_tasks "
                             "WHERE id = ?", (task_id,)).fetchone())


def test_linked_task_shows_evidence_and_disagreements(conn):
    payment = tid(conn, "Payment integration (Razorpay test mode)")
    before = snapshot(conn, payment)
    v = pm_sync.view(conn, pm.task_detail(conn, payment))
    assert v["source"]["url"] == "https://github.com/campuseats/app/issues/16"
    assert v["counts"]["commit"] == 2 and v["workers"] == {"Aman": 2}
    texts = " ".join(d["text"] for d in v["differences"])
    assert "GitHub has it assigned to Kabir" in texts and "GitHub's deadline is" in texts
    assert snapshot(conn, payment) == before                     # looking changes nothing


def test_status_disagreement_both_ways(conn):
    menu = tid(conn, "Menu API")                                  # completed here; Notion still says in progress
    texts = [d["text"] for d in pm_sync.view(conn, pm.task_detail(conn, menu))["differences"]]
    assert any("Completed here, but Notion still says “in progress”" in x for x in texts)
    pm.set_status(conn, LEADER, menu, "in_progress")              # reopen: GitHub's merged PR says done
    texts = [d["text"] for d in pm_sync.view(conn, pm.task_detail(conn, menu))["differences"]]
    assert any(x.startswith("GitHub says “done”, but here it's In progress") for x in texts)


def test_who_can_link(conn):
    report = tid(conn, "Project report draft")                    # Neha's task, not linked in the demo
    with pytest.raises(PermissionDenied):
        pm_sync.link(conn, KABIR, report, "task:notion:doc-report")
    assert [s["id"] for s in pm_sync.suggestions(conn, pm.task_detail(conn, report))] == ["task:notion:doc-report"]
    pm_sync.link(conn, LEADER, report, "task:notion:doc-report")
    recs = tid(conn, "Food recommendations")                      # Kabir may relink his own task
    pm_sync.unlink(conn, KABIR, recs)
    pm_sync.link(conn, KABIR, recs, "task:campuseats/app#18")
    with pytest.raises(pm.PMError, match="already linked"):
        pm_sync.link(conn, LEADER, report, "task:campuseats/app#18")
    with pytest.raises(pm.PMError, match="isn't in TeamPulse"):
        pm_sync.link(conn, LEADER, report, "task:nope#1")
    hist = [h["field"] for h in pm.task_detail(conn, recs)["history"]]
    assert hist.count("linked_to") == 2


def test_copying_a_deadline_is_a_leaders_explicit_edit(conn):
    payment = tid(conn, "Payment integration (Razorpay test mode)")
    with pytest.raises(PermissionDenied):
        pm_sync.use_source_deadline(conn, KABIR, payment, "github")
    github_due = pm_sync.view(conn, pm.task_detail(conn, payment))["card"]["deadline"]["github"]["value"][:10]
    pm_sync.use_source_deadline(conn, LEADER, payment, "github")
    assert snapshot(conn, payment)["due_date"] == github_due
    assert any("Deadline for" in n["text"] for n in pm.notifications(conn, KABIR))


def test_import_unplanned_source_tasks(conn):
    pid = pm.projects(conn)[0]["id"]
    unplanned = [s["id"] for s in pm_sync.overview(conn, pid)["unplanned"]]
    assert unplanned == ["task:notion:doc-report"]
    with pytest.raises(PermissionDenied):
        pm_sync.import_tasks(conn, KABIR, pid, unplanned)
    [new] = pm_sync.import_tasks(conn, LEADER, pid, unplanned)
    t = pm.task_detail(conn, new)
    assert t["title"] == "Project Report draft" and t["assignees"] == ["Neha"] and t["due_date"]
    assert t["status"] == "in_progress"                           # Notion says in progress
    assert pm_sync.overview(conn, pid)["unplanned"] == []
    assert pm_sync.import_tasks(conn, LEADER, pid, unplanned) == []   # importing twice does nothing


def test_missing_source_is_reported_not_crashed(conn):
    with conn:
        conn.execute("DELETE FROM tasks WHERE id = 'task:campuseats/app#22'")
    over = pm_sync.overview(conn)
    assert any(d["kind"] == "missing" for item in over["disagree"] for d in item["differences"])


# ---- pages -----------------------------------------------------------------------------------------------

def test_task_panel_and_dashboard_tab(team):  # noqa: F811
    claims.refresh(team)
    payment = tid(team, "Payment integration (Razorpay test mode)")
    token = auth.login(team, "neha", LEADER_PW)[0]
    at = page("views/board.py", token)
    next(b for b in at.button if b.label == f"Open #{payment}").click().run()
    assert any(e.label.startswith("GitHub & Notion (") for e in at.expander)
    assert "GitHub has it assigned to Kabir" in text(at)
    use = next(b for b in at.button if b.label.startswith("Use GitHub's deadline"))
    use.click().run()
    assert not at.error, [e.value for e in at.error]

    dash = page("views/dashboard.py", token)
    assert any(t.label.startswith("GitHub & Notion (") for t in dash.tabs)
    dash.multiselect(key="import-pick").set_value(["task:notion:doc-report"]).run()
    next(b for b in dash.button if b.label == "Add to project").click().run()
    assert not dash.error, [e.value for e in dash.error]
    assert team.execute("SELECT COUNT(*) FROM pm_source_links WHERE source_task = 'task:notion:doc-report'"
                        ).fetchone()[0] == 1


def test_member_sees_evidence_but_no_leader_controls(team):  # noqa: F811
    claims.refresh(team)
    payment = tid(team, "Payment integration (Razorpay test mode)")
    at = page("views/workspace.py", auth.login(team, "kabir", MEMBER_PW)[0])
    next(b for b in at.button if b.label == f"Open #{payment}").click().run()
    assert "GitHub has it assigned to Kabir" in text(at)
    assert not any(b.label.startswith("Use GitHub") for b in at.button)


def test_hostile_source_titles_are_text(team):  # noqa: F811
    claims.refresh(team)
    team.execute("UPDATE tasks SET title = title || '<img src=x onerror=alert(1)>', url = 'javascript:alert(1)'")
    team.execute("UPDATE events SET title = '<script>alert(1)</script>', url = 'javascript:alert(1)'")
    team.commit()
    payment = tid(team, "Payment integration (Razorpay test mode)")
    at = page("views/board.py", auth.login(team, "neha", LEADER_PW)[0])
    next(b for b in at.button if b.label == f"Open #{payment}").click().run()
    live = " ".join(m.value for m in at.markdown if getattr(m, "allow_html", False))
    assert "<img src=x" not in live and "<script>" not in live and "javascript:" not in live
