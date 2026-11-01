"""Two-factor sign-in with an authenticator app (TOTP, RFC 6238), using only the standard library.

  - Works with Google Authenticator, Microsoft Authenticator, Aegis, 1Password and others (type the key in, or
    open the otpauth:// link on the phone).
  - A code can be used once: the last accepted time step is stored, so a code seen over someone's shoulder or
    replayed from a log is refused.
  - Eight one-time recovery codes are shown once at setup; only their hashes are stored.
  - Signing in is two steps. The password step only creates a short-lived "pending" token (5 minutes) that is not
    a session; the session is issued after the code. Wrong codes count towards the same lockout as passwords.
  - TEAMPULSE_MFA=leaders makes it required for team leaders: until they set it up they can only reach My account.
  - If someone loses their phone and their recovery codes, a leader's password reset (which needs the leader's own
    recent confirmation) clears it, and they set it up again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote

from teampulse import config
from teampulse.auth import (LOCK_MINUTES, Actor, AuthError, _check_lock, _issue_session, _record_failure, _row_actor,
                            _throttle_key, _token_hash, audit, get_user, iso, now, verify_password)

STEP = 30
DIGITS = 6
WINDOW = 1            # accept the previous and next 30 seconds (phone clocks drift)
PENDING_MINUTES = 5
RECOVERY_CODES = 8


def policy() -> str:
    """optional (default): anyone may turn it on. leaders: required for team leaders."""
    value = (config.get("TEAMPULSE_MFA", "optional") or "optional").lower()
    return value if value in ("optional", "leaders") else "optional"


def required_for(role: str) -> bool:
    return policy() == "leaders" and role == "leader"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** DIGITS).zfill(DIGITS)


def code_at(secret: str, when: float | None = None) -> str:
    return _code(secret, int((time.time() if when is None else when) // STEP))


def _match(secret: str, code: str, after_step: int | None, when: float | None = None) -> int | None:
    """The time step the code belongs to, if it's valid and newer than the last one used."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = int((time.time() if when is None else when) // STEP)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(_code(secret, step), code) and (after_step is None or step > after_step):
            return step
    return None


def provisioning_uri(secret: str, username: str) -> str:
    return f"otpauth://totp/TeamPulse:{quote(username)}?secret={secret}&issuer=TeamPulse&digits={DIGITS}&period={STEP}"


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").strip().lower().encode()).hexdigest()


