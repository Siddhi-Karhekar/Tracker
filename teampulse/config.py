"""Settings read from environment variables (.env). Each source only needs its own settings."""
import os

from dotenv import load_dotenv

load_dotenv()


def _from_streamlit_secrets(name: str) -> str | None:
    """On Streamlit Community Cloud, settings live in the app's Secrets. Only consulted if a secrets file exists."""
    try:
        from pathlib import Path

        if not any(p.exists() for p in (Path(".streamlit/secrets.toml"), Path.home() / ".streamlit/secrets.toml",
                                         Path("/mount/src"))):
            return None
        import streamlit as st

        value = st.secrets.get(name)
        return None if value is None else str(value)
    except Exception:
        return None


def get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value in (None, ""):
        value = _from_streamlit_secrets(name)
    return value if value not in (None, "") else default


def require(name: str) -> str:
    value = get(name)
    if value is None:
        raise SystemExit(f"Missing setting {name}. Add it to your .env file (see .env.example).")
    return value


def get_list(name: str) -> list[str]:
    raw = get(name, "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_int(name: str, default: int) -> int:
    return int(get(name, str(default)))


DB_PATH = get("TEAMPULSE_DB", "data/teampulse.db")
