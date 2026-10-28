"""Every action that changes data, each behind a server-side permission check.

The web pages call these functions and never write to the database themselves, so a team member can't do
an admin action by any route: hidden buttons, a crafted request, or a page they weren't given. A refused
attempt raises auth.PermissionDenied and is written to the audit log.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from teampulse import auth, config
from teampulse.auth import Actor, AuthError, audit, iso, now, require

log = logging.getLogger("teampulse.actions")


class ServiceError(AuthError):
    """Something on our side failed. The message is safe to show; the details go to the server log."""


class RateLimited(AuthError):
    pass


# ---- accounts ----------------------------------------------------------------------------------------

def create_user(conn, actor: Actor, display_name: str, role: str = "member", username: str | None = None,
                member_name: str | None = None) -> tuple[str, str]:
    """Returns (username, one-time password). The password is shown to the leader once and never stored."""
    require(conn, actor, "users.manage", "create user")
    username = (username or auth.suggest_username(conn, display_name)).strip().lower()
    auth.validate_new_user(conn, display_name, username, role, member_name)
    password = auth.generate_password()
    auth._insert_user(conn, display_name, username, role, member_name, password, True, actor.id)
    audit(conn, actor, "account.create", username, detail=role)
    return username, password


def _target(conn, user_id: str):
    user = auth.get_user(conn, user_id)
    if user is None:
        raise AuthError("That account doesn't exist.")
    return user


def reset_credentials(conn, actor: Actor, user_id: str) -> tuple[str, str]:
    """New one-time password; the user is signed out everywhere and must choose a new password."""
    require(conn, actor, "users.manage", user_id)
    user = _target(conn, user_id)
    password = auth.generate_password()
    with conn:
        conn.execute("UPDATE users SET password_hash = ?, must_change = 1 WHERE id = ?", (auth.hash_password(password), user_id))
        conn.execute("DELETE FROM login_throttle WHERE key = ?", (user["username"].lower(),))
    auth.revoke_all(conn, user_id)
    audit(conn, actor, "account.reset", user["username"])
    return user["username"], password


def set_active(conn, actor: Actor, user_id: str, active: bool) -> None:
    require(conn, actor, "users.manage", user_id)
    user = _target(conn, user_id)
    if not active:
        if user_id == actor.id:
            raise AuthError("You can't deactivate your own account.")
        if user["role"] == "leader" and conn.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'leader' AND active = 1").fetchone()[0] <= 1:
            raise AuthError("The team needs at least one active leader.")
    with conn:
        conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))
    if not active:
        auth.revoke_all(conn, user_id)
    audit(conn, actor, "account.activate" if active else "account.deactivate", user["username"])


def set_member_pages(conn, actor: Actor, pages: list[str]) -> None:
    require(conn, actor, "settings.manage", "member pages")
    clean = [p for p in pages if p in auth.PAGES]
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('member_pages', ?)", (json.dumps(clean),))
    audit(conn, actor, "settings.member_pages", ",".join(clean))


# ---- data processing ---------------------------------------------------------------------------------

def refresh_all(conn, actor: Actor, index, llm) -> dict:
    require(conn, actor, "data.refresh", "compare sources")
    from teampulse import discrepancies
    stats = discrepancies.run(conn, index, llm, refresh=True)
    audit(conn, actor, "data.refresh")
    return stats


def rebuild_tasks(conn, actor: Actor, index, llm) -> dict:
    require(conn, actor, "data.refresh", "rebuild tasks")
    from teampulse import claims
    stats = claims.refresh(conn, index, llm, scan_chat=True)
    audit(conn, actor, "data.rebuild_tasks")
    return stats


def scan_chat(conn, actor: Actor, classifier, index, rescan: bool = False) -> dict:
    require(conn, actor, "data.refresh", "read chat")
    from teampulse import signals
    stats = signals.scan(conn, classifier, index, rescan=rescan)
    audit(conn, actor, "data.scan_chat", detail="rescan" if rescan else "")
    return stats


_first_build_lock = threading.Lock()


def first_build(conn, index, llm) -> None:
    """Automatic, once, when a database has never been processed. Changes no one's data, so any viewer may start it.

    If several people open the app at that moment, one builds and the others wait for it, instead of each
    starting the same AI-heavy build."""
    from teampulse import discrepancies
    with _first_build_lock:
        if conn.execute("SELECT 1 FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone():
            return   # someone else finished it while we waited
        discrepancies.run(conn, index, llm, refresh=True)


# ---- data edits ----------------------------------------------------------------------------------------

def set_verdict(conn, actor: Actor, discrepancy_id: str, verdict: str) -> None:
    require(conn, actor, "data.edit", discrepancy_id)
    if verdict not in ("real", "false_alarm"):
        raise AuthError("Unknown verdict.")
    from teampulse import discrepancies
    discrepancies.set_verdict(conn, discrepancy_id, verdict, actor.display_name)
    audit(conn, actor, "conflict.verdict", discrepancy_id, detail=verdict)


def correct_link(conn, actor: Actor, event_id: str, task_id: str | None, index=None) -> None:
    require(conn, actor, "data.edit", event_id)
    if task_id and not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise AuthError("That task doesn't exist.")
    from teampulse import claims
    from teampulse.tasks import save_feedback
    save_feedback(conn, event_id, task_id, actor.display_name)
    claims.refresh(conn, index, None)
    audit(conn, actor, "link.correct", event_id, detail=task_id or "none")


def add_label(conn, actor: Actor, event_id: str, label: str) -> None:
    require(conn, actor, "data.edit", event_id)
    from teampulse.signals import TYPES
    if label not in (*TYPES, "skip"):
        raise AuthError("Unknown label.")
    if not conn.execute("SELECT 1 FROM events WHERE id = ? AND source = 'discord'", (event_id,)).fetchone():
        raise AuthError("That message doesn't exist.")
    with conn:
        conn.execute("INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?)", (event_id, label, actor.display_name, iso(now())))


def undo_label(conn, actor: Actor, event_id: str) -> None:
    require(conn, actor, "data.edit", event_id)
    with conn:
        conn.execute("DELETE FROM labels WHERE event_id = ?", (event_id,))


def remember(conn, actor: Actor, text: str, llm=None) -> dict:
    require(conn, actor, "memory.write", "remember")
    if not 3 <= len(text.strip()) <= 300:
        raise AuthError("Keep a remembered fact between 3 and 300 characters.")
    from teampulse import memory
    rec = memory.remember(conn, text, llm, actor.display_name)
    audit(conn, actor, "memory.add", str(rec["id"]))
    return rec


def forget_memory(conn, actor: Actor, memory_id: int) -> None:
    require(conn, actor, "memory.write", str(memory_id))
    with conn:
        conn.execute("UPDATE memory SET active = 0 WHERE id = ?", (memory_id,))
    audit(conn, actor, "memory.forget", str(memory_id))


def review_own_report(conn, actor: Actor, status: str, note: str = "") -> None:
    """The consent step: a person approves or hides THEIR OWN report. Nobody can do it for someone else."""
    require(conn, actor, "contrib.review_own", "own report")
    if not actor.member_name:
        raise AuthError("Your account isn't linked to a team member yet. Ask your team leader to link it.")
    if status not in ("approved", "disputed"):
        raise AuthError("Unknown choice.")
    from teampulse import contributions
    contributions.set_review(conn, actor.member_name, status, note[:1000])
    audit(conn, actor, "contrib.review", actor.member_name, detail=status)


def run_evaluation(conn, actor: Actor, which: str, llm) -> str:
    require(conn, actor, "eval.run", which)
    from teampulse.eval import blockers as evaluation
    _, report = evaluation.run("db", which, conn=conn, llm=llm)
    audit(conn, actor, "eval.run", which)
    return report


def check_connections(conn, actor: Actor) -> list:
    require(conn, actor, "sources.check", "connections")
    from teampulse import health
    audit(conn, actor, "sources.check")
    return health.check_all()


def test_ai(conn, actor: Actor, llm):
    require(conn, actor, "sources.check", "ai")
    audit(conn, actor, "sources.test_ai")
    return llm.chat([{"role": "user", "content": "Reply with the single word: ready"}])


# ---- questions ---------------------------------------------------------------------------------------

def ask_limit() -> int:
    return int(config.get("TEAMPULSE_ASK_PER_HOUR", "60"))


def ask(conn, actor: Actor, agent, question: str, history: list[dict]) -> dict:
    """Questions are open to every role, but rate-limited per person, and team memory stays leader-only."""
    require(conn, actor, "ask")
    question = question.strip()[:1000]
    recent = conn.execute("SELECT COUNT(*) FROM audit_log WHERE actor_id = ? AND action = 'ask' AND outcome IN ('ok', 'failed') "
                          "AND at >= ?", (actor.id, iso(now() - timedelta(hours=1)))).fetchone()[0]
    if recent >= ask_limit():
        audit(conn, actor, "ask", outcome="denied", detail="rate limit")
        raise RateLimited(f"You've asked {ask_limit()} questions in the last hour. Try again a little later.")
    if not question:
        raise auth.AuthError("Type a question first.")
    try:
        result = agent.ask(question, history=history, allow_memory_write=auth.can(actor, "memory.write"),
                           allowed_specialists=allowed_specialists(conn, actor))
    except Exception as exc:   # search index, database or an unexpected AI reply: log it, don't show internals
        log.exception("Answering a question failed")
        audit(conn, actor, "ask", outcome="failed", detail=type(exc).__name__)
        raise ServiceError("Something went wrong while answering. Please try again; if it keeps happening, tell "
                           "your team leader (the details are in the server log).") from exc
    audit(conn, actor, "ask")   # counted once answered; the question text itself is never logged
    return result


# Which page holds each specialist's information. Asking must not reach what the page would not show.
SPECIALIST_PAGE = {"blockers": "blockers", "discrepancies": "conflicts", "contributions": "contributions",
                   "memory": "memory", "retrieval": "ask"}


def allowed_specialists(conn, actor: Actor) -> list[str]:
    return [name for name, page in SPECIALIST_PAGE.items() if auth.page_allowed(conn, actor, page)]
