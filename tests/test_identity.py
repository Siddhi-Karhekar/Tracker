"""Advanced identity: two-factor sign-in (TOTP), sessions you can see and end, a session moving browsers, and
contribution approvals that lapse when the evidence behind them changes."""
import time
from datetime import timedelta

import pytest

from teampulse import actions, auth, claims, contributions, db, mfa
from teampulse.seed_demo import seed
from tests.test_roles_ui import LEADER_PW, MEMBER_PW, page, team, text  # noqa: F401  (shared fixture)


@pytest.fixture()
def conn(tmp_path):
    path = str(tmp_path / "i.db")
    seed(path)
    c = db.connect(path)
    claims.refresh(c)
    lid = auth._insert_user(c, "Neha Sharma", "neha", "leader", "Neha", LEADER_PW, False, None)
    auth._insert_user(c, "Kabir Rao", "kabir", "member", "Kabir", MEMBER_PW, False, lid)
    return c


def enroll(conn, username, password):
    actor = auth.login(conn, username, password)[1]
    secret = mfa.new_secret()
    codes = mfa.enable(conn, actor, secret, mfa.code_at(secret, time.time() - 30), password)
    return actor, secret, codes


def next_code(secret, steps_ahead=1):
    return mfa.code_at(secret, time.time() + 30 * steps_ahead)


# ---- TOTP itself ---------------------------------------------------------------------------------------------

def test_rfc6238_test_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"         # "12345678901234567890", RFC 6238 appendix B
    assert mfa.code_at(secret, 59) == "287082" and mfa.code_at(secret, 1111111109) == "081804"


# ---- turning it on --------------------------------------------------------------------------------------------

def test_enable_needs_the_password_and_a_working_code(conn):
    kabir = auth.login(conn, "kabir", MEMBER_PW)[1]
    secret = mfa.new_secret()
    with pytest.raises(auth.AuthError, match="password"):
        mfa.enable(conn, kabir, secret, mfa.code_at(secret), "wrong-password-1")
    with pytest.raises(auth.AuthError, match="doesn't match"):
        mfa.enable(conn, kabir, secret, "000000", MEMBER_PW)
    codes = mfa.enable(conn, kabir, secret, mfa.code_at(secret), MEMBER_PW)
    stored = conn.execute("SELECT mfa_recovery FROM users WHERE username = 'kabir'").fetchone()[0]
    assert len(codes) == 8 and not any(c in stored or c.replace("-", "") in stored for c in codes)   # hashes only


# ---- signing in ---------------------------------------------------------------------------------------------------

def test_two_step_sign_in_with_no_session_before_the_code(conn):
    _, secret, codes = enroll(conn, "kabir", MEMBER_PW)
    sessions_before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    with pytest.raises(auth.MFARequired) as need:
        auth.login(conn, "kabir", MEMBER_PW)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions_before   # nothing issued yet
    with pytest.raises(auth.AuthError, match="doesn't match"):
        mfa.complete_login(conn, need.value.pending, "123456")
    code = next_code(secret)
    token, actor = mfa.complete_login(conn, need.value.pending, code)
    assert auth.resolve(conn, token).username == "kabir"
    with pytest.raises(auth.MFARequired) as again:
        auth.login(conn, "kabir", MEMBER_PW)
    with pytest.raises(auth.AuthError):                                        # the same code can't be replayed
        mfa.complete_login(conn, again.value.pending, code)
    mfa.complete_login(conn, again.value.pending, codes[0])                    # a recovery code works once
    with pytest.raises(auth.MFARequired) as third:
        auth.login(conn, "kabir", MEMBER_PW)
    with pytest.raises(auth.AuthError):
        mfa.complete_login(conn, third.value.pending, codes[0])
    assert mfa.recovery_left(conn, actor.id) == 7


