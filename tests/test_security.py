"""Security-first upgrade: deny-by-default policy, resource rules, capability grants, re-authentication, and the
authorization coverage that proves every action is checked."""
import sys
import textwrap
from datetime import timedelta

import pytest

from teampulse import actions, auth, claims, db, pm, pm_ai, pm_monitor, pm_sync, security
from teampulse.auth import Actor, PermissionDenied
from teampulse.seed_demo import seed
from tests.test_roles_ui import LEADER_PW, MEMBER_PW, page, team, text  # noqa: F401  (shared fixture)


@pytest.fixture()
def demo(tmp_path):
    path = str(tmp_path / "s.db")
    seed(path)
    conn = db.connect(path)
    claims.refresh(conn)
    lid = auth._insert_user(conn, "Neha Sharma", "neha", "leader", "Neha", LEADER_PW, False, None)
    auth._insert_user(conn, "Kabir Rao", "kabir", "member", "Kabir", MEMBER_PW, False, lid)
    auth._insert_user(conn, "Riya", "riya", "member", "Riya", MEMBER_PW, False, lid)
    return conn


def signin(conn, who, confirm=False):
    actor = auth.login(conn, who, LEADER_PW if who == "neha" else MEMBER_PW)[1]
    if confirm:
        auth.confirm_password(conn, actor, LEADER_PW if who == "neha" else MEMBER_PW)
    return actor


def uid(conn, username):
    return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]


def tid(conn, title):
    return conn.execute("SELECT id FROM pm_tasks WHERE title LIKE ?", (title + "%",)).fetchone()[0]


# ---- coverage: every action reaches a check -------------------------------------------------------------

def test_every_action_reaches_a_permission_check():
    c = security.authorization_coverage()
    assert c["unchecked"] == [] and c["percent"] == 100 and c["mcp_tools_guarded"]
    assert c["functions"] >= 60


