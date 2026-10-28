"""Both roles through the real pages: sign-in, what each role sees, and that hidden pages stay closed."""
import os

import pytest
from streamlit.testing.v1 import AppTest

from teampulse import auth, db, memory
from teampulse.seed_demo import seed

ROOT = os.path.join(os.path.dirname(__file__), "..")
LEADER_PW, MEMBER_PW = "violet-river-stone-42", "amber-cloud-lantern-7"
HERO = "<div class='tp-hero"                      # the Home headline element (the class also appears in the CSS)
NO_ACCESS = "You don&#x27;t have access to this page."


@pytest.fixture()
def team(tmp_path, monkeypatch):
    path = str(tmp_path / "demo.db")
    seed(path)
    conn = db.connect(path)
    lid = auth._insert_user(conn, "Neha Sharma", "neha", "leader", "Neha", LEADER_PW, False, None)
    auth._insert_user(conn, "Kabir Rao", "kabir", "member", "Kabir", MEMBER_PW, False, lid)
    for k, v in {"TEAMPULSE_DB": path, "TEAMPULSE_EMBEDDER": "hash", "LLM_PROVIDERS": "off",
                 "TEAMPULSE_AUTH": "on", "TEAMPULSE_PUBLIC_DEMO": "off"}.items():
        monkeypatch.setenv(k, v)
    import teampulse.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", path)
    return conn


def page(file: str, token: str | None = None) -> AppTest:
    at = AppTest.from_file(os.path.join(ROOT, *file.split("/")), default_timeout=90)
    if token:
        at.session_state["tp_session"] = token
    at.run()
    assert not at.exception, at.exception
    return at


def text(at) -> str:
    return " ".join(m.value for m in at.markdown)


def labels(at) -> set[str]:
    return {b.label for b in at.button} | {b.label for b in at.sidebar.button}


def sign_in_via_form(at, username, password):
    at.text_input[0].input(username)
    at.text_input[1].input(password)
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert not at.exception, at.exception
    return at


# ---- signing in and out -------------------------------------------------------------------------------

def test_signed_out_sees_only_the_sign_in_page(team):
    at = page("app.py")
    assert "Sign in to TeamPulse" in text(at) and HERO not in text(at)
    for view in ("views/home.py", "views/team.py", "views/tasks.py"):     # opening a page file directly
        assert "Please sign in." in text(page(view))


def test_wrong_password_shows_an_error(team):
    at = sign_in_via_form(page("app.py"), "kabir", "not-my-password")
    assert any("don't match" in e.value for e in at.error)
    assert "Sign in to TeamPulse" in text(at)


def test_member_signs_in_sees_role_and_signs_out(team):
    at = sign_in_via_form(page("app.py"), "kabir", MEMBER_PW)
    token = at.session_state["tp_session"]
    assert HERO in text(at)                                            # Home
    sidebar = " ".join(m.value for m in at.sidebar.markdown)
    assert "Kabir Rao" in sidebar and "Team member" in sidebar
    assert "Refresh now" not in labels(at)                                  # no admin controls on Home
    next(b for b in at.sidebar.button if b.label == "Sign out").click().run()
    assert "Sign in to TeamPulse" in text(at) and auth.resolve(team, token) is None


def test_one_time_password_leads_to_choose_password(team):
    leader = auth.login(team, "neha", LEADER_PW)[1]
    from teampulse import actions
    username, one_time = actions.create_user(team, leader, "Riya Mehta", "member", member_name="Riya")
    at = sign_in_via_form(page("app.py"), username, one_time)
    assert "Choose your own password" in text(at) and HERO not in text(at)
    at.text_input[0].input(one_time)
    at.text_input[1].input("fresh-garden-teapot-3")
    at.text_input[2].input("fresh-garden-teapot-3")
    next(b for b in at.button if b.label == "Save password").click().run()
    assert not at.exception and HERO in text(at)                      # now in the app


def test_deactivated_mid_session_is_signed_out_on_next_click(team):
    token, member = auth.login(team, "kabir", MEMBER_PW)
    at = page("app.py", token)
    assert HERO in text(at)
    leader = auth.login(team, "neha", LEADER_PW)[1]
    from teampulse import actions
    actions.set_active(team, leader, member.id, False)
    at.run()
    assert "Sign in to TeamPulse" in text(at) and any("session ended" in i.value for i in at.info)


# ---- what a member sees ----------------------------------------------------------------------------------

def test_member_cannot_open_admin_pages(team):
    token = auth.login(team, "kabir", MEMBER_PW)[0]
    for view in ("views/team.py", "views/label.py"):
        at = page(view, token)
        assert NO_ACCESS in text(at)
    denied = team.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'page.open' AND outcome = 'denied'").fetchone()[0]
    assert denied == 2


