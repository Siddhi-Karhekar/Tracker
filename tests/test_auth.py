"""Server-side security tests: passwords, sessions, lockout, and that members are refused every admin action."""
import json
from datetime import timedelta

import pytest

from teampulse import actions, auth, db, memory
from teampulse.agents.retrieval import RetrievalAgent
from teampulse.agents.supervisor import SupervisorAgent
from teampulse.index import EventIndex, HashEmbedder
from teampulse.llm import LLMReply
from teampulse.seed_demo import seed

LEADER_PW, MEMBER_PW = "violet-river-stone-42", "amber-cloud-lantern-7"


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setenv("TEAMPULSE_AUTH", "on")


@pytest.fixture()
def team(tmp_path):
    path = str(tmp_path / "t.db")
    seed(path)
    conn = db.connect(path)
    lid = auth._insert_user(conn, "Neha Sharma", "neha", "leader", "Neha", LEADER_PW, False, None)
    mid = auth._insert_user(conn, "Kabir Rao", "kabir", "member", "Kabir", MEMBER_PW, False, lid)
    _, leader = auth.login(conn, "neha", LEADER_PW)
    auth.confirm_password(conn, leader, LEADER_PW)   # as the leader does before high-impact changes
    _, member = auth.login(conn, "kabir", MEMBER_PW)
    return conn, leader, member, tmp_path


def age(conn, sql, minutes):
    conn.execute(sql, ((auth.now() - timedelta(minutes=minutes)).isoformat(),))
    conn.commit()


# ---- passwords ---------------------------------------------------------------------------------------

def test_passwords_are_salted_hashes_never_plain(team):
    conn, *_ = team
    h1, h2 = auth.hash_password("same-password-here"), auth.hash_password("same-password-here")
    assert h1 != h2 and h1.startswith("scrypt$") and auth.verify_password("same-password-here", h1)
    assert not auth.verify_password("Same-password-here", h1) and not auth.verify_password("x", "garbage")
    dump = "\n".join(str(tuple(r)) for t in ("users", "sessions", "audit_log")
                     for r in conn.execute(f"SELECT * FROM {t}"))
    assert LEADER_PW not in dump and MEMBER_PW not in dump


def test_password_policy():
    assert auth.password_problem("short") and auth.password_problem("password123")
    assert auth.password_problem("aaaaaaaaaaaa")                        # too few distinct characters
    assert "name" in auth.password_problem("kabir-is-great-99", "kabir", "Kabir Rao")
    assert auth.password_problem("violet-river-stone-42") is None
    pw = auth.generate_password()
    assert len(pw) == 14 and auth.password_problem(pw) is None and not set(pw) & set("0O1lI")


# ---- sign-in, sessions, lockout ------------------------------------------------------------------------

def test_login_errors_do_not_reveal_which_usernames_exist(team):
    conn, *_ = team
    with pytest.raises(auth.AuthError) as wrong_pw:
        auth.login(conn, "kabir", "not-the-password")
    with pytest.raises(auth.AuthError) as no_user:
        auth.login(conn, "nobody-here", "not-the-password")
    assert str(wrong_pw.value) == str(no_user.value)


@pytest.mark.parametrize("username", ["kabir", "ghost-user"])
def test_lockout_after_five_failures_for_real_and_unknown_names(team, username):
    conn, *_ = team
    for _ in range(4):
        with pytest.raises(auth.AuthError, match="don't match"):
            auth.login(conn, username, "wrong-password-x")
    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.login(conn, username, "wrong-password-x")
    if username == "kabir":   # even the right password is refused while locked
        with pytest.raises(auth.AuthError, match="Too many"):
            auth.login(conn, "kabir", MEMBER_PW)
        conn.execute("UPDATE login_throttle SET locked_until = ? WHERE key = 'kabir'",
                     ((auth.now() - timedelta(minutes=1)).isoformat(),))
        conn.commit()
        assert auth.login(conn, "kabir", MEMBER_PW)[1].username == "kabir"


