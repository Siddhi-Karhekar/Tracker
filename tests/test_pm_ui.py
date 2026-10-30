"""TeamPulse 2.0 pages through the real Streamlit app: each person's workspace, the board, the leader dashboard."""
import re

import pytest

from teampulse import auth, pm
from tests.test_roles_ui import LEADER_PW, MEMBER_PW, labels, page, team, text  # noqa: F401  (shared fixture)

PAYLOADS = ["<img src=x onerror=alert(1)>", "<script>alert(1)</script>"]


def login(conn, who):
    return auth.login(conn, who, LEADER_PW if who == "neha" else MEMBER_PW)[0]


def task_id(conn, title):
    return conn.execute("SELECT id FROM pm_tasks WHERE title = ?", (title,)).fetchone()[0]


def test_member_sees_their_own_workspace_only(team):  # noqa: F811
    at = page("views/workspace.py", login(team, "kabir"))
    body = text(at)
    assert "My work" in body and "Food recommendations" in body and "Payment integration" in body
    assert "Order history screen" not in body                       # Riya's task
    assert not at.sidebar.selectbox                                  # no picking someone else's workspace
    assert any("Notifications" in e.label for e in at.expander)


def test_leader_can_open_every_members_workspace(team):  # noqa: F811
    at = page("views/workspace.py", login(team, "neha"))
    picker = at.sidebar.selectbox[0]
    assert set(picker.options) == {"Aman", "Kabir", "Neha", "Riya"}
    picker.set_value("Riya").run()
    assert "Riya&#x27;s work" in text(at) and "Order history screen" in text(at)
    assert not any("Notifications" in e.label for e in at.expander)  # Riya's inbox stays hers


def test_member_updates_own_task_from_the_panel(team):  # noqa: F811
    tid = task_id(team, "Food recommendations")
    at = page("views/workspace.py", login(team, "kabir"))
    next(b for b in at.button if b.label == f"Open #{tid}").click().run()
    assert not at.exception
    assert "Update your progress" in text(at)
    assert "Completed" not in at.selectbox(key=f"status-{tid}").options     # members send for review instead
    at.selectbox(key=f"status-{tid}").set_value("in_progress")
    at.slider(key=f"progress-{tid}").set_value(45)
    next(b for b in at.button if b.label == "Save update").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    row = team.execute("SELECT status, reported_progress FROM pm_tasks WHERE id = ?", (tid,)).fetchone()
    assert tuple(row) == ("in_progress", 45)


def test_member_cannot_change_someone_elses_task_but_can_comment(team):  # noqa: F811
    tid = task_id(team, "Order history screen")
    at = page("views/board.py", login(team, "kabir"))
    next(b for b in at.button if b.label == f"Open #{tid}").click().run()
    assert any("isn't assigned to you" in c.value for c in at.caption)
    assert "Save update" not in labels(at) and "Save changes" not in labels(at)
    at.text_area(key=f"comment-body-{tid}").input("Looks good @Riya")
    next(b for b in at.button if b.label == "Comment").click().run()
    assert not at.exception and not at.error
    assert team.execute("SELECT COUNT(*) FROM pm_notifications WHERE recipient='Riya' AND text LIKE '%commented%'"
                        ).fetchone()[0] >= 1


def test_member_has_no_planning_controls_and_no_dashboard(team):  # noqa: F811
    token = login(team, "kabir")
    at = page("views/board.py", token)
    assert not any(e.label in ("New project", "New task", "New milestone") for e in at.expander)
    assert "You don&#x27;t have access to this page." in text(page("views/dashboard.py", token))


def test_leader_plans_on_the_board(team):  # noqa: F811
    at = page("views/board.py", login(team, "neha"))
    labels_ = {e.label for e in at.expander}
    assert {"New project", "New task", "New milestone"} <= labels_
    form_title = next(t for t in at.text_input if t.label == "Title")
    form_title.input("Write test plan")
    next(b for b in at.button if b.label == "Create task").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    assert team.execute("SELECT COUNT(*) FROM pm_tasks WHERE title = 'Write test plan'").fetchone()[0] == 1


def test_leader_dashboard_reviews_and_decides(team):  # noqa: F811
    at = page("views/dashboard.py", login(team, "neha"))
    body = text(at)
    assert "Order history screen" in body and "Food recommendations" in body     # in review / at risk
    assert "not a ranking" in " ".join(c.value for c in at.caption)
    next(b for b in at.button if b.label == "Approve").click().run()                # Kabir's deadline request
    assert not at.exception and not at.error
    assert team.execute("SELECT status FROM pm_requests").fetchone()[0] == "approved"


def test_leader_approves_finished_work(team):  # noqa: F811
    tid = task_id(team, "Order history screen")
    # Riya's list view is done; finish filters so the task can be completed.
    riya = auth.Actor("r", "riya", "Riya", "member", "Riya")
    sub = team.execute("SELECT id FROM pm_tasks WHERE parent_id = ? AND status != 'done'", (tid,)).fetchone()[0]
    pm.set_status(team, riya, sub, "done")
    at = page("views/dashboard.py", login(team, "neha"))
    next(b for b in at.button if b.label == f"Open #{tid}").click().run()
    next(b for b in at.button if b.label == "Approve and mark Completed").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    assert team.execute("SELECT status FROM pm_tasks WHERE id = ?", (tid,)).fetchone()[0] == "done"


@pytest.mark.parametrize("view", ["views/workspace.py", "views/board.py", "views/dashboard.py"])
def test_hostile_task_text_is_never_html(team, view):  # noqa: F811
    poison = " ".join(PAYLOADS)
    team.execute("UPDATE pm_tasks SET title = title || ?, description = ?, blocked_reason = ?", (poison, poison, poison))
    team.execute("UPDATE pm_comments SET body = ?", (poison,))
    team.execute("UPDATE pm_notifications SET text = ?", (poison,))
    team.commit()
    at = page(view, login(team, "neha"))
    tid = team.execute("SELECT id FROM pm_tasks WHERE title LIKE 'Food recommendations%'").fetchone()[0]
    if any(b.label == f"Open #{tid}" for b in at.button):
        next(b for b in at.button if b.label == f"Open #{tid}").click().run()
    live = " ".join(m.value for m in at.markdown if getattr(m, "allow_html", False))
    for p in PAYLOADS:
        assert p not in live
    assert not re.search(r"<script", live, re.I)