def test_member_pages_have_no_editing_controls(team):
    token = auth.login(team, "kabir", MEMBER_PW)[0]
    checks = {
        "views/discrepancies.py": {"Real", "False alarm", "Compare sources now"},
        "views/blockers.py": {"Read new messages", "Read everything again"},
        "views/tasks.py": {"Rebuild tasks", "Save"},
        "views/memory.py": {"Remember", "Forget"},
        "views/sources.py": {"Run check", "Send a test message"},
        "app.py": {"Refresh now"},                                           # Home, inside the full app
    }
    for view, forbidden in checks.items():
        at = page(view, token)
        assert not (labels(at) & forbidden), (view, labels(at) & forbidden)
    assert len(page("views/tasks.py", token).tabs) == 1                    # no "Fix chat links" tab
    assert not page("views/sources.py", token).dataframe                     # no member/account list


def test_member_sees_only_their_own_report_and_can_review_it(team):
    token = auth.login(team, "kabir", MEMBER_PW)[0]
    at = page("views/contributions.py", token)
    mine = " ".join(m.value for m in at.tabs[0].markdown)
    assert "Kabir" in mine and "### Aman" not in mine and not at.selectbox  # no picking someone else
    next(b for b in at.button if b.label.startswith("Keep hidden")).click().run()
    assert team.execute("SELECT status FROM contribution_reviews WHERE member='Kabir'").fetchone()[0] == "disputed"
    assert team.execute("SELECT status FROM contribution_reviews WHERE member='Neha'").fetchone()[0] == "approved"


def test_member_asking_to_change_memory_is_refused(team):
    token = auth.login(team, "kabir", MEMBER_PW)[0]
    before = len(memory.active(team))
    at = page("views/ask.py", token)
    at.chat_input[0].set_value("remember that we switched to MongoDB").run()
    assert "Only the team leader can change team memory" in text(at) and len(memory.active(team)) == before


def test_leader_hides_a_page_from_members(team):
    leader = auth.login(team, "neha", LEADER_PW)[1]
    from teampulse import actions
    actions.set_member_pages(team, leader, ["home", "ask"])
    token = auth.login(team, "kabir", MEMBER_PW)[0]
    assert NO_ACCESS in text(page("views/tasks.py", token))


# ---- what the leader sees ------------------------------------------------------------------------------

def test_leader_creates_account_and_sees_password_once(team):
    token = auth.login(team, "neha", LEADER_PW)[0]
    at = page("views/team.py", token)
    at.text_input[0].input("Riya Mehta")
    next(b for b in at.button if b.label == "Create account").click().run()
    assert not at.exception
    shown = " ".join(c.value for c in at.code)
    assert "Username: riya.mehta" in shown and "One-time password:" in shown
    one_time = shown.split("One-time password:")[1].strip()
    stored = team.execute("SELECT password_hash, must_change FROM users WHERE username='riya.mehta'").fetchone()
    assert one_time not in stored[0] and stored[1] == 1 and auth.verify_password(one_time, stored[0])
    next(b for b in at.button if b.label == "I've shared it, hide this").click().run()
    assert not at.code                                                       # gone, and never shown again


def test_leader_resets_and_deactivates_from_the_page(team):
    token = auth.login(team, "neha", LEADER_PW)[0]
    kabir_id = team.execute("SELECT id FROM users WHERE username='kabir'").fetchone()[0]
    at = page("views/team.py", token)
    at.button(key=f"reset-{kabir_id}").click().run()
    assert "One-time password:" in " ".join(c.value for c in at.code)
    at.button(key=f"active-{kabir_id}").click().run()
    assert team.execute("SELECT active FROM users WHERE id = ?", (kabir_id,)).fetchone()[0] == 0
    labels_now = labels(at)
    assert "Reactivate" in labels_now


def test_leader_pages_show_editing_controls(team):
    token = auth.login(team, "neha", LEADER_PW)[0]
    assert {"Real", "False alarm", "Compare sources now"} <= labels(page("views/discrepancies.py", token))
    assert "Rebuild tasks" in labels(page("views/tasks.py", token))
    assert len(page("views/tasks.py", token).tabs) == 2
    assert "Run check" in labels(page("views/sources.py", token))


def test_public_demo_team_page_is_look_only(team, monkeypatch):
    from datetime import datetime, timezone
    db.set_state(team, "demo:seeded_at", datetime.now(timezone.utc).isoformat())   # don't let the demo re-seed
    monkeypatch.setenv("TEAMPULSE_PUBLIC_DEMO", "on")
    token = auth.login(team, "neha", LEADER_PW)[0]
    at = page("views/team.py", token)
    assert "switched off in the public demo" in " ".join(i.value for i in at.info)
    assert all(b.disabled for b in at.button if b.label in ("Create account", "Reset password", "Deactivate", "Save"))
    assert any(b.label == "Reset password" for b in at.button)