def test_sessions_logout_idle_and_expiry(team):
    conn, *_ = team
    token, _ = auth.login(conn, "kabir", MEMBER_PW)
    assert auth.resolve(conn, token).username == "kabir"
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (token,)).fetchone()[0] == 0  # only a hash is stored
    auth.logout(conn, token)
    assert auth.resolve(conn, token) is None                              # signed out = token dead
    t2, _ = auth.login(conn, "kabir", MEMBER_PW)
    age(conn, "UPDATE sessions SET last_seen = ? WHERE revoked_at IS NULL", 61)
    assert auth.resolve(conn, t2) is None                                 # idle more than 60 minutes
    t3, _ = auth.login(conn, "kabir", MEMBER_PW)
    age(conn, "UPDATE sessions SET expires_at = ? WHERE revoked_at IS NULL", 1)
    assert auth.resolve(conn, t3) is None                                 # past the 12-hour limit
    assert auth.resolve(conn, None) is None and auth.resolve(conn, "forged-token") is None


def test_deactivation_ends_sessions_and_blocks_sign_in(team):
    conn, leader, member, _ = team
    token, _ = auth.login(conn, "kabir", MEMBER_PW)
    actions.set_active(conn, leader, member.id, False)
    assert auth.resolve(conn, token) is None
    with pytest.raises(auth.AuthError, match="deactivated"):
        auth.login(conn, "kabir", MEMBER_PW)
    actions.set_active(conn, leader, member.id, True)
    assert auth.login(conn, "kabir", MEMBER_PW)


def test_one_time_password_must_be_changed_first(team):
    conn, leader, *_ = team
    username, one_time = actions.create_user(conn, leader, "Riya Mehta", "member", member_name="Riya")
    token, riya = auth.login(conn, username, one_time)
    assert riya.must_change and not auth.can(riya, "view") and not auth.page_allowed(conn, riya, "home")
    with pytest.raises(auth.AuthError, match="current password"):
        auth.change_password(conn, riya, token, "wrong", "new-strong-phrase-9")
    with pytest.raises(auth.AuthError, match="different"):
        auth.change_password(conn, riya, token, one_time, one_time)
    other, _ = auth.login(conn, username, one_time)                       # a second device
    auth.change_password(conn, riya, token, one_time, "new-strong-phrase-9")
    assert auth.resolve(conn, token) and not auth.resolve(conn, token).must_change
    assert auth.resolve(conn, other) is None                              # other devices signed out
    with pytest.raises(auth.AuthError):
        auth.login(conn, username, one_time)                              # one-time password is dead


def test_reset_credentials(team):
    conn, leader, member, _ = team
    token, _ = auth.login(conn, "kabir", MEMBER_PW)
    _, new_pw = actions.reset_credentials(conn, leader, member.id)
    assert auth.resolve(conn, token) is None
    with pytest.raises(auth.AuthError):
        auth.login(conn, "kabir", MEMBER_PW)
    assert auth.login(conn, "kabir", new_pw)[1].must_change
    log = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM audit_log"))
    assert new_pw not in log


def test_first_leader_only_once(tmp_path):
    conn = db.connect(str(tmp_path / "x.db"))
    auth.create_first_leader(conn, "First Leader")
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_first_leader(conn, "Second Person")


# ---- role-based access: members are refused everything except viewing, asking, and their own review ----

def every_admin_action(conn, actor, member, index):
    """Every guarded write, called the way a page would call it."""
    disc_id = conn.execute("SELECT id FROM discrepancies LIMIT 1").fetchone()
    msg = conn.execute("SELECT id FROM events WHERE source='discord' LIMIT 1").fetchone()[0]
    mem = conn.execute("SELECT id FROM memory LIMIT 1").fetchone()[0]
    return {
        "create_user": lambda: actions.create_user(conn, actor, "New Person"),
        "reset_credentials": lambda: actions.reset_credentials(conn, actor, member.id),
        "deactivate": lambda: actions.set_active(conn, actor, member.id, False),
        "member_pages": lambda: actions.set_member_pages(conn, actor, ["home"]),
        "refresh_all": lambda: actions.refresh_all(conn, actor, index, None),
        "rebuild_tasks": lambda: actions.rebuild_tasks(conn, actor, index, None),
        "scan_chat": lambda: actions.scan_chat(conn, actor, None, index),
        "set_verdict": lambda: actions.set_verdict(conn, actor, disc_id[0] if disc_id else "x", "real"),
        "correct_link": lambda: actions.correct_link(conn, actor, msg, None, index),
        "add_label": lambda: actions.add_label(conn, actor, msg, "blocker"),
        "undo_label": lambda: actions.undo_label(conn, actor, msg),
        "remember": lambda: actions.remember(conn, actor, "the demo is on 3 Dec"),
        "forget": lambda: actions.forget_memory(conn, actor, mem),
        "run_evaluation": lambda: actions.run_evaluation(conn, actor, "keyword", None),
        "check_connections": lambda: actions.check_connections(conn, actor),
        "test_ai": lambda: actions.test_ai(conn, actor, None),
    }


