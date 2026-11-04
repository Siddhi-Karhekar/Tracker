"""The design rules from the SOLID review, kept true by tests:

  - pages only lay things out: no SQL in views/ or app.py (queries live in teampulse/queries.py);
  - check-ins and conflict rules are registered functions, so a new rule is added without editing the others;
  - auth.py is split by responsibility, but every old import still works, and the base modules depend on nothing.
"""
import ast
import re
from pathlib import Path

from teampulse import auth, db, discrepancies, pm_monitor, queries
from teampulse.seed_demo import seed

ROOT = Path(__file__).resolve().parents[1]
SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b.+\b(FROM|INTO|SET|WHERE)\b", re.I)


def test_pages_contain_no_sql():
    for page in [*sorted((ROOT / "views").glob("*.py")), ROOT / "app.py"]:
        tree = ast.parse(page.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("execute", "executemany", "executescript"):
                raise AssertionError(f"{page.name}:{node.lineno} runs SQL; add a function to teampulse/queries.py")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQL.search(node.value):
                raise AssertionError(f"{page.name}:{node.lineno} contains SQL: {node.value[:60]}")


def test_queries_answer_on_the_demo_team(tmp_path):
    path = str(tmp_path / "demo.db")
    seed(path)
    conn = db.connect(path)
    assert queries.has_events(conn) and not queries.tasks_refreshed_at(conn)
    assert queries.discord_message_count(conn) > 0
    assert queries.member_names(conn) == sorted(queries.member_names(conn))
    assert all(r["type"] in ("blocker", "help_request") for r in queries.open_blockers(conn, days=3650))
    assert sum(r["n"] for r in queries.daily_activity(conn, days=3650)) > 0
    count, _ = queries.source_stats(conn, "github")
    assert count > 0


def test_activity_log_filters(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    auth.audit(conn, None, "ask", "q")
    auth.audit(conn, None, "auth.login", "neha", "failed")
    auth.audit(conn, None, "task.update", "1", "denied")
    assert [r["outcome"] for r in queries.activity_log(conn, "denied")] == ["denied"]
    assert "ask" not in [r["action"] for r in queries.activity_log(conn, skip_questions=True)]
    assert len(queries.activity_log(conn, limit=2)) == 2
    assert set(queries.activity_log(conn)[0]) == {"at", "actor_name", "action", "target", "outcome", "detail"}


def test_a_new_check_in_is_added_without_editing_the_others(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "c.db"))
    assert len(pm_monitor.TASK_CHECKS) == 6 and len(pm_monitor.TEAM_CHECKS) == 4

    def always(ctx):
        yield pm_monitor.alert("custom", "low", "A new kind of check.", "Do something.", member="x", key="custom:x")

    monkeypatch.setattr(pm_monitor, "TEAM_CHECKS", [*pm_monitor.TEAM_CHECKS, always])
    assert [a["kind"] for a in pm_monitor.find(conn)] == ["custom"]


def test_a_new_conflict_rule_is_added_without_editing_the_others(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "r.db"))
    card = {"id": "t1", "number": 12, "title": "Login", "status": {}, "owner": {}, "deadline": {}}
    assert discrepancies.detect_task(conn, card) == []          # the three built-in rules find nothing to compare
    assert [r.__name__ for r in discrepancies.TASK_RULES] == ["status_rule", "owner_rule", "deadline_rule"]

    def custom(f):
        yield f.found("custom", "low", f"{f.name}: custom", "Fix it.", [])

    monkeypatch.setattr(discrepancies, "TASK_RULES", [custom])
    assert [d["type"] for d in discrepancies.detect_task(conn, card)] == ["custom"]


def test_auth_split_keeps_every_name_and_base_modules_depend_on_nothing():
    for name in ("Actor", "AuthError", "audit", "verify_audit", "hash_password", "create_api_token",
                 "resolve_api_token", "TOKEN_SCOPES", "set_grant", "list_grants", "login", "authorize"):
        assert getattr(auth, name), name
    for module in ("identity", "passwords"):
        tree = ast.parse((ROOT / "teampulse" / f"{module}.py").read_text(encoding="utf-8"))
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
        assert not [m for m in imports if m.startswith("teampulse")], f"{module}.py must not import teampulse modules"