def test_pending_sign_in_expires_and_wrong_codes_lock(conn):
    _, secret, _ = enroll(conn, "kabir", MEMBER_PW)
    with pytest.raises(auth.MFARequired) as need:
        auth.login(conn, "kabir", MEMBER_PW)
    with conn:
        conn.execute("UPDATE mfa_pending SET expires_at = '2000-01-01T00:00:00+00:00'")
    with pytest.raises(auth.AuthError, match="took too long"):
        mfa.complete_login(conn, need.value.pending, next_code(secret))
    with pytest.raises(auth.MFARequired) as need:
        auth.login(conn, "kabir", MEMBER_PW)
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError):
            mfa.complete_login(conn, need.value.pending, "000000")
    with pytest.raises(auth.AuthError, match="Too many"):
        mfa.complete_login(conn, need.value.pending, "000000")
    with pytest.raises(auth.AuthError, match="Too many|took too long"):        # even the right code now
        mfa.complete_login(conn, need.value.pending, next_code(secret, 2))


def test_required_for_leaders_until_set_up(conn, monkeypatch):
    monkeypatch.setenv("TEAMPULSE_MFA", "leaders")
    token, neha = auth.login(conn, "neha", LEADER_PW)
    assert neha.needs_mfa and not auth.can(neha, "view") and not auth.page_allowed(conn, neha, "home")
    kabir = auth.login(conn, "kabir", MEMBER_PW)[1]
    assert not kabir.needs_mfa and auth.can(kabir, "view")                     # members aren't forced
    secret = mfa.new_secret()
    mfa.enable(conn, neha, secret, mfa.code_at(secret), LEADER_PW)
    assert auth.can(auth.resolve(conn, token), "users.manage")                 # set up: full access again
    with pytest.raises(auth.AuthError, match="required"):
        mfa.disable(conn, auth.resolve(conn, token), LEADER_PW, next_code(secret))


def test_step_up_needs_the_code_too_and_reset_clears_it(conn):
    _, secret, _ = enroll(conn, "neha", LEADER_PW)
    with pytest.raises(auth.MFARequired) as need:
        auth.login(conn, "neha", LEADER_PW)
    _, neha = mfa.complete_login(conn, need.value.pending, mfa.code_at(secret))
    with pytest.raises(auth.AuthError):
        auth.confirm_password(conn, neha, LEADER_PW)                           # password alone isn't enough
    auth.confirm_password(conn, neha, LEADER_PW, next_code(secret))
    kabir_id = conn.execute("SELECT id FROM users WHERE username = 'kabir'").fetchone()[0]
    kabir_secret = mfa.new_secret()
    mfa.enable(conn, auth.login(conn, "kabir", MEMBER_PW)[1], kabir_secret, mfa.code_at(kabir_secret), MEMBER_PW)
    actions.reset_credentials(conn, neha, kabir_id)                            # lost phone: the leader resets
    assert not mfa.enabled(conn, kabir_id)


# ---- sessions ------------------------------------------------------------------------------------------------------

def test_see_and_end_your_own_sessions_only(conn):
    laptop, kabir = auth.login(conn, "kabir", MEMBER_PW, "Firefox on Windows")
    phone = auth.login(conn, "kabir", MEMBER_PW, "Chrome on Android")[0]
    neha_session = auth.login(conn, "neha", LEADER_PW)[0]
    mine = auth.list_sessions(conn, kabir)
    assert {s["user_agent"] for s in mine} == {"Firefox on Windows", "Chrome on Android"}
    assert [s["current"] for s in mine if s["user_agent"] == "Firefox on Windows"] == [True]
    other = next(s["id"] for s in mine if not s["current"])
    auth.revoke_session(conn, kabir, other)
    assert auth.resolve(conn, phone) is None and auth.resolve(conn, laptop) is not None
    neha_row = conn.execute("SELECT rowid FROM sessions WHERE token_hash = ?", (auth._token_hash(neha_session),)).fetchone()[0]
    auth.revoke_session(conn, kabir, neha_row)                                 # someone else's: no effect, logged
    assert auth.resolve(conn, neha_session) is not None
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'auth.revoke_session' AND outcome = 'denied'"
                        ).fetchone()[0] == 1


