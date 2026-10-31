"""Security measurements: computed from the code and the database, never typed in.

authorization_coverage() reads the source of every module that acts for a signed-in person and checks that each
public function taking an `actor` reaches a permission check: directly (require, authorize, ...), through another
checked function, or because it only ever touches the caller's own data (listed with the reason). A new function that
does none of these is reported as unchecked, and the test suite fails on it.

metrics() collects the numbers for the Security page from the activity log and the database.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from datetime import timedelta

from teampulse.auth import iso, now

MODULES = ["teampulse.actions", "teampulse.pm", "teampulse.pm_sync", "teampulse.pm_ai", "teampulse.pm_monitor",
           "teampulse.auth", "teampulse.mcp_tools"]
CHECKS = {"require", "authorize", "_require_work", "require_recent_auth", "guard"}
# The policy itself (these *are* the checks) and read-only helpers that answer "may they?".
PRIMITIVES = {"can", "authorize", "require", "page_allowed", "require_recent_auth", "recently_authenticated", "audit",
              "is_assignee", "can_manage", "can_work_on", "allowed_specialists", "guard"}
# Functions that can only ever act on the caller's own data: the scoping is the check.
SELF_SCOPED = {
    "notifications": "reads only the caller's own inbox (and the leaders' inbox if they manage)",
    "mark_read": "marks only the caller's own inbox",
    "create_api_token": "creates a token for the caller only, never more than their own role",
    "list_api_tokens": "lists the caller's own tokens; everyone's only with users.manage",
    "change_password": "the caller's own password, after checking the current one",
    "confirm_password": "the caller's own session, after checking their password",
}
# MCP tools run only through mcp_server.call(), which runs mcp_tools.guard() first (checked separately below).
MCP_GUARDED = "mcp_tools"


def _functions():
    for name in MODULES:
        mod = importlib.import_module(name)
        tree = ast.parse(inspect.getsource(mod))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_") and \
                    "actor" in [a.arg for a in node.args.args]:
                calls = {c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None)
                         for c in ast.walk(node) if isinstance(c, ast.Call)}
                yield name.split(".")[-1], node.name, {c for c in calls if c}


def _mcp_tools_all_go_through_call() -> bool:
    """Every tool registered in mcp_server.build() must call `call(...)`, which runs guard() and the scope check."""
    from teampulse import mcp_server
    tree = ast.parse(inspect.getsource(mcp_server.build))
    tools = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.decorator_list]
    return bool(tools) and all(any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "call"
                                   for c in ast.walk(t)) for t in tools)


def authorization_coverage() -> dict:
    funcs = list(_functions())
    checked = {name for _, name, calls in funcs if calls & CHECKS}
    changed = True
    while changed:   # a function that calls a checked function is checked too
        changed = False
        for _, name, calls in funcs:
            if name not in checked and calls & checked:
                checked.add(name)
                changed = True
    mcp_ok = _mcp_tools_all_go_through_call()
    rows = []
    for module, name, _ in funcs:
        if name in PRIMITIVES:
            how = "policy"
        elif name in checked:
            how = "checked"
        elif name in SELF_SCOPED:
            how = "self-scoped"
        elif module == MCP_GUARDED and mcp_ok:
            how = "mcp guard"
        else:
            how = "UNCHECKED"
        rows.append({"module": module, "function": name, "how": how})
    actions = [r for r in rows if r["how"] != "policy"]
    unchecked = [r for r in actions if r["how"] == "UNCHECKED"]
    return {"functions": len(actions), "covered": len(actions) - len(unchecked),
            "percent": round(100 * (len(actions) - len(unchecked)) / len(actions)) if actions else 100,
            "unchecked": unchecked, "rows": rows, "mcp_tools_guarded": mcp_ok}


# ---- measurements for the Security page ------------------------------------------------------------------------

PRIVILEGED = ("account.create", "account.reset", "account.reset_cli", "account.activate", "account.deactivate",
              "grant.add", "grant.remove", "settings.member_pages", "token.create", "token.revoke",
              "account.create_first_leader")


def _count(conn, sql: str, params=()) -> int:
    return conn.execute(sql, params).fetchone()[0] or 0


def metrics(conn, days: int = 30) -> dict:
    """Every number here is counted from the activity log, the database or the code at the moment it's asked."""
    import json

    from teampulse import auth, config
    from teampulse.guardrails import SECRET

    since = iso(now() - timedelta(days=days))
    stamp = iso(now())
    m: dict = {"days": days}
    m["denied"] = _count(conn, "SELECT COUNT(*) FROM audit_log WHERE outcome = 'denied' AND at >= ?", (since,))
    m["denied_by_action"] = [dict(r) for r in conn.execute(
        "SELECT action, COUNT(*) AS n FROM audit_log WHERE outcome = 'denied' AND at >= ? GROUP BY action "
        "ORDER BY n DESC LIMIT 8", (since,))]
    m["failed_signins"] = _count(conn, "SELECT COUNT(*) FROM audit_log WHERE action = 'auth.login' AND outcome = 'failed' "
                                       "AND at >= ?", (since,))
    m["failed_reauth"] = _count(conn, "SELECT COUNT(*) FROM audit_log WHERE action = 'auth.reauth' AND outcome = 'failed' "
                                      "AND at >= ?", (since,))
    m["locked_now"] = _count(conn, "SELECT COUNT(*) FROM login_throttle WHERE locked_until > ?", (stamp,))
    marks = ",".join("?" * len(PRIVILEGED))
    m["privileged_changes"] = [dict(r) for r in conn.execute(
        f"SELECT at, actor_name, action, target, detail FROM audit_log WHERE action IN ({marks}) AND outcome = 'ok' "
        "AND at >= ? ORDER BY id DESC LIMIT 20", (*PRIVILEGED, since))]
    m["privileged_count"] = _count(conn, f"SELECT COUNT(*) FROM audit_log WHERE action IN ({marks}) AND outcome = 'ok' "
                                         "AND at >= ?", (*PRIVILEGED, since))
    m["sessions_active"] = _count(conn, "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > ?",
                                  (stamp,))
    m["leaders"] = _count(conn, "SELECT COUNT(*) FROM users WHERE role = 'leader' AND active = 1")
    m["grants"] = [dict(r) for r in conn.execute(
        "SELECT u.display_name, g.permission, g.granted_at FROM user_grants g JOIN users u ON u.id = g.user_id "
        "WHERE u.active = 1 ORDER BY u.display_name") if r["permission"] in auth.GRANTABLE]
    soon = iso(now() + timedelta(days=14))
    m["tokens_active"] = _count(conn, "SELECT COUNT(*) FROM api_tokens WHERE revoked_at IS NULL AND expires_at > ?", (stamp,))
    m["tokens_write"] = _count(conn, "SELECT COUNT(*) FROM api_tokens WHERE revoked_at IS NULL AND expires_at > ? "
                                     "AND scope = 'write'", (stamp,))
    m["tokens_expiring"] = _count(conn, "SELECT COUNT(*) FROM api_tokens WHERE revoked_at IS NULL AND expires_at > ? "
                                        "AND expires_at <= ?", (stamp, soon))
    m["mcp_calls"] = _count(conn, "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'mcp.%' AND outcome = 'ok' AND at >= ?",
                            (since,))
    m["mcp_denied"] = _count(conn, "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'mcp.%' AND outcome = 'denied' "
                                   "AND at >= ?", (since,))
    m["guardrail_hits"] = [dict(r) for r in conn.execute(
        "SELECT substr(action, 11) AS guard, COUNT(*) AS n FROM audit_log WHERE action LIKE 'guardrail.%' AND at >= ? "
        "GROUP BY action ORDER BY n DESC", (since,))]
    m["secrets_redacted"] = _count(conn, "SELECT SUM(CAST(detail AS INTEGER)) FROM audit_log "
                                         "WHERE action = 'security.secret_redacted' AND at >= ?", (since,))
    m["secrets_in_log"] = sum(1 for r in conn.execute("SELECT target, detail FROM audit_log")
                              if SECRET.search(f"{r[0] or ''} {r[1] or ''}"))
    left = [r[0] for r in conn.execute("SELECT discord_id FROM members WHERE consent = 0 AND discord_id IS NOT NULL")]
    m["opted_out"] = len(left)
    m["opted_out_messages_left"] = sum(_count(conn, "SELECT COUNT(*) FROM events WHERE source = 'discord' AND author = ?",
                                              (d,)) for d in left)
    m["audit"] = auth.verify_audit(conn)
    row = conn.execute("SELECT value, updated_at FROM sync_state WHERE key = 'backup:last'").fetchone()
    m["backup"] = {**json.loads(row[0]), "at": row[1]} if row else None
    m["backup_key_set"] = bool(config.get("TEAMPULSE_BACKUP_KEY"))
    m["coverage"] = authorization_coverage()
    m["auth_on"] = (config.get("TEAMPULSE_AUTH", "on") or "on").lower() != "off"
    m["public_demo"] = auth.public_demo()
    return m