def test_member_is_refused_every_admin_action_and_each_is_logged(team):
    conn, leader, member, tmp = team
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp / "chroma"))
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("users", "labels", "memory", "settings")}
    calls = every_admin_action(conn, member, member, index)
    for name, call in calls.items():
        with pytest.raises(auth.PermissionDenied):
            call()
    after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
    assert after == before                                                # nothing changed
    denied = conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_id = ? AND outcome = 'denied'", (member.id,)).fetchone()[0]
    assert denied == len(calls)
    assert auth.resolve(conn, auth.login(conn, "kabir", MEMBER_PW)[0]).role == "member"   # still a member


def test_signed_out_and_must_change_users_are_refused_too(team):
    conn, leader, member, _ = team
    with pytest.raises(auth.PermissionDenied):
        actions.create_user(conn, None, "Sneaky")
    pending = auth.Actor(leader.id, "neha", "Neha", "leader", None, must_change=True)
    with pytest.raises(auth.PermissionDenied):
        actions.create_user(conn, pending, "Sneaky")


def test_leader_can_do_everything(team):
    conn, leader, member, tmp = team
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp / "chroma"))
    from teampulse import discrepancies
    discrepancies.run(conn, index, None)
    for name, call in every_admin_action(conn, leader, member, index).items():
        if name in ("check_connections", "test_ai", "scan_chat", "deactivate"):
            continue   # need network / a classifier / would end the member's access for later steps
        call()


def test_account_rules(team):
    conn, leader, member, _ = team
    with pytest.raises(auth.AuthError, match="own account"):
        actions.set_active(conn, leader, leader.id, False)
    other_id = auth._insert_user(conn, "Other Leader", "other", "leader", None, LEADER_PW, False, None)
    other = auth.login(conn, "other", LEADER_PW)[1]
    auth.confirm_password(conn, other, LEADER_PW)
    actions.set_active(conn, leader, other_id, False)          # two active leaders: allowed
    with pytest.raises(auth.PermissionDenied):                 # a deactivated leader's session is gone, and
        actions.set_active(conn, other, leader.id, False)      # with it the password confirmation
    with conn:                                                 # even with a stale confirmation, the team keeps a leader
        conn.execute("UPDATE sessions SET revoked_at = NULL, reauth_at = ? WHERE user_id = ?", (auth.iso(auth.now()), other_id))
    with pytest.raises(auth.AuthError, match="at least one active leader"):
        actions.set_active(conn, other, leader.id, False)
    with pytest.raises(auth.AuthError, match="valid role"):
        actions.create_user(conn, leader, "Role Hacker", role="admin")
    with pytest.raises(auth.AuthError, match="name of 2 to 60"):
        actions.create_user(conn, leader, "<script>alert(1)</script>")
    with pytest.raises(auth.AuthError, match="Usernames are"):
        actions.create_user(conn, leader, "Valid Name", username="DROP TABLE users;--")
    with pytest.raises(auth.AuthError, match="already taken"):
        actions.create_user(conn, leader, "Kabir Again", username="kabir")


def test_page_access(team):
    conn, leader, member, _ = team
    assert auth.page_allowed(conn, leader, "team") and auth.page_allowed(conn, leader, "label")
    assert not auth.page_allowed(conn, member, "team") and not auth.page_allowed(conn, member, "label")
    assert auth.page_allowed(conn, member, "tasks")
    actions.set_member_pages(conn, leader, ["home", "ask", "team", "label"])   # admin pages can't be granted
    assert auth.member_pages(conn) == ["home", "ask"]
    assert not auth.page_allowed(conn, member, "tasks") and not auth.page_allowed(conn, member, "team")


