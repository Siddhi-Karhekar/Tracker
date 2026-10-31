"""Accounts, passwords, sessions and permissions.

Two roles:
  leader  full control: creates and manages accounts, changes data and settings, sees every page
  member  read-only: views the pages the leader allows; may approve or hide ONLY their own contribution
          report (the consent step), unless TEAMPULSE_CONTRIB_REVIEW=off

Security choices:
  * passwords: scrypt (memory-hard, in Python's standard library) with a random 16-byte salt per user;
    compared in constant time; never stored, logged or shown after the one-time hand-over
  * leader-issued passwords are random and must be changed at first sign-in
  * sessions: random 256-bit tokens kept server-side (Streamlit session state, never in the browser's
    storage); only a SHA-256 of the token is stored; 60-minute idle and 12-hour absolute expiry; sign out,
    deactivation and password resets revoke them immediately
  * sign-in throttling per username, applied the same way to names that don't exist, so attempts can't
    discover which accounts exist
  * every permission check happens here, server-side; the UI only hides what the server would refuse

    python -m teampulse.auth create-leader --name "Neha Sharma"     # first account (prints a one-time password)
    python -m teampulse.auth list
    python -m teampulse.auth reset <username>
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from teampulse import config

ROLES = ("leader", "member")
ROLE_NAME = {"leader": "Team leader", "member": "Team member"}

# Permissions. Leaders have all of them; members only those listed.
PERMISSIONS = {
    "view": "See dashboards and project data",
    "ask": "Ask questions in chat",
    "contrib.review_own": "Approve or hide their own contribution report",
    "data.refresh": "Rebuild tasks, read new chat, compare sources",
    "data.edit": "Mark conflicts, correct chat links, label messages",
    "memory.write": "Add or remove team memory",
    "users.manage": "Create accounts, reset passwords, activate or deactivate",
    "settings.manage": "Choose what members can see",
    "sources.check": "Check connections and test the AI model",
    "eval.run": "Run evaluations",
    "pm.manage": "Create projects and tasks, assign and reassign, edit deadlines, approve requests, mark complete",
    "pm.work": "Update their own tasks (status, progress, subtasks), report blockers, request deadline changes, "
               "comment on any task",
    "audit.read": "Read the activity log and the Security page",
}
MEMBER_PERMISSIONS = {"view", "ask", "contrib.review_own", "pm.work"}
# What a leader may grant one member on top of that (fine-grained, revocable). Account and security
# administration (users.manage, settings.manage) is never grantable: there's no path to becoming an admin.
GRANTABLE = {
    "pm.manage": "Co-lead: plan, assign, approve and complete tasks; open the Team dashboard",
    "data.edit": "Fix data: mark conflicts, correct chat links, label chat",
    "data.refresh": "Refresh data: rebuild tasks, read new chat, compare sources",
    "memory.write": "Change team memory",
    "audit.read": "Read the activity log and the Security page",
}
NEVER_GRANTABLE = {"users.manage", "settings.manage"}
# Admin pages a grant opens (pages a member normally can't see).
PAGE_PERMISSION = {"dashboard": "pm.manage", "label": "data.edit", "security": "audit.read"}

# Pages. Admin-only pages can never be granted to members.
PAGES = {"home": "Home", "ask": "Ask", "blockers": "Blockers", "conflicts": "Conflicts", "tasks": "Tasks",
         "contributions": "Contributions", "sources": "Data sources", "memory": "Team memory"}
ADMIN_PAGES = {"label": "Label chat", "team": "Team & access", "dashboard": "Team dashboard",
               "security": "Security"}
# Every signed-in person always has these; they aren't in the leader's page settings.
PERSONAL_PAGES = {"workspace": "My work", "board": "Project board"}
DEFAULT_MEMBER_PAGES = ["home", "ask", "blockers", "conflicts", "tasks", "contributions", "memory"]

SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 10
MAX_FAILURES = 5
LOCK_MINUTES = 15
COMMON = {"password", "password1", "password123", "1234567890", "qwertyuiop", "iloveyou12", "admin12345",
          "letmein123", "welcome123", "teampulse1", "abc1234567"}


class AuthError(Exception):
    """A sign-in or account problem with a message that is safe to show the user."""


class PermissionDenied(Exception):
    """The signed-in user's role doesn't allow this action."""


@dataclass(frozen=True)
class Actor:
    id: str
    username: str
    display_name: str
    role: str
    member_name: str | None = None
    must_change: bool = False
    grants: frozenset = frozenset()     # extra capabilities from user_grants, loaded on every request
    session: str | None = None          # hash of the session token (None: AI-assistant token, CLI, local mode)

    @property
    def is_leader(self) -> bool:
        return self.role == "leader"


LOCAL_ADMIN = Actor("local", "local", "Local admin", "leader", None, False)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---- passwords ------------------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"],
                            dklen=SCRYPT["dklen"])
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = None


def _burn_time(password: str) -> None:
    """Spend the same time as a real check, so response time doesn't reveal whether a username exists."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(12))
    verify_password(password, _DUMMY_HASH)


