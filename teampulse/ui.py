"""TeamPulse design system for Streamlit: fonts, colours and small HTML components.

Palette: sage-white paper, green-black ink, ballpoint-blue accent. Green / amber / red are used ONLY for
meaning (sources agree / something is stale / sources conflict or a blocker is open).
Type: Bricolage Grotesque for headings, Atkinson Hyperlegible for everything else.
Signature element: the sync strip, one row per task with what GitHub, Notion and chat each say.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import streamlit as st

INK, MUTED, LINE, PAPER, CARD = "#14302B", "#5B6F69", "#D5DED9", "#F3F6F4", "#FFFFFF"
ACCENT, AGREE, STALE, CONFLICT = "#2E46C9", "#2F8F6B", "#B7791F", "#C2412D"
TONE = {"ok": AGREE, "warn": STALE, "bad": CONFLICT, "info": ACCENT, "quiet": MUTED}
SEVERITY_TONE = {"high": "bad", "medium": "warn", "low": "quiet"}
SOURCE_LABEL = {"github": "GitHub", "notion": "Notion", "chat": "Chat", "discord": "Chat"}
STATUS_WORD = {"todo": "to do", "in_progress": "in progress", "in_review": "in review", "blocked": "blocked",
               "done": "done", "dropped": "dropped", "open": "open"}
STATUS_TONE = {"done": "ok", "dropped": "quiet", "blocked": "bad", "in_review": "info", "in_progress": "info",
               "todo": "quiet", "open": "quiet"}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,650&display=swap');
html, body, .stApp, .stMarkdown, button, input, textarea, [data-testid="stSidebar"] {{
  font-family: 'Atkinson Hyperlegible', system-ui, sans-serif; }}
h1, h2, h3, .tp-display {{ font-family: 'Bricolage Grotesque', 'Atkinson Hyperlegible', sans-serif !important;
  letter-spacing: -0.01em; color: {INK}; }}
h1 {{ font-weight: 650 !important; }}
h2, h3 {{ font-weight: 600 !important; }}
.block-container, [data-testid="stMainBlockContainer"] {{ padding-top: 2.2rem; max-width: 1180px; }}
[data-testid="stSidebar"] {{ border-right: 1px solid {LINE}; }}
:focus-visible {{ outline: 2px solid {ACCENT} !important; outline-offset: 2px; }}

.tp-head {{ margin: 0 0 1.4rem 0; }}
.tp-head h1 {{ font-size: 2rem; line-height: 1.15; margin: 0 0 .35rem 0; padding: 0; }}
.tp-head p {{ color: {MUTED}; margin: 0; max-width: 68ch; font-size: 1.02rem; }}

.tp-hero {{ font-family: 'Bricolage Grotesque', sans-serif; font-size: 2.05rem; line-height: 1.18; font-weight: 600;
  color: {INK}; max-width: 30ch; margin: .2rem 0 .7rem 0; }}
.tp-hero-calm {{ color: {AGREE}; }}
.tp-sub {{ color: {MUTED}; font-size: 1.02rem; margin-bottom: 1.4rem; }}
.tp-meta {{ color: {MUTED}; font-size: .9rem; }}

.tp-pill {{ display: inline-block; padding: .08rem .55rem; border-radius: 999px; font-size: .82rem; line-height: 1.5;
  border: 1px solid currentColor; white-space: nowrap; }}
.tp-pill-fill {{ color: #fff !important; border-color: transparent; }}
.tp-src {{ display: inline-flex; align-items: center; gap: .3rem; font-size: .82rem; color: {MUTED}; }}
.tp-src b {{ display: inline-grid; place-items: center; width: 1.15rem; height: 1.15rem; border: 1.5px solid {MUTED};
  border-radius: 4px; font-size: .68rem; color: {INK}; }}

.tp-row {{ border-left: 4px solid {LINE}; background: {CARD}; padding: .7rem .9rem .7rem 1rem; margin-bottom: .55rem;
  border-radius: 0 6px 6px 0; }}
.tp-row p {{ margin: .15rem 0 0 0; }}
.tp-row .tp-title {{ font-weight: 700; color: {INK}; }}
.tp-row a {{ color: {ACCENT}; text-decoration: none; }}
.tp-row a:hover {{ text-decoration: underline; }}

.tp-strip {{ width: 100%; border-collapse: separate; border-spacing: 0 .35rem; font-size: .93rem; }}
.tp-strip th {{ text-align: left; color: {MUTED}; font-weight: 400; font-size: .85rem; padding: 0 .6rem; }}
.tp-strip td {{ background: {CARD}; padding: .55rem .6rem; vertical-align: middle; }}
.tp-strip td:first-child {{ border-left: 4px solid {LINE}; border-radius: 6px 0 0 6px; max-width: 280px; }}
.tp-strip td:last-child {{ border-radius: 0 6px 6px 0; }}
.tp-strip .tp-task a {{ color: {INK}; font-weight: 700; text-decoration: none; }}
.tp-strip .tp-task a:hover {{ color: {ACCENT}; }}
.tp-strip .tp-none {{ color: {LINE}; }}
.tp-legend {{ color: {MUTED}; font-size: .85rem; margin-top: .3rem; }}
.tp-legend span {{ display: inline-block; width: .7rem; height: .7rem; border-radius: 2px; margin: 0 .3rem 0 .9rem;
  vertical-align: -1px; }}

.tp-source-card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 8px; padding: .9rem 1rem; height: 100%; }}
.tp-source-card h4 {{ font-family: 'Bricolage Grotesque', sans-serif; margin: 0 0 .25rem 0; font-size: 1.1rem; }}
.tp-dot {{ display: inline-block; width: .6rem; height: .6rem; border-radius: 50%; margin-right: .35rem; }}
.tp-empty {{ border: 1.5px dashed {LINE}; border-radius: 8px; padding: 1.2rem 1.3rem; color: {MUTED}; }}
.tp-empty b {{ color: {INK}; }}
@media (max-width: 640px) {{ .tp-hero {{ font-size: 1.55rem; }} .tp-strip {{ font-size: .85rem; }} }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
"""