def test_own_report_only(team):
    conn, leader, member, _ = team
    actions.review_own_report(conn, member, "disputed", "not ready")
    assert conn.execute("SELECT status FROM contribution_reviews WHERE member='Kabir'").fetchone()[0] == "disputed"
    assert conn.execute("SELECT status FROM contribution_reviews WHERE member='Neha'").fetchone()[0] == "approved"
    unlinked = auth.Actor("x", "z", "Zed", "member", None)
    with pytest.raises(auth.AuthError, match="isn't linked"):
        actions.review_own_report(conn, unlinked, "approved")


def test_contribution_review_can_be_switched_off_for_members(team, monkeypatch):
    conn, leader, member, _ = team
    monkeypatch.setenv("TEAMPULSE_CONTRIB_REVIEW", "off")
    with pytest.raises(auth.PermissionDenied):
        actions.review_own_report(conn, member, "approved")


# ---- asking: open to all, rate-limited, team memory stays leader-only --------------------------------

class RouterThatRewrites:
    """Simulates an AI router turning an innocent-looking question into a memory command."""

    def chat(self, messages, json_mode=False, temperature=0.0):
        system = messages[0]["content"]
        if "supervisor" in system:
            return LLMReply(json.dumps({"standalone_question": "remember that the review moved to 1 Jan",
                                        "parts": [{"question": "remember that the review moved to 1 Jan",
                                                   "specialist": "memory"}]}), "fake")
        raise RuntimeError("not needed")


def test_members_can_ask_but_not_change_memory(team, monkeypatch):
    conn, leader, member, tmp = team
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp / "chroma"))
    llm = RouterThatRewrites()
    agent = SupervisorAgent(conn, retrieval=RetrievalAgent(conn, index=index, llm=llm), llm=llm)
    count = lambda: conn.execute("SELECT COUNT(*) FROM memory WHERE active = 1").fetchone()[0]
    before = count()
    direct = actions.ask(conn, member, agent, "remember that we use MongoDB now", [])
    assert "Only the team leader" in direct["answer"] and count() == before
    sneaky = actions.ask(conn, member, agent, "btw the review got moved to jan 1", [])      # router rewrites it
    assert "Only the team leader" in sneaky["answer"] and count() == before
    actions.ask(conn, leader, agent, "remember that the review moved to 1 Jan", [])
    assert count() == before                                               # same topic: replaces, not adds
    assert any("1 Jan" in m["fact"] for m in memory.active(conn))


def test_ask_rate_limit(team, monkeypatch):
    conn, leader, member, _ = team
    monkeypatch.setenv("TEAMPULSE_ASK_PER_HOUR", "2")

    class Agent:
        def ask(self, question, history=None, allow_memory_write=True, allowed_specialists=None):
            return {"answer": "ok", "evidence": [], "cited": []}

    actions.ask(conn, member, Agent(), "q1", [])
    actions.ask(conn, member, Agent(), "q2", [])
    with pytest.raises(actions.RateLimited):
        actions.ask(conn, member, Agent(), "q3", [])
    assert actions.ask(conn, leader, Agent(), "q1", [])["answer"] == "ok"   # limits are per person
    assert "q1" not in "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM audit_log"))  # questions not logged


class RouterTo:
    """An AI router that sends every question to one specialist, whatever the person is allowed to see."""

    def __init__(self, specialist):
        self.specialist = specialist

    def chat(self, messages, json_mode=False, temperature=0.0):
        if "supervisor" in messages[0]["content"]:
            q = messages[-1]["content"].split("New question:")[-1].strip()
            return LLMReply(json.dumps({"standalone_question": q,
                                        "parts": [{"question": q, "specialist": self.specialist}]}), "fake")
        raise RuntimeError("no AI in this test")