def test_a_session_moving_to_another_browser_is_ended(conn):
    token, _ = auth.login(conn, "kabir", MEMBER_PW, "Firefox on Windows")
    assert auth.resolve(conn, token, "Firefox on Windows") is not None
    assert auth.resolve(conn, token, "curl/8.0") is None                       # ended, fail closed
    assert auth.resolve(conn, token, "Firefox on Windows") is None             # and stays ended
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'auth.session_moved'").fetchone()[0] == 1


# ---- contribution approvals cover the evidence that was approved -------------------------------------------------

def test_approval_lapses_when_approved_evidence_changes(conn):
    kabir = auth.login(conn, "kabir", MEMBER_PW)[1]
    actions.review_own_report(conn, kabir, "approved", "")
    member = next(m for m in contributions.members(conn) if m.name == "Kabir")
    assert contributions.visible_to_team(conn, member)
    snapshot = contributions.get_review(conn, "Kabir")["evidence_hash"]
    some_event = next(iter(__import__("json").loads(snapshot)["items"]))
    with conn:   # someone edits a piece of evidence Kabir approved
        conn.execute("UPDATE events SET updated_at = '2030-01-01T00:00:00+00:00' WHERE id = ?", (some_event,))
    assert not contributions.visible_to_team(conn, member)                     # hidden at once
    assert contributions.refresh_integrity(conn) == ["Kabir"]
    assert contributions.get_review(conn, "Kabir")["status"] == "changed"
    assert any("changed after you approved" in n["text"] for n in auth_notifications(conn, "Kabir"))
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'contrib.approval_lapsed'").fetchone()[0] == 1
    actions.review_own_report(conn, kabir, "approved", "Checked again")         # Kabir looks again
    assert contributions.visible_to_team(conn, member)


def test_new_work_does_not_lapse_an_approval(conn):
    kabir = auth.login(conn, "kabir", MEMBER_PW)[1]
    actions.review_own_report(conn, kabir, "approved", "")
    from teampulse.models import Event
    db.upsert_event(conn, Event(source="github", kind="commit", native_id="commit:new123",
                                author="kabir-ml", author_name="kabir-ml", title="Add model training script",
                                created_at=auth.iso(auth.now()), updated_at=auth.iso(auth.now())))
    assert contributions.refresh_integrity(conn) == []
    member = next(m for m in contributions.members(conn) if m.name == "Kabir")
    assert contributions.visible_to_team(conn, member)


def auth_notifications(conn, member):
    return [dict(r) for r in conn.execute("SELECT * FROM pm_notifications WHERE recipient = ?", (member,))]


# ---- pages -----------------------------------------------------------------------------------------------------------

def test_sign_in_page_asks_for_the_code(team):  # noqa: F811
    secret = mfa.new_secret()
    mfa.enable(team, auth.login(team, "kabir", MEMBER_PW)[1], secret, mfa.code_at(secret, time.time() - 30), MEMBER_PW)
    at = page("app.py")
    at.text_input[0].input("kabir")
    at.text_input[1].input(MEMBER_PW)
    next(b for b in at.button if b.label == "Sign in").click().run()
    assert any("Two-factor sign-in" in m.value for m in at.markdown)
    next(t for t in at.text_input if t.label == "Code").input(mfa.code_at(secret, time.time() + 30))
    next(b for b in at.button if b.label == "Continue").click().run()
    assert not at.exception and "Sign in to TeamPulse" not in text(at)


def test_account_page_sets_up_two_factor(team):  # noqa: F811
    token = auth.login(team, "kabir", MEMBER_PW)[0]
    at = page("views/account.py", token)
    secret = at.session_state["mfa_secret"]
    next(t for t in at.text_input if t.label == "Code from your app").input(mfa.code_at(secret))
    next(t for t in at.text_input if t.label == "Password").input(MEMBER_PW)
    next(b for b in at.button if b.label == "Turn on two-factor sign-in").click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    assert mfa.enabled(team, team.execute("SELECT id FROM users WHERE username='kabir'").fetchone()[0])
    assert len(" ".join(c.value for c in at.code).split()) >= 8                  # recovery codes shown once
