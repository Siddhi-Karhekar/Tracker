"""Who is acting: the Actor type, the errors every module shares, and the clock. Has no dependencies, so any
module can import it without cycles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


class AuthError(Exception):
    """A sign-in or account problem with a message that is safe to show the user."""


class MFARequired(AuthError):
    """The password was right; now the two-factor code. Carries a short-lived pending token, not a session."""

    def __init__(self, pending: str):
        super().__init__("Enter the 6-digit code from your authenticator app.")
        self.pending = pending


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
    needs_mfa: bool = False             # two-factor is required for them and not set up yet: they get nothing else

    @property
    def is_leader(self) -> bool:
        return self.role == "leader"


LOCAL_ADMIN = Actor("local", "local", "Local admin", "leader", None, False)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")
