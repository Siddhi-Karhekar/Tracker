"""Capabilities a leader grants to a member (co-lead, audit access), on top of their role."""
from __future__ import annotations

from teampulse.audit_log import audit
from teampulse.auth import GRANTABLE, NEVER_GRANTABLE, get_user, require, require_recent_auth
from teampulse.identity import Actor, AuthError, PermissionDenied, iso, now


# ---- capability grants ------------------------------------------------------------------------------------

def list_grants(conn) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT user_id, permission FROM user_grants ORDER BY permission"):
        if r[1] in GRANTABLE:
            out.setdefault(r[0], []).append(r[1])
    return out


def set_grant(conn, actor: Actor, user_id: str, permission: str, on: bool) -> None:
    """Leader only. Members only, allowlisted capabilities only, never to yourself; takes effect on the person's next
    click or AI-assistant call, because permissions are loaded on every request."""
    require(conn, actor, "users.manage", f"grant {permission}")
    require_recent_auth(conn, actor, "grant")
    if permission not in GRANTABLE or permission in NEVER_GRANTABLE:
        audit(conn, actor, "grant.set", user_id, "denied", f"not grantable: {permission}")
        raise PermissionDenied("That can't be granted. Account and security administration stay with team leaders.")
    if user_id == actor.id:
        raise PermissionDenied("You can't change your own permissions.")
    user = get_user(conn, user_id)
    if user is None or user["role"] != "member" or not user["active"]:
        raise AuthError("Capabilities can only be granted to active team members.")
    with conn:
        if on:
            conn.execute("INSERT OR IGNORE INTO user_grants (user_id, permission, granted_by, granted_at) "
                         "VALUES (?, ?, ?, ?)", (user_id, permission, actor.id, iso(now())))
        else:
            conn.execute("DELETE FROM user_grants WHERE user_id = ? AND permission = ?", (user_id, permission))
    audit(conn, actor, "grant.add" if on else "grant.remove", user["username"], detail=permission)
