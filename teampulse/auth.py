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
from dataclasses import dataclass
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
}
MEMBER_PERMISSIONS = {"view", "ask", "contrib.review_own", "pm.work"}

# Pages. Admin-only pages can never be granted to members.
PAGES = {"home": "Home", "ask": "Ask", "blockers": "Blockers", "conflicts": "Conflicts", "tasks": "Tasks",
         "contributions": "Contributions", "sources": "Data sources", "memory": "Team memory"}
ADMIN_PAGES = {"label": "Label chat", "team": "Team & access", "dashboard": "Team dashboard"}
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
                      target[:200], outcome, detail[:300]))


# ---- accounts --------------------------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")


def _row_actor(row: sqlite3.Row) -> Actor:
    return Actor(row["id"], row["username"], row["display_name"], row["role"], row["member_name"], bool(row["must_change"]))


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
    actor = _row_actor(user)
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
    actor = _row_actor(user)
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
    return Actor(row["user_id"], row["username"], row["display_name"], row["role"], row["member_name"], bool(row["must_change"]))


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
    keep = _token_hash(except_token) if except_token else ""
    with conn:
        conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL AND token_hash != ?",
                     (iso(now()), user_id, keep))


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
    if actor is None or actor.must_change:
        return False
    if permission in DEMO_LOCKED and public_demo():
        return False
    if actor.role == "leader":
        return permission in PERMISSIONS
    if permission == "contrib.review_own" and not contrib_review_enabled():
        return False
    return permission in MEMBER_PERMISSIONS


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
    return page in member_pages(conn)


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