def password_problem(password: str, username: str = "", display_name: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password) > 128:
        return "Use at most 128 characters."
    low = password.lower()
    if low in COMMON or len(set(low)) < 5:
        return "That password is too easy to guess. Try a short phrase of unrelated words."
    for part in [username, *display_name.split()]:
        if len(part) >= 3 and part.lower() in low:
            return "Don't include your name or username in your password."
    return None


def generate_password() -> str:
    """A one-time password: 14 characters, letters and digits, no look-alikes (0/O, 1/l/I)."""
    alphabet = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
    while True:
        candidate = "".join(secrets.choice(alphabet) for _ in range(14))
        if any(c.isdigit() for c in candidate) and any(c.isupper() for c in candidate):
            return candidate


# ---- audit -------------------------------------------------------------------------------------------

def audit(conn: sqlite3.Connection, actor: Actor | None, action: str, target: str = "", outcome: str = "ok",
          detail: str = "") -> None:
    with conn:
        conn.execute("INSERT INTO audit_log (at, actor_id, actor_name, action, target, outcome, detail) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (iso(now()), actor.id if actor else None, actor.display_name if actor else None, action,
                      str(target or "")[:200], outcome, str(detail or "")[:300]))
    seal_audit(conn)


# ---- tamper-evident activity log ---------------------------------------------------------------------------
# Every entry carries the hash of the one before it, so changing or deleting an entry breaks the chain from there
# on, and verify_audit() says where. Entries are sealed in id order under SQLite's write lock, so the app, the poller
# and the bot writing at once can't fork the chain. The newest hash is also kept separately, which catches entries
# removed from the end. (Someone with full write access to the database file could rebuild the whole chain; keep
# backups, whose verification records the head hash, to compare against.)

AUDIT_FIELDS = ("at", "actor_id", "actor_name", "action", "target", "outcome", "detail")


