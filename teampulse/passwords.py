"""Password hashing (scrypt), checks and generation. Standard library only; no database."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string

SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 10
COMMON = {"password", "password1", "password123", "1234567890", "qwertyuiop", "iloveyou12", "admin12345",
          "letmein123", "welcome123", "teampulse1", "abc1234567"}



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
