"""Personal access tokens for an AI assistant (MCP). Each acts as its owner, with the owner's permissions."""
from __future__ import annotations

import secrets
from datetime import timedelta

from teampulse.audit_log import audit
from teampulse.auth import _needs_mfa, _row_actor, _token_hash, authorize, can, public_demo, require
from teampulse.identity import LOCAL_ADMIN, Actor, AuthError, iso, now


# ---- personal access tokens for an AI assistant (MCP) ---------------------------------------------------------

TOKEN_PREFIX = "tp_"
TOKEN_DAYS = 90
MAX_TOKENS = 5
TOKEN_SCOPES = {"read": "Read only", "write": "Read, and update my own tasks"}


def create_api_token(conn, actor: Actor, name: str, scope: str = "read", days: int = TOKEN_DAYS) -> str:
    """Returns the token once; only its hash is stored. It acts as this person, never with more than their role."""
    if actor is None or actor.must_change or actor.id == LOCAL_ADMIN.id:
        raise AuthError("Sign in with your own account to connect an AI assistant.")
    if public_demo():
        raise AuthError("AI assistant connections are switched off in the public demo.")
    if scope not in TOKEN_SCOPES:
        raise AuthError("Choose read only, or read and update.")
    name = (name or "").strip()[:60] or "AI assistant"
    active = conn.execute("SELECT COUNT(*) FROM api_tokens WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?",
                          (actor.id, iso(now()))).fetchone()[0]
    if active >= MAX_TOKENS:
        raise AuthError(f"You already have {MAX_TOKENS} connections. Remove one first.")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with conn:
        conn.execute("INSERT INTO api_tokens (user_id, name, token_hash, scope, created_at, expires_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (actor.id, name, _token_hash(token), scope, iso(now()),
                                                   iso(now() + timedelta(days=max(1, min(int(days), 365))))))
    audit(conn, actor, "token.create", name, detail=scope)
    return token


def resolve_api_token(conn, token: str | None) -> tuple[Actor, str] | None:
    """(person, scope) for a valid token, or None if it's unknown, revoked, expired, or the account can't be used."""
    if not token or not str(token).startswith(TOKEN_PREFIX):
        return None
    row = conn.execute("SELECT t.id AS token_id, t.scope, t.expires_at, t.revoked_at, t.last_used, u.* "
                       "FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
                       (_token_hash(str(token)),)).fetchone()
    if row is None or row["revoked_at"] or row["expires_at"] <= iso(now()) or not row["active"] or row["must_change"] \
            or _needs_mfa(row):
        return None
    if not row["last_used"] or row["last_used"] < iso(now() - timedelta(minutes=1)):   # at most one write a minute
        with conn:
            conn.execute("UPDATE api_tokens SET last_used = ? WHERE id = ?", (iso(now()), row["token_id"]))
    return _row_actor(row, conn), row["scope"]


def list_api_tokens(conn, actor: Actor, everyone: bool = False) -> list[dict]:
    sql = ("SELECT t.id, t.name, t.scope, t.created_at, t.expires_at, t.last_used, u.display_name, u.id AS user_id "
           "FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.revoked_at IS NULL AND t.expires_at > ?")
    params: list = [iso(now())]
    if not (everyone and can(actor, "users.manage")):
        sql += " AND t.user_id = ?"
        params.append(actor.id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY t.id DESC", params)]


def revoke_api_token(conn, actor: Actor, token_id: int) -> None:
    row = conn.execute("SELECT user_id, name FROM api_tokens WHERE id = ? AND revoked_at IS NULL", (int(token_id),)).fetchone()
    if row is None:
        return
    if not authorize(conn, actor, "token.revoke", row["user_id"]):   # your own, or a leader cutting anyone's
        require(conn, actor, "users.manage", f"token {token_id}")
    with conn:
        conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (iso(now()), int(token_id)))
    audit(conn, actor, "token.revoke", row["name"])