def test_coverage_catches_a_new_unchecked_action(tmp_path, monkeypatch):
    (tmp_path / "rogue_actions.py").write_text(textwrap.dedent('''
        def delete_everything(conn, actor):
            conn.execute("DELETE FROM pm_tasks")
        def safe_delete(conn, actor):
            from teampulse.auth import require
            require(conn, actor, "pm.manage")
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(security, "MODULES", [*security.MODULES, "rogue_actions"])
    c = security.authorization_coverage()
    assert [r["function"] for r in c["unchecked"]] == ["delete_everything"]
    sys.modules.pop("rogue_actions", None)


def test_signed_out_callers_are_refused_everywhere(demo):
    """Every guarded write, called with no signed-in person, is refused and changes nothing."""
    task, draft = tid(demo, "Food recommendations"), pm_ai.drafts(demo, 1)[0]["id"]
    pm_monitor.run_checks(demo)
    alert = pm_monitor.alerts(demo)[0]["id"]
    calls = {
        "create_project": lambda: pm.create_project(demo, None, "X"),
        "create_task": lambda: pm.create_task(demo, None, 1, "X"),
        "update_task": lambda: pm.update_task(demo, None, task, title="X"),
        "set_status": lambda: pm.set_status(demo, None, task, "done"),
        "set_progress": lambda: pm.set_progress(demo, None, task, 99),
        "verify_progress": lambda: pm.verify_progress(demo, None, task, 99),
        "add_comment": lambda: pm.add_comment(demo, None, task, "x"),
        "request_deadline": lambda: pm.request_deadline(demo, None, task, "2030-01-01", "x"),
        "decide_request": lambda: pm.decide_request(demo, None, 1, True),
        "archive_task": lambda: pm.archive_task(demo, None, task),
        "link": lambda: pm_sync.link(demo, None, task, "task:campuseats/app#18"),
        "unlink": lambda: pm_sync.unlink(demo, None, task),
        "import_tasks": lambda: pm_sync.import_tasks(demo, None, 1, ["task:notion:doc-report"]),
        "propose_plan": lambda: pm_ai.propose_plan(demo, None, 1, "- x y"),
        "accept_plan": lambda: pm_ai.accept_plan(demo, None, draft, []),
        "discard": lambda: pm_ai.discard(demo, None, draft),
        "draft_update": lambda: pm_ai.draft_update(demo, None, task),
        "weekly_summary": lambda: pm_ai.weekly_summary(demo, None, 1),
        "dismiss": lambda: pm_monitor.dismiss(demo, None, alert),
        "remind": lambda: pm_monitor.remind(demo, None, alert),
        "set_grant": lambda: auth.set_grant(demo, None, uid(demo, "kabir"), "pm.manage", True),
        "create_user": lambda: actions.create_user(demo, None, "X Y"),
        "reset_credentials": lambda: actions.reset_credentials(demo, None, uid(demo, "kabir")),
        "set_active": lambda: actions.set_active(demo, None, uid(demo, "kabir"), False),
        "remember": lambda: actions.remember(demo, None, "remember x"),
    }
    before = [tuple(r) for r in demo.execute("SELECT * FROM pm_tasks ORDER BY id")]
    for name, call in calls.items():
        with pytest.raises(PermissionDenied):
            call()
        assert [tuple(r) for r in demo.execute("SELECT * FROM pm_tasks ORDER BY id")] == before, name
    assert demo.execute("SELECT COUNT(*) FROM audit_log WHERE outcome = 'denied' AND actor_id IS NULL"
                        ).fetchone()[0] >= len(calls) - 2    # a few are refused by the resource rule after the lookup


# ---- deny by default and resource rules -------------------------------------------------------------------

def test_deny_by_default(demo):
    neha = signin(demo, "neha")
    assert not auth.can(neha, "made.up.permission")              # even a leader: unknown means no
    assert not auth.can(None, "view")
    assert not auth.can(Actor("x", "x", "X", "member", None, must_change=True), "view")
    assert not auth.authorize(demo, neha, "made.up", object())


def test_resource_rules_are_one_decision_for_every_caller(demo):
    kabir, riya, neha = signin(demo, "kabir"), signin(demo, "riya"), signin(demo, "neha")
    recs = pm.task_detail(demo, tid(demo, "Food recommendations"))
    assert auth.authorize(demo, kabir, "pm.work", recs) and not auth.authorize(demo, riya, "pm.work", recs)
    assert auth.authorize(demo, neha, "pm.work", recs)
    assert auth.authorize(demo, kabir, "contrib.review_own", "Kabir")
    assert not auth.authorize(demo, kabir, "contrib.review_own", "Riya")
    assert auth.authorize(demo, kabir, "workspace.read", "kabir") and not auth.authorize(demo, kabir, "workspace.read", "Riya")
    assert auth.authorize(demo, neha, "workspace.read", "Riya")
    assert not auth.authorize(demo, kabir, "workspace.read")      # a derived permission needs its resource
    assert auth.authorize(demo, kabir, "token.revoke", kabir.id) and not auth.authorize(demo, kabir, "token.revoke", riya.id)


# ---- capability grants --------------------------------------------------------------------------------------

def test_grant_co_lead_takes_effect_on_the_next_request_and_is_removable(demo):
    neha = signin(demo, "neha", confirm=True)
    kabir_session = auth.login(demo, "kabir", MEMBER_PW)[0]
    assert not pm.can_manage(auth.resolve(demo, kabir_session))
    auth.set_grant(demo, neha, uid(demo, "kabir"), "pm.manage", True)
    kabir = auth.resolve(demo, kabir_session)                      # the same session, next click
    assert pm.can_manage(kabir) and auth.page_allowed(demo, kabir, "dashboard")
    assert not auth.page_allowed(demo, kabir, "team") and not auth.can(kabir, "users.manage")
    pm.create_project(demo, kabir, "Kabir's side project")         # co-lead can plan
    with pytest.raises(PermissionDenied):
        actions.create_user(demo, kabir, "Sneaky Person")           # but never administer accounts
    auth.set_grant(demo, neha, uid(demo, "kabir"), "pm.manage", False)
    assert not pm.can_manage(auth.resolve(demo, kabir_session))   # removed: gone on the very next request


def test_grant_rules(demo):
    neha = signin(demo, "neha")
    with pytest.raises(PermissionDenied, match="confirm your password"):
        auth.set_grant(demo, neha, uid(demo, "kabir"), "audit.read", True)     # needs recent re-authentication
    auth.confirm_password(demo, neha, LEADER_PW)
    for bad in ("users.manage", "settings.manage", "made.up"):
        with pytest.raises(PermissionDenied, match="can't be granted"):
            auth.set_grant(demo, neha, uid(demo, "kabir"), bad, True)
    with pytest.raises(PermissionDenied, match="your own"):
        auth.set_grant(demo, neha, neha.id, "audit.read", True)
    kabir = signin(demo, "kabir")
    with pytest.raises(PermissionDenied):
        auth.set_grant(demo, kabir, uid(demo, "riya"), "pm.manage", True)     # members can't grant
    assert demo.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'grant.set' AND outcome = 'denied'"
                        ).fetchone()[0] == 3


def test_a_tampered_grant_row_gives_nothing(demo):
    kabir_session = auth.login(demo, "kabir", MEMBER_PW)[0]
    with demo:
        demo.execute("INSERT INTO user_grants VALUES (?, 'users.manage', 'attacker', '2026-01-01')", (uid(demo, "kabir"),))
    kabir = auth.resolve(demo, kabir_session)
    assert not auth.can(kabir, "users.manage") and "users.manage" not in kabir.grants


# ---- re-authentication ---------------------------------------------------------------------------------------

def test_high_impact_actions_need_a_recent_password_in_this_session(demo):
    neha = signin(demo, "neha")
    kabir_id = uid(demo, "kabir")
    for call in (lambda: actions.reset_credentials(demo, neha, kabir_id),
                 lambda: actions.set_active(demo, neha, kabir_id, False),
                 lambda: actions.create_user(demo, neha, "New Leader", "leader")):
        with pytest.raises(PermissionDenied, match="confirm your password"):
            call()
    actions.create_user(demo, neha, "New Member")                   # ordinary changes don't need it
    with pytest.raises(auth.AuthError, match="isn't right"):
        auth.confirm_password(demo, neha, "wrong-password-123")
    auth.confirm_password(demo, neha, LEADER_PW)
    other_device = signin(demo, "neha")                              # a second session isn't unlocked by the first
    assert auth.recently_authenticated(demo, neha) and not auth.recently_authenticated(demo, other_device)
    actions.set_active(demo, neha, kabir_id, False)
    with demo:
        demo.execute("UPDATE sessions SET reauth_at = ? WHERE token_hash = ?",
                     (auth.iso(auth.now() - timedelta(minutes=auth.REAUTH_MINUTES + 1)), neha.session))
    with pytest.raises(PermissionDenied, match="confirm your password"):     # 10 minutes later: confirm again
        actions.reset_credentials(demo, neha, kabir_id)


def test_reauth_is_throttled_like_sign_in(demo):
    neha = signin(demo, "neha")
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError):
            auth.confirm_password(demo, neha, "nope-nope-nope")
    with pytest.raises(auth.AuthError, match="Try again"):
        auth.confirm_password(demo, neha, "nope-nope-nope")
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.confirm_password(demo, neha, LEADER_PW)                # locked, even with the right password


# ---- pages ---------------------------------------------------------------------------------------------------

def test_capabilities_tab_and_co_lead_pages(team):  # noqa: F811
    token = auth.login(team, "neha", LEADER_PW)[0]
    at = page("views/team.py", token)
    next(t for t in at.text_input if t.label == "Your password").input(LEADER_PW)
    next(b for b in at.button if b.label == "Confirm").click().run()
    kabir_id = team.execute("SELECT id FROM users WHERE username='kabir'").fetchone()[0]
    at.checkbox(key=f"grant-{kabir_id}-pm.manage").check().run()
    assert not at.error, [e.value for e in at.error]
    member_token = auth.login(team, "kabir", MEMBER_PW)[0]
    dash = page("views/dashboard.py", member_token)
    assert "Team dashboard" in text(dash) and "You don&#x27;t have access" not in text(dash)
    assert "You don&#x27;t have access" in text(page("views/team.py", member_token))


# ---- one team per install: two installs share nothing --------------------------------------------------------

def test_two_installs_share_nothing(tmp_path, monkeypatch):
    from teampulse.index import EventIndex
    monkeypatch.delenv("TEAMPULSE_CHROMA", raising=False)
    a_path, b_path = str(tmp_path / "a" / "team.db"), str(tmp_path / "b" / "team.db")
    a, b = db.connect(a_path), db.connect(b_path)
    for conn in (a, b):
        auth._insert_user(conn, "Neha", "neha", "leader", None, LEADER_PW, False, None)
    session_a, neha_a = auth.login(a, "neha", LEADER_PW)
    token_a = auth.create_api_token(a, neha_a, "assistant")
    assert auth.resolve(b, session_a) is None                   # a sign-in on one install means nothing on the other
    assert auth.resolve_api_token(b, token_a) is None
    pm.create_project(a, neha_a, "Team A secret project")
    assert pm.projects(b) == []
    from teampulse.index import HashEmbedder
    ia, ib = EventIndex(a, embedder=HashEmbedder()), EventIndex(b, embedder=HashEmbedder())
    assert ia.path != ib.path and ia.path.startswith(str(tmp_path / "a")) and ib.path.startswith(str(tmp_path / "b"))


# ---- tamper-evident activity log and encrypted backups --------------------------------------------------------

def test_activity_log_is_tamper_evident(demo):
    auth.audit(demo, None, "test.one")
    assert auth.verify_audit(demo)["ok"]
    first = demo.execute("SELECT MIN(id) FROM audit_log").fetchone()[0]
    with demo:
        demo.execute("UPDATE audit_log SET outcome = 'ok' WHERE id = ?", (first + 3,))     # quietly edit history
        demo.execute("UPDATE audit_log SET detail = 'nothing to see' WHERE id = ?", (first + 3,))
    report = auth.verify_audit(demo)
    assert not report["ok"] and report["first_bad_id"] == first + 3


def test_cutting_off_the_newest_entries_is_noticed_even_after_new_ones(demo):
    last = demo.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
    with demo:
        demo.execute("DELETE FROM audit_log WHERE id >= ?", (last - 2,))
    assert not auth.verify_audit(demo)["ok"]
    auth.audit(demo, None, "after.the.cut")
    assert not auth.verify_audit(demo)["ok"]


def test_retention_may_remove_the_oldest_entries(demo):
    first = demo.execute("SELECT MIN(id) FROM audit_log").fetchone()[0]
    with demo:
        demo.execute("DELETE FROM audit_log WHERE id < ?", (first + 5,))
    auth.audit(demo, None, "still.fine")
    assert auth.verify_audit(demo)["ok"]


def test_encrypted_backups(demo, tmp_path, monkeypatch):
    from teampulse import maintenance
    key = maintenance.new_key()
    monkeypatch.setenv("TEAMPULSE_BACKUP_KEY", key)
    path = demo.execute("PRAGMA database_list").fetchone()[2]
    copy = maintenance.backup(path, str(tmp_path / "backups"))
    raw = copy.read_bytes()
    assert copy.name.endswith(".db.enc") and b"SQLite format" not in raw and b"Kabir" not in raw
    assert not list((tmp_path / "backups").glob("*.db"))                    # no plaintext copy on disk
    assert maintenance.verify(str(copy))["users"] == 3
    with pytest.raises(ValueError, match="different key"):
        maintenance.verify(str(copy), key=maintenance.new_key())
    tampered = tmp_path / "tampered.db.enc"
    tampered.write_bytes(raw[:200] + bytes([raw[200] ^ 1]) + raw[201:])
    with pytest.raises(ValueError, match="changed or damaged"):
        maintenance.verify(str(tampered))
    restored = str(tmp_path / "restored.db")
    maintenance.restore(str(copy), restored)
    assert auth.verify_audit(db.connect(restored))["ok"]
    monkeypatch.delenv("TEAMPULSE_BACKUP_KEY")
    with pytest.raises(ValueError, match="Set TEAMPULSE_BACKUP_KEY"):
        maintenance.verify(str(copy))


# ---- secrets never leave for an AI provider -------------------------------------------------------------------

def test_secrets_are_removed_before_any_ai_request(demo, monkeypatch):
    from teampulse import llm as llm_mod
    sent = []

    class Fake:
        class chat:
            class completions:
                @staticmethod
                def create(**request):
                    sent.append(request["messages"])
                    from types import SimpleNamespace
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                                           usage=None)
    ai = llm_mod.LLM(demo, providers=["groq"])
    monkeypatch.setattr(ai, "_client", lambda name: (Fake, "model"))
    key = "gsk_" + "A" * 40
    ai.chat([{"role": "system", "content": "s"}, {"role": "user", "content": f"Kabir pasted {key} in chat"}])
    body = " ".join(m["content"] for m in sent[0])
    assert key not in body and "[secret removed]" in body
    cached = demo.execute("SELECT COUNT(*) FROM llm_cache WHERE response LIKE ?", (f"%{key}%",)).fetchone()[0]
    assert cached == 0 and ai.stats["redacted"] == 1
    assert demo.execute("SELECT detail FROM audit_log WHERE action = 'security.secret_redacted'").fetchone()[0] == "1"


# ---- the Security page shows only measured numbers ------------------------------------------------------------

def test_security_page_for_leaders_and_audit_readers_only(team):  # noqa: F811
    try:
        auth.login(team, "kabir", "wrong-password-xx")
    except auth.AuthError:
        pass
    leader_token = auth.login(team, "neha", LEADER_PW)[0]
    at = page("views/security.py", leader_token)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Actions checked"] == "100%" and metrics["Activity log"] == "Intact"
    assert metrics["Failed sign-ins"] == "1" and metrics["Secrets found in the log"] == "0"
    member_token = auth.login(team, "kabir", MEMBER_PW)[0]
    assert "You don&#x27;t have access" in text(page("views/security.py", member_token))
    neha = auth.login(team, "neha", LEADER_PW)[1]
    auth.confirm_password(team, neha, LEADER_PW)
    auth.set_grant(team, neha, team.execute("SELECT id FROM users WHERE username='kabir'").fetchone()[0],
                   "audit.read", True)
    assert "Activity log" in {m.label for m in page("views/security.py", member_token).metric}


def test_security_page_reports_tampering(team):  # noqa: F811
    with team:
        team.execute("UPDATE audit_log SET detail = 'edited' WHERE id = (SELECT MIN(id) + 1 FROM audit_log)")
    at = page("views/security.py", auth.login(team, "neha", LEADER_PW)[0])
    assert any("tampered with" in e.value for e in at.error)


def test_secret_scanner():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scripts"))
    import scan_secrets
    assert scan_secrets.findings_in("token = 'gsk_Xq9vLm2Pz7Rt4Wn8Ks1Bd6Hf3Jy5Cg0Ae'")
    assert scan_secrets.findings_in("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert not scan_secrets.findings_in("gsk_" + "A" * 40 + " and gsk_abcdefghijklmnopqrstuvwxyz123456")
    assert scan_secrets.scan_tree() == []                       # nothing real in the repository