# Streamlit still reads Markdown (links, images, bold, LaTeX, :colour[...] directives) inside HTML we render,
# so those characters become HTML entities too. The browser shows the same text; Markdown sees nothing to parse.
# One pass, so an entity made for one character is never escaped again.
_ESCAPES = str.maketrans({**{c: f"&#{ord(c)};" for c in "[]()!*_`~$:\\#|"},
                          "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;"})


def e(text) -> str:
    """Escape text for HTML and Markdown. Every value from GitHub, Notion, Discord or a user goes through this."""
    return str(text or "").translate(_ESCAPES)


def url(link) -> str:
    """Only http(s) links, escaped for an attribute. Anything else (javascript:, data:) becomes a dead link."""
    link = str(link or "").strip()
    return html.escape(link) if link.lower().startswith(("https://", "http://")) else "#"


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#!|])")


_MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
_MD_AUTOLINK = re.compile(r"<((?:https?|mailto|javascript|data):[^>\s]*)>", re.I)


def md_safe(text) -> str:
    """For untrusted text shown as Markdown (AI answers, which can repeat what's in chat or an issue).

    Embedded images are dropped (they load from any server the text names). Links the text itself makes are
    shown as "label (address)", so a message can't disguise a phishing link as a friendly label; the
    citation links TeamPulse adds afterwards are the only real links in an answer."""
    text = _MD_IMAGE.sub(r"\1", str(text or ""))
    text = _MD_LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(2) else m.group(1), text)
    return _MD_AUTOLINK.sub(r"\1", text)


def md_text(text) -> str:
    """Untrusted text placed inside our own Markdown (e.g. link labels): every Markdown character escaped."""
    return _MD_SPECIAL.sub(r"\\\1", str(text or "").replace("\n", " "))


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def header(title: str, lead: str = "") -> None:
    st.markdown(f"<div class='tp-head'><h1>{e(title)}</h1>{f'<p>{e(lead)}</p>' if lead else ''}</div>",
                unsafe_allow_html=True)


def pill(text: str, tone: str = "quiet", filled: bool = False) -> str:
    color = TONE.get(tone, MUTED)
    style = f"background:{color};" if filled else f"color:{color};"
    return f"<span class='tp-pill{' tp-pill-fill' if filled else ''}' style='{style}'>{e(text)}</span>"


def status_pill(status: str | None) -> str:
    if not status:
        return "<span class='tp-none'>—</span>"
    return pill(STATUS_WORD.get(status, status), STATUS_TONE.get(status, "quiet"))


def source_mark(source: str) -> str:
    label = SOURCE_LABEL.get(source, source.title())
    return f"<span class='tp-src'><b>{e(label[0])}</b>{e(label)}</span>"


def row(title: str, body: str = "", tone: str = "quiet", meta: str = "") -> str:
    """A left-ruled row: the rule colour carries severity, nothing else does."""
    color = TONE.get(tone, LINE) if tone != "quiet" else LINE
    return (f"<div class='tp-row' style='border-left-color:{color}'><div class='tp-title'>{title}</div>"
            + (f"<p>{body}</p>" if body else "") + (f"<p class='tp-meta'>{meta}</p>" if meta else "") + "</div>")


def empty(title: str, body: str) -> None:
    st.markdown(f"<div class='tp-empty'><b>{e(title)}</b><br>{e(body)}</div>", unsafe_allow_html=True)


def ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return iso[:16]
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"
