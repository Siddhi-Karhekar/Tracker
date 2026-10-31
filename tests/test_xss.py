"""Hostile text in GitHub, Notion or Discord must never run as code in TeamPulse's pages.

Anyone on the team (or anyone who can open an issue on a public repo) controls titles, chat messages and links,
so every page is rendered with poisoned data and the HTML it produces is checked.
"""
import re

import pytest

from teampulse import auth, ui
from tests.test_roles_ui import LEADER_PW, page, team  # noqa: F401  (shared fixture)

PAYLOADS = ["<img src=x onerror=alert(1)>", "<script>alert(1)</script>", "<svg onload=alert(1)>"]
BAD_URL = "javascript:alert(1)"
POISON = " ".join(PAYLOADS) + " ![x](https://evil.example/track.png)"
PAGES = ["home", "ask", "blockers", "discrepancies", "tasks", "contributions", "sources", "memory", "label", "team",
         "security"]


@pytest.fixture()
def poisoned(team):  # noqa: F811
    team.execute("UPDATE events SET title = COALESCE(title, '') || ?, body = COALESCE(body, '') || ?, url = ?",
                 (POISON, POISON, BAD_URL))
    team.execute("UPDATE tasks SET title = title || ?, url = ?", (POISON, BAD_URL))
    team.execute("UPDATE discrepancies SET title = COALESCE(title, '') || ?, suggestion = ?", (POISON, POISON))
    team.execute("UPDATE memory SET fact = fact || ?", (POISON,))
    team.commit()
    return team


def html_out(at) -> str:
    """Only Markdown rendered with unsafe_allow_html can carry live HTML; everything else is shown as text."""
    return " ".join(m.value for m in at.markdown if getattr(m, "allow_html", False))


@pytest.mark.parametrize("name", PAGES)
def test_no_page_renders_hostile_text_as_html(poisoned, name):
    token = auth.login(poisoned, "neha", LEADER_PW)[0]
    at = page("app.py" if name == "home" else f"views/{name}.py", token)   # Home links need the router
    live = html_out(at)
    for p in PAYLOADS:
        assert p not in live, f"{name}: raw {p!r} reached the page"
    assert not re.search(r"href=['\"]?\s*javascript:", live, re.I), f"{name}: javascript: link"
    everything = " ".join(m.value for m in at.markdown)
    # An escaped "\](" is shown as text; only an unescaped one becomes a live link or image.
    assert not re.search(r"(?<!\\)\]\((javascript:|https://evil\.example)", everything), f"{name}: live Markdown link"


def test_helpers():
    assert ui.url(BAD_URL) == "#" and ui.url(" JaVaScRiPt:alert(1)") == "#" and ui.url("data:text/html,x") == "#"
    assert ui.url("https://github.com/a?b=1&c='2'") == "https://github.com/a?b=1&amp;c=&#x27;2&#x27;"
    assert "<" not in ui.e(PAYLOADS[0])
    assert "![" not in ui.md_safe("look ![x](https://evil.example/t.png)")
    assert ui.md_text("[a](javascript:x)") == r"\[a\](javascript:x)"