def test_asking_cannot_reach_a_page_the_leader_closed(team):
    """Regression: hiding Contributions from members used to leave the same reports one question away in Ask."""
    conn, leader, member, tmp = team
    index = EventIndex(conn, embedder=HashEmbedder(), path=str(tmp / "chroma"))
    llm = RouterTo("contributions")
    agent = SupervisorAgent(conn, retrieval=RetrievalAgent(conn, index=index, llm=llm), llm=llm)
    open_answer = actions.ask(conn, member, agent, "what did Kabir do this week?", [])
    assert "hasn't opened" not in open_answer["answer"] and open_answer["evidence"]      # page open: answered

    actions.set_member_pages(conn, leader, ["home", "ask", "blockers"])                 # leader closes it
    closed = actions.ask(conn, member, agent, "what did Kabir do this week?", [])
    assert "hasn't opened" in closed["answer"] and not closed["evidence"]
    assert not closed.get("memories")                                                   # memory page closed too
    assert "contributions" in actions.allowed_specialists(conn, leader)                  # leaders keep everything
    led = actions.ask(conn, leader, agent, "what did Kabir do this week?", [])
    assert "hasn't opened" not in led["answer"]


def test_public_demo_visitors_cannot_change_accounts_or_settings_for_everyone(team, monkeypatch):
    """Regression: in the public demo every visitor is the same leader, so one could lock everyone else out."""
    conn, leader, member, _ = team
    monkeypatch.setenv("TEAMPULSE_PUBLIC_DEMO", "on")
    kabir = conn.execute("SELECT id FROM users WHERE username = 'kabir'").fetchone()[0]
    before = conn.execute("SELECT COUNT(*), SUM(active) FROM users").fetchone()[:]
    for call in (lambda: actions.create_user(conn, leader, "Visitor", "leader"),
                 lambda: actions.set_active(conn, leader, kabir, False),
                 lambda: actions.reset_credentials(conn, leader, kabir),
                 lambda: actions.set_member_pages(conn, leader, []),
                 lambda: actions.check_connections(conn, leader),
                 lambda: actions.run_evaluation(conn, leader, "llm", None)):
        with pytest.raises(auth.PermissionDenied, match="public demo"):
            call()
    assert conn.execute("SELECT COUNT(*), SUM(active) FROM users").fetchone()[:] == before
    assert auth.member_pages(conn) == auth.DEFAULT_MEMBER_PAGES
    assert auth.can(leader, "data.edit") and auth.can(leader, "memory.write")   # the demo stays explorable
    monkeypatch.setenv("TEAMPULSE_PUBLIC_DEMO", "off")
    assert auth.can(leader, "users.manage")                                      # normal copies unaffected


def test_setup_code_is_throttled_and_must_be_long(team, monkeypatch):
    conn, *_ = team
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    with pytest.raises(auth.AuthError, match="at least 16"):
        auth.check_setup_code(conn, "short", "short")                 # weak configured code refused outright
    code = "a-long-random-setup-code-123"
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError, match="isn't right"):
            auth.check_setup_code(conn, "guess", code)
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.check_setup_code(conn, "guess", code)
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.check_setup_code(conn, code, code)                       # locked even with the right code
    assert conn.execute("SELECT COUNT(*) FROM login_throttle WHERE key = 'neha'").fetchone()[0] == 0


def test_wrong_current_password_is_throttled(team, monkeypatch):
    conn, leader, member, _ = team
    monkeypatch.setattr(auth.time, "sleep", lambda s: None)
    token = auth.login(conn, member.username, MEMBER_PW)[0]
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError, match="isn't right"):
            auth.change_password(conn, member, token, "guess-guess-1", "a-brand-new-password-9")
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.change_password(conn, member, token, "guess-guess-1", "a-brand-new-password-9")
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.change_password(conn, member, token, MEMBER_PW, "a-brand-new-password-9")


def test_a_crash_while_answering_is_reported_safely_and_logged_as_failed(team):
    conn, leader, member, _ = team

    class Broken:
        def ask(self, *a, **k):
            raise RuntimeError("chroma: /srv/data/secret_path corrupted")

    with pytest.raises(actions.ServiceError) as err:
        actions.ask(conn, member, Broken(), "what's blocked?", [])
    assert "secret_path" not in str(err.value) and "try again" in str(err.value)
    row = conn.execute("SELECT outcome, detail FROM audit_log WHERE action='ask' ORDER BY id DESC").fetchone()
    assert tuple(row) == ("failed", "RuntimeError")
    with pytest.raises(auth.AuthError, match="Type a question"):
        actions.ask(conn, member, Broken(), "   ", [])