def enabled(conn, user_id: str) -> bool:
    row = conn.execute("SELECT mfa_enabled_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


# ---- setting it up and turning it off ------------------------------------------------------------------------

def enable(conn, actor: Actor, secret: str, code: str, password: str) -> list[str]:
    """Turn it on after proving the phone works. Returns the recovery codes (shown once)."""
    if actor is None or actor.id == "local":
        raise AuthError("Sign in with your own account to set up two-factor sign-in.")
    user = get_user(conn, actor.id)
    if user is None or not verify_password(password or "", user["password_hash"]):
        audit(conn, actor, "mfa.enable", actor.username, "failed", "password")
        raise AuthError("That password isn't right.")
    step = _match(secret, code, None)
    if step is None:
        raise AuthError("That code doesn't match. Check the phone's time is set automatically, and type the newest "
                        "code.")
    codes = ["-".join(secrets.token_hex(4)[i:i + 4] for i in (0, 4)) for _ in range(RECOVERY_CODES)]
    with conn:
        conn.execute("UPDATE users SET mfa_secret = ?, mfa_enabled_at = ?, mfa_last_step = ?, mfa_recovery = ? "
                     "WHERE id = ?", (secret, iso(now()), step, json.dumps([_hash_recovery(c) for c in codes]),
                                      actor.id))
    audit(conn, actor, "mfa.enable", actor.username)
    return codes


def disable(conn, actor: Actor, password: str, code: str) -> None:
    if required_for(actor.role):
        raise AuthError("Two-factor sign-in is required for team leaders here, so it can't be turned off.")
    user = get_user(conn, actor.id)
    if user is None or not verify_password(password or "", user["password_hash"]) or not check_code(conn, actor.id, code):
        audit(conn, actor, "mfa.disable", actor.username, "failed")
        raise AuthError("The password or code isn't right.")
    clear(conn, actor.id)
    audit(conn, actor, "mfa.disable", actor.username)


def clear(conn, user_id: str) -> None:
    """Used by disable() and by a leader's password reset (account recovery)."""
    with conn:
        conn.execute("UPDATE users SET mfa_secret = NULL, mfa_enabled_at = NULL, mfa_last_step = NULL, "
                     "mfa_recovery = NULL WHERE id = ?", (user_id,))


def check_code(conn, user_id: str, code: str) -> bool:
    """A current authenticator code (never one already used) or an unused recovery code, which is then spent."""
    user = get_user(conn, user_id)
    if user is None or not user["mfa_enabled_at"]:
        return False
    step = _match(user["mfa_secret"], code, user["mfa_last_step"])
    if step is not None:
        with conn:
            changed = conn.execute("UPDATE users SET mfa_last_step = ? WHERE id = ? AND "
                                   "(mfa_last_step IS NULL OR mfa_last_step < ?)", (step, user_id, step)).rowcount
        return bool(changed)   # two requests racing with the same code: only one wins
    hashes = json.loads(user["mfa_recovery"] or "[]")
    wanted = _hash_recovery(code or "")
    if wanted in hashes:
        hashes.remove(wanted)
        with conn:
            conn.execute("UPDATE users SET mfa_recovery = ? WHERE id = ?", (json.dumps(hashes), user_id))
        return True
    return False


def recovery_left(conn, user_id: str) -> int:
    row = conn.execute("SELECT mfa_recovery FROM users WHERE id = ?", (user_id,)).fetchone()
    return len(json.loads(row[0] or "[]")) if row else 0


# ---- the second sign-in step ------------------------------------------------------------------------------------

def start_pending(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute("DELETE FROM mfa_pending WHERE expires_at < ?", (iso(now()),))
        conn.execute("INSERT INTO mfa_pending (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (_token_hash(token), user_id, iso(now() + timedelta(minutes=PENDING_MINUTES))))
    return token


def complete_login(conn, pending: str, code: str, user_agent: str | None = None) -> tuple[str, Actor]:
    row = conn.execute("SELECT * FROM mfa_pending WHERE token_hash = ?", (_token_hash(pending or ""),)).fetchone()
    if row is None or row["expires_at"] < iso(now()):
        raise AuthError("That sign-in took too long. Please enter your password again.")
    user = get_user(conn, row["user_id"])
    key = _throttle_key(user["username"])
    _check_lock(conn, key)
    if not user["active"]:
        raise AuthError("This account is deactivated. Ask your team leader to reactivate it.")
    if not check_code(conn, user["id"], code):
        locked = _record_failure(conn, key)
        audit(conn, _row_actor(user), "auth.login_code", user["username"], "failed", "locked" if locked else "")
        if locked:
            with conn:
                conn.execute("DELETE FROM mfa_pending WHERE token_hash = ?", (row["token_hash"],))
            raise AuthError(f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.")
        raise AuthError("That code doesn't match. Type the newest code from your authenticator app, or a recovery code.")
    with conn:
        conn.execute("DELETE FROM mfa_pending WHERE token_hash = ?", (row["token_hash"],))
        conn.execute("DELETE FROM login_throttle WHERE key = ?", (key,))
    token = _issue_session(conn, user["id"], user_agent)
    from dataclasses import replace
    from teampulse.auth import _grants
    actor = replace(_row_actor(user), grants=_grants(conn, user["id"]), session=_token_hash(token))
    audit(conn, actor, "auth.login", user["username"], detail="with two-factor code")
    return token, actor