def _digest(prev: str, row) -> str:
    payload = json.dumps([prev] + [row[f] for f in AUDIT_FIELDS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def seal_audit(conn) -> int:
    if conn.in_transaction:      # inside someone else's transaction: the next audit() call seals it
        return 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Continue from the recorded head, not from whatever row is last: if entries were cut off the end, the next
        # entry then points at a hash that no longer exists, and the gap stays visible.
        head = conn.execute("SELECT value FROM settings WHERE key = 'audit:head'").fetchone()
        last = conn.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        prev = json.loads(head[0])["hash"] if head else (last[0] if last else "genesis")
        rows = conn.execute(f"SELECT id, {', '.join(AUDIT_FIELDS)} FROM audit_log WHERE hash IS NULL ORDER BY id"
                            ).fetchall()
        for r in rows:
            h = _digest(prev, r)
            conn.execute("UPDATE audit_log SET prev_hash = ?, hash = ? WHERE id = ?", (prev, h, r["id"]))
            prev = h
        if rows:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('audit:head', ?)",
                         (json.dumps({"id": rows[-1]["id"], "hash": prev}),))
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise


def verify_audit(conn) -> dict:
    """Recompute the chain. The first entry still kept (older ones may be removed by retention) anchors it."""
    seal_audit(conn)
    rows = conn.execute(f"SELECT id, prev_hash, hash, {', '.join(AUDIT_FIELDS)} FROM audit_log ORDER BY id").fetchall()
    if not rows:
        return {"ok": True, "checked": 0}
    prev = rows[0]["prev_hash"]
    for r in rows:
        if r["prev_hash"] != prev or _digest(prev, r) != r["hash"]:
            return {"ok": False, "checked": len(rows), "first_bad_id": r["id"], "at": r["at"],
                    "reason": "an entry was changed or removed here"}
        prev = r["hash"]
    head = conn.execute("SELECT value FROM settings WHERE key = 'audit:head'").fetchone()
    if head:
        h = json.loads(head[0])
        if h["id"] > rows[-1]["id"] or (h["id"] == rows[-1]["id"] and h["hash"] != prev):
            return {"ok": False, "checked": len(rows), "first_bad_id": rows[-1]["id"], "at": rows[-1]["at"],
                    "reason": "the newest entries were removed"}
    return {"ok": True, "checked": len(rows), "since": rows[0]["at"], "head": prev}


# ---- accounts --------------------------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")


def _grants(conn, user_id: str) -> frozenset:
    """Only allowlisted capabilities count, whatever the table says."""
    return frozenset(r[0] for r in conn.execute("SELECT permission FROM user_grants WHERE user_id = ?", (user_id,))
                     if r[0] in GRANTABLE)


def _row_actor(row: sqlite3.Row, conn=None) -> Actor:
    return Actor(row["id"], row["username"], row["display_name"], row["role"], row["member_name"],
                 bool(row["must_change"]), _grants(conn, row["id"]) if conn is not None else frozenset())


def suggest_username(conn: sqlite3.Connection, display_name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", ".", display_name.lower()).strip(".")[:24] or "member"
    if not base[0].isalpha():
        base = "u." + base
    base = base if len(base) >= 3 else base + ".tp"
    name, n = base, 2
    while conn.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
        name, n = f"{base}{n}", n + 1
    return name


def validate_new_user(conn, display_name: str, username: str, role: str, member_name: str | None) -> None:
    display_name = display_name.strip()
    if not 2 <= len(display_name) <= 60 or any(c in display_name for c in "<>\"`{}\\"):
        raise AuthError("Enter a name of 2 to 60 characters, without < > \" ` { } or \\.")
    if not USERNAME_RE.match(username):
        raise AuthError("Usernames are 3 to 32 characters: lowercase letters, digits, dots, dashes or underscores, "
                        "starting with a letter.")
    if role not in ROLES:
        raise AuthError("Choose a valid role.")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError(f"The username {username} is already taken.")
    if member_name and not conn.execute("SELECT 1 FROM members WHERE display_name = ?", (member_name,)).fetchone():
        raise AuthError("That team member record doesn't exist.")


def _insert_user(conn, display_name, username, role, member_name, password, must_change, created_by,
                 only_if_no_leader: bool = False) -> str:
    user_id = secrets.token_hex(12)
    password_hash = hash_password(password)   # slow on purpose; done before the write lock is taken
    # only_if_no_leader makes "is there a leader yet?" and the insert one statement, so two people setting up
    # at the same moment can't both become the first leader.
    guard = " WHERE NOT EXISTS (SELECT 1 FROM users WHERE role = 'leader')" if only_if_no_leader else ""
    with conn:
        inserted = conn.execute(
            "INSERT INTO users (id, username, display_name, role, password_hash, must_change, active, member_name, "
            "created_at, created_by) SELECT ?, ?, ?, ?, ?, ?, 1, ?, ?, ?" + guard,
            (user_id, username, display_name.strip(), role, password_hash, int(must_change), member_name or None,
             iso(now()), created_by)).rowcount
    if not inserted:
        raise AuthError("A team leader already exists. Ask them to create your account.")
    return user_id


def create_first_leader(conn, display_name: str, username: str | None = None, password: str | None = None,
                        must_change: bool = True) -> tuple[str, str]:
    """Bootstrap: allowed only while no leader exists. Returns (username, password to hand over)."""
    if conn.execute("SELECT 1 FROM users WHERE role = 'leader'").fetchone():
        raise AuthError("A team leader already exists. Ask them to create your account.")
    username = (username or suggest_username(conn, display_name)).lower()
    validate_new_user(conn, display_name, username, "leader", None)
    if password is not None:
        problem = password_problem(password, username, display_name)
        if problem:
            raise AuthError(problem)
    password = password or generate_password()
    user_id = _insert_user(conn, display_name, username, "leader", None, password, must_change, None,
                           only_if_no_leader=True)
    audit(conn, Actor(user_id, username, display_name, "leader"), "account.create_first_leader", username)
    return username, password


def list_users(conn) -> list[dict]:
    rows = conn.execute("SELECT u.id, u.username, u.display_name, u.role, u.active, u.must_change, u.member_name, "
                        "u.created_at, u.last_login, t.locked_until FROM users u LEFT JOIN login_throttle t "
                        "ON t.key = lower(u.username) ORDER BY u.role, u.display_name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["locked"] = bool(r["locked_until"] and r["locked_until"] > iso(now()))
        out.append(d)
    return out


def get_user(conn, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# ---- sessions ----------------------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_hours() -> float:
    return float(config.get("TEAMPULSE_SESSION_HOURS", "12"))


def idle_minutes() -> float:
    return float(config.get("TEAMPULSE_IDLE_MINUTES", "60"))


def _throttle_key(username: str) -> str:
    return username.strip().lower()[:64]


def _check_lock(conn, key: str) -> None:
    row = conn.execute("SELECT locked_until FROM login_throttle WHERE key = ?", (key,)).fetchone()
    if row and row["locked_until"] and row["locked_until"] > iso(now()):
        minutes = max(1, int((datetime.fromisoformat(row["locked_until"]) - now()).total_seconds() // 60) + 1)
        raise AuthError(f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")


def _record_failure(conn, key: str) -> bool:
    """Count a failed attempt; returns True if this one locked the key.

    One transaction, and the count is incremented inside SQLite (failures + 1), so guesses sent in parallel
    can't overwrite each other's count and dodge the lock (tests/test_concurrency.py)."""
    with conn:
        conn.execute("INSERT INTO login_throttle (key, failures) VALUES (?, 1) "
                     "ON CONFLICT(key) DO UPDATE SET failures = failures + 1", (key,))
        failures = conn.execute("SELECT failures FROM login_throttle WHERE key = ?", (key,)).fetchone()[0]
        if failures < MAX_FAILURES:
            return False
        conn.execute("UPDATE login_throttle SET failures = 0, locked_until = ? WHERE key = ?",
                     (iso(now() + timedelta(minutes=LOCK_MINUTES)), key))
    return True


def login(conn, username: str, password: str) -> tuple[str, Actor]:
    """Returns (session token, actor) or raises AuthError with a safe, specific message."""
    key = _throttle_key(username)
    _check_lock(conn, key)
    user = conn.execute("SELECT * FROM users WHERE username = ?", (key,)).fetchone()
    ok = user is not None and verify_password(password, user["password_hash"])
    if user is None:
        _burn_time(password)
    if not ok:
        locked = _record_failure(conn, key)
        audit(conn, None, "auth.login", key, "failed", "locked" if locked else "")
        time.sleep(0.4)
        if locked:
            raise AuthError(f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.")
        raise AuthError("That username and password don't match. Check both and try again.")
    if not user["active"]:
        audit(conn, _row_actor(user), "auth.login", key, "denied", "deactivated")
        raise AuthError("This account is deactivated. Ask your team leader to reactivate it.")
    with conn:
        conn.execute("DELETE FROM login_throttle WHERE key = ?", (key,))
    token = _issue_session(conn, user["id"])
    actor = replace(_row_actor(user, conn), session=_token_hash(token))
    audit(conn, actor, "auth.login", key)
    return token, actor


def _issue_session(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    stamp = now()
    with conn:
        conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_seen, expires_at) VALUES (?, ?, ?, ?, ?)",
                     (_token_hash(token), user_id, iso(stamp), iso(stamp), iso(stamp + timedelta(hours=session_hours()))))
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (iso(stamp), user_id))
        conn.execute("DELETE FROM sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
                     (iso(stamp), iso(stamp - timedelta(days=7))))
    return token


def demo_session(conn, username: str) -> tuple[str, Actor]:
    """One-click sign-in to the fictional demo accounts. Only callable when public demo mode is on."""
    if (config.get("TEAMPULSE_PUBLIC_DEMO", "off") or "off").lower() != "on":
        raise AuthError("Demo sign-in is only available on the public demo.")
    user = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
    if user is None:
        raise AuthError("The demo accounts aren't set up yet. Reload the page.")
    token = _issue_session(conn, user["id"])
    actor = replace(_row_actor(user, conn), session=_token_hash(token))
    audit(conn, actor, "auth.demo_login", username)
    return token, actor


def resolve(conn, token: str | None) -> Actor | None:
    """The signed-in user for this token, or None if missing, expired, idle too long, revoked or deactivated."""
    if not token:
        return None
    row = conn.execute("SELECT s.*, u.username, u.display_name, u.role, u.member_name, u.must_change, u.active "
                       "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                       (_token_hash(token),)).fetchone()
    stamp = now()
    if row is None or row["revoked_at"] or not row["active"] or row["expires_at"] < iso(stamp) or \
            datetime.fromisoformat(row["last_seen"]) < stamp - timedelta(minutes=idle_minutes()):
        return None
    with conn:
        conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (iso(stamp), row["token_hash"]))
    return Actor(row["user_id"], row["username"], row["display_name"], row["role"], row["member_name"],
                 bool(row["must_change"]), _grants(conn, row["user_id"]), row["token_hash"])


# ---- re-authentication for high-impact actions ------------------------------------------------------------

REAUTH_MINUTES = 10


def confirm_password(conn, actor: Actor, password: str) -> None:
    """"Confirm it's you": marks this session (only this one) as recently authenticated. Throttled like sign-in."""
    if actor is None or not actor.session:
        raise AuthError("Sign in again to do this.")
    key = _throttle_key(actor.username)
    _check_lock(conn, key)
    user = get_user(conn, actor.id)
    if user is None or not verify_password(password or "", user["password_hash"]):
        locked = _record_failure(conn, key)
        audit(conn, actor, "auth.reauth", actor.username, "failed", "locked" if locked else "")
        raise AuthError("That password isn't right." + (f" Try again in {LOCK_MINUTES} minutes." if locked else ""))
    with conn:
        conn.execute("DELETE FROM login_throttle WHERE key = ?", (key,))
        conn.execute("UPDATE sessions SET reauth_at = ? WHERE token_hash = ?", (iso(now()), actor.session))
    audit(conn, actor, "auth.reauth", actor.username)


def recently_authenticated(conn, actor: Actor | None) -> bool:
    if actor is not None and actor.id == LOCAL_ADMIN.id:
        return True        # sign-in switched off (local development only): there's no password to confirm
    if actor is None or not actor.session:
        return False
    row = conn.execute("SELECT reauth_at FROM sessions WHERE token_hash = ? AND revoked_at IS NULL",
                       (actor.session,)).fetchone()
    return bool(row and row[0] and row[0] >= iso(now() - timedelta(minutes=REAUTH_MINUTES)))


def require_recent_auth(conn, actor: Actor | None, what: str) -> None:
    if not recently_authenticated(conn, actor):
        audit(conn, actor, f"reauth.{what}", "", "denied", "no recent password confirmation")
        raise PermissionDenied(f"For your team's safety, confirm your password first (valid {REAUTH_MINUTES} minutes).")


def logout(conn, token: str | None) -> None:
    if not token:
        return
    actor = resolve(conn, token)
    with conn:
        conn.execute("UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                     (iso(now()), _token_hash(token)))
    if actor:
        audit(conn, actor, "auth.logout", actor.username)


def revoke_all(conn, user_id: str, except_token: str | None = None) -> None:
    """Ends every session (except the current one, if given) and every AI-assistant token: a password reset,
    deactivation or "sign out of other devices" must also cut off anything connected with the old access."""
    keep = _token_hash(except_token) if except_token else ""
    with conn:
        conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND token_hash != ?",
                     (iso(now()), user_id, keep))
        conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (iso(now()), user_id))


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
    if row is None or row["revoked_at"] or row["expires_at"] <= iso(now()) or not row["active"] or row["must_change"]:
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


def change_password(conn, actor: Actor, token: str, current: str, new: str) -> None:
    key = _throttle_key(actor.username)          # same counter as sign-in: a stolen session can't guess it
    _check_lock(conn, key)
    user = get_user(conn, actor.id)
    if user is None or not verify_password(current, user["password_hash"]):
        locked = _record_failure(conn, key)
        audit(conn, actor, "auth.change_password", actor.username, "failed", "wrong current password")
        if locked:
            raise AuthError(f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes.")
        raise AuthError("Your current password isn't right.")
    if hmac.compare_digest(current, new):
        raise AuthError("Choose a password different from the current one.")
    problem = password_problem(new, actor.username, actor.display_name)
    if problem:
        raise AuthError(problem)
    with conn:
        conn.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?", (hash_password(new), actor.id))
    revoke_all(conn, actor.id, except_token=token)   # other devices must sign in again
    audit(conn, actor, "auth.change_password", actor.username)


# ---- permissions -------------------------------------------------------------------------------------

SETUP_CODE_MIN = 16
SETUP_KEY = "!setup"   # "!" can't appear in a username, so this counter is never shared with an account


def check_setup_code(conn, given: str, expected: str | None) -> None:
    """Gate for creating the first leader from the sign-in page. Throttled like sign-in, and the configured
    code must be long enough that guessing it isn't practical."""
    if not expected or len(expected) < SETUP_CODE_MIN:
        raise AuthError(f"TEAMPULSE_SETUP_CODE must be at least {SETUP_CODE_MIN} characters. Set a longer one "
                        "in the app's settings, or create the leader from the command line.")
    _check_lock(conn, SETUP_KEY)
    if not hmac.compare_digest(given.encode(), expected.encode()):
        locked = _record_failure(conn, SETUP_KEY)
        audit(conn, None, "account.create_first_leader", "", "denied", "wrong setup code")
        time.sleep(0.4)
        raise AuthError(f"Too many failed attempts. Try again in {LOCK_MINUTES} minutes." if locked
                        else "That setup code isn't right.")


def contrib_review_enabled() -> bool:
    return (config.get("TEAMPULSE_CONTRIB_REVIEW", "on") or "on").lower() != "off"


# In the public demo every visitor signs in as the same demo leader, so anything that would change the demo
# for everyone else (accounts, settings) or spend the shared AI quota in bulk is switched off.
DEMO_LOCKED = {"users.manage", "settings.manage", "eval.run", "sources.check"}


def public_demo() -> bool:
    return (config.get("TEAMPULSE_PUBLIC_DEMO", "off") or "off").lower() == "on"


def can(actor: Actor | None, permission: str) -> bool:
    """Deny by default: an unknown permission, no account, or an account that must change its password gets nothing."""
    if actor is None or actor.must_change or permission not in PERMISSIONS:
        return False
    if permission in DEMO_LOCKED and public_demo():
        return False
    if actor.role == "leader":
        return True
    if permission == "contrib.review_own" and not contrib_review_enabled():
        return False
    return permission in MEMBER_PERMISSIONS or (permission in actor.grants and permission in GRANTABLE)


# Resource rules (ABAC): having a permission isn't enough; it must also be *this* person's resource, unless they
# manage it. One place for every "is this mine?" decision, so pages, AI agents and MCP tools all agree.
def _owns_task(conn, actor: Actor, task) -> bool:
    if can(actor, "pm.manage"):
        return True
    mine = (actor.member_name or "").lower()
    return bool(mine) and mine in {a.lower() for a in task.get("assignees", [])}


RESOURCE_RULES = {
    "pm.work": lambda conn, actor, task: _owns_task(conn, actor, task),
    "contrib.review_own": lambda conn, actor, member: bool(actor.member_name) and actor.member_name == member,
    "workspace.read": lambda conn, actor, member: can(actor, "pm.manage") or (
        bool(actor.member_name) and actor.member_name.lower() == str(member).lower()),
    "token.revoke": lambda conn, actor, owner_id: owner_id == actor.id or can(actor, "users.manage"),
}
# Resource-level permissions that aren't plain capabilities: who may do them is decided by the rule alone.
DERIVED = {"workspace.read": "view", "token.revoke": "view"}


def authorize(conn, actor: Actor | None, permission: str, resource=None) -> bool:
    """The policy decision point: capability check, then the resource rule if there's a resource. Never raises."""
    base = DERIVED.get(permission, permission)
    if not can(actor, base):
        return False
    rule = RESOURCE_RULES.get(permission)
    if resource is not None and rule is not None:
        return bool(rule(conn, actor, resource))
    return permission not in DERIVED or resource is not None


def require(conn, actor: Actor | None, permission: str, target: str = "") -> Actor:
    """The server-side gate. Every change to data goes through this first."""
    if not can(actor, permission):
        audit(conn, actor, permission, target, "denied")
        if actor and permission in DEMO_LOCKED and public_demo():
            raise PermissionDenied("This is switched off in the public demo, so visitors can't change it for "
                                   "everyone else. It works normally on your own copy.")
        who = ROLE_NAME.get(actor.role, "Your account") if actor else "You"
        raise PermissionDenied(f"{who}s can't do this. Only the team leader can."
                               if actor and actor.role == "member" else f"{who} must sign in to do this.")
    return actor


def member_pages(conn) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = 'member_pages'").fetchone()
    pages = json.loads(row[0]) if row else DEFAULT_MEMBER_PAGES
    return [p for p in pages if p in PAGES]


def page_allowed(conn, actor: Actor | None, page: str) -> bool:
    if actor is None or actor.must_change:
        return False
    if page in PERSONAL_PAGES:
        return True
    if actor.role == "leader":
        return page in PAGES or page in ADMIN_PAGES
    if page in PAGE_PERMISSION:              # an admin page a grant opens
        return can(actor, PAGE_PERMISSION[page])
    return page in member_pages(conn)


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


# ---- CLI (for whoever runs the server; it has direct database access anyway) -----------------------

def main():
    from teampulse import db

    parser = argparse.ArgumentParser(prog="teampulse.auth")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-leader", help="create the first team leader account")
    p.add_argument("--name", required=True)
    p.add_argument("--username")
    sub.add_parser("list")
    p = sub.add_parser("reset", help="issue a new one-time password and sign the user out everywhere")
    p.add_argument("username")
    args = parser.parse_args()
    conn = db.connect()
    if args.cmd == "create-leader":
        username, password = create_first_leader(conn, args.name, args.username)
        print(f"Team leader account created.\n  username: {username}\n  one-time password: {password}\n"
              "Sign in and you'll be asked to choose your own password. This password won't be shown again.")
    elif args.cmd == "list":
        for u in list_users(conn):
            state = "active" if u["active"] else "deactivated"
            print(f"{u['username']:24} {ROLE_NAME[u['role']]:12} {state:12} last sign-in {u['last_login'] or 'never'}")
    elif args.cmd == "reset":
        user = conn.execute("SELECT * FROM users WHERE username = ?", (args.username.lower(),)).fetchone()
        if user is None:
            raise SystemExit("No such user.")
        password = generate_password()
        with conn:
            conn.execute("UPDATE users SET password_hash = ?, must_change = 1 WHERE id = ?", (hash_password(password), user["id"]))
            conn.execute("DELETE FROM login_throttle WHERE key = ?", (user["username"],))
        revoke_all(conn, user["id"])
        audit(conn, None, "account.reset_cli", user["username"])
        print(f"New one-time password for {user['username']}: {password}")


if __name__ == "__main__":
    main()
