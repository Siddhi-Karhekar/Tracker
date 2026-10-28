"""Home: the one thing that most needs attention, what else does, and the sync strip of every task."""
import pandas as pd
import streamlit as st

from teampulse import actions, auth, claims, discrepancies as disc, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("home")
agent = get_agent()
conn = agent.conn

refreshed = (conn.execute("SELECT value FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone() or [None])[0]
has_events = conn.execute("SELECT 1 FROM events LIMIT 1").fetchone() is not None
if has_events and not refreshed:
    with st.spinner("First look at your data: reading chat, linking tasks, comparing sources…"):
        actions.first_build(conn, agent.index, agent.llm)
    refreshed = (conn.execute("SELECT value FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone() or [None])[0]

if not has_events:
    ui.header("TeamPulse", "See where your team's GitHub, Notion and Discord agree, and where they don't.")
    ui.empty("No project data yet.",
             "Connect GitHub, Notion and Discord on the Data sources page, or load the demo team with "
             "`python -m teampulse.seed_demo` and set TEAMPULSE_DB=data/demo.db.")
    if auth.page_allowed(conn, actor, "sources"):
        st.page_link("views/sources.py", label="Set up data sources", icon=":material/cable:")
    st.stop()

# ---- what needs attention -------------------------------------------------------------------------
conflicts = disc.open_items(conn)
blockers = conn.execute(
    "SELECT s.type, s.summary, e.body, e.author_name, e.created_at, e.url, t.title AS task, t.github_number "
    "FROM signals s JOIN events e ON e.id = s.event_id LEFT JOIN task_links tl ON tl.event_id = s.event_id "
    "LEFT JOIN tasks t ON t.id = tl.task_id WHERE s.type IN ('blocker','help_request') AND s.resolved = 0 "
    "AND e.created_at >= date('now', '-21 days') ORDER BY s.type, e.created_at DESC").fetchall()
cards = claims.task_cards(conn)
done = sum(1 for c in cards if c["status"].get("github", {}).get("value") == "done")


def strip_number(title: str) -> str:
    return title.split(" ", 1)[1] if title.startswith("#") and " " in title else title


top = next((d for d in conflicts if d["severity"] == "high"), None)
others = len(conflicts) - (1 if top else 0)
n_block = sum(1 for b in blockers if b["type"] == "blocker")
n_req = len(blockers) - n_block

st.markdown(f"<p class='tp-meta'>Refreshed {ui.ago(refreshed)}. {done} of {len(cards)} tasks done on GitHub.</p>",
            unsafe_allow_html=True)
if top:
    st.markdown(f"<div class='tp-hero'>{ui.e(strip_number(top['title']))}.</div>", unsafe_allow_html=True)
elif conflicts or blockers:
    lead = f"{len(conflicts)} source conflict{'s' if len(conflicts) != 1 else ''} and {n_block} open blocker{'s' if n_block != 1 else ''} need a look."
    st.markdown(f"<div class='tp-hero'>{ui.e(lead)}</div>", unsafe_allow_html=True)
else:
    st.markdown("<div class='tp-hero tp-hero-calm'>GitHub, Notion and chat agree on every task.</div>",
                unsafe_allow_html=True)
parts = []
if top and others:
    parts.append(f"{others} more source conflict{'s' if others != 1 else ''}")
if n_block:
    parts.append(f"{n_block} open blocker{'s' if n_block != 1 else ''}")
if n_req:
    parts.append(f"{n_req} unanswered request{'s' if n_req != 1 else ''}")
if parts and top:
    st.markdown(f"<div class='tp-sub'>Also: {', '.join(parts)}.</div>", unsafe_allow_html=True)

# ---- attention list + activity ------------------------------------------------------------------------
left, right = st.columns([3, 2], gap="large")
with left:
    st.subheader("Needs attention")
    items = []
    for d in conflicts:
        if d["severity"] == "low" and len(items) >= 3:
            continue
        sides = " &nbsp; ".join(f"{ui.source_mark(s['source'])} {ui.pill(ui.STATUS_WORD.get(s['value'], s['value'])[:40])}"
                                for s in d["sides"][:3])
        items.append((0 if d["severity"] == "high" else 2 if d["severity"] == "medium" else 3,
                      ui.row(ui.e(strip_number(d["title"])), sides, ui.SEVERITY_TONE[d["severity"]], ui.e(d["suggestion"]))))
    for b in blockers:
        what = "Blocked" if b["type"] == "blocker" else "Waiting for help"
        task = f" on {ui.e(b['task'])}" if b["task"] else ""
        link = f" <a href='{ui.url(b['url'])}' target='_blank'>open in Discord</a>" if b["url"] else ""
        items.append((1 if b["type"] == "blocker" else 2,
                      ui.row(f"{what}{task}", ui.e(b["summary"] or b["body"]),
                             "bad" if b["type"] == "blocker" else "warn",
                             f"{ui.e(b['author_name'])}, {ui.ago(b['created_at'])}.{link}")))
    items.sort(key=lambda x: x[0])
    if items:
        st.markdown("".join(html for _, html in items[:7]), unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        if auth.page_allowed(conn, actor, "conflicts"):
            c1.page_link("views/discrepancies.py", label=f"All conflicts ({len(conflicts)})", icon=":material/compare_arrows:")
        if auth.page_allowed(conn, actor, "blockers"):
            c2.page_link("views/blockers.py", label=f"All blockers ({len(blockers)})", icon=":material/block:")
    else:
        ui.empty("Nothing needs attention.", "No conflicts between sources and no open blockers in the last 3 weeks.")

with right:
    st.subheader("Activity, last 14 days")
    rows = conn.execute(
        "SELECT substr(created_at, 1, 10) AS day, source, COUNT(*) AS n FROM events "
        "WHERE created_at >= date('now', '-14 days') GROUP BY day, source").fetchall()
    if rows:
        frame = pd.DataFrame([dict(r) for r in rows]).replace({"source": {"github": "GitHub", "notion": "Notion",
                                                                           "discord": "Chat"}})
        frame = frame.rename(columns={"day": "Day", "source": "Source", "n": "Items"})
        st.bar_chart(frame, x="Day", y="Items", color="Source", height=230,
                     width="stretch")
    else:
        st.caption("No activity in the last 14 days.")
    if auth.page_allowed(conn, actor, "ask"):
        with st.form("quick-ask", border=False):
            q = st.text_input("Ask a question", placeholder="kya atka hua hai?")
            if st.form_submit_button("Ask", icon=":material/forum:") and q.strip():
                st.session_state.pending = q.strip()
                st.switch_page("views/ask.py")

# ---- sync strip -------------------------------------------------------------------------------------
st.subheader("Every task, three sources")
st.markdown("<p class='tp-meta'>What GitHub, Notion and chat each say. The edge colour shows the most serious "
            "conflict on that task.</p>", unsafe_allow_html=True)
worst = {}
for d in conflicts:
    if d["task_id"]:
        rank = {"high": 0, "medium": 1, "low": 2}[d["severity"]]
        worst[d["task_id"]] = min(worst.get(d["task_id"], 9), rank)


def cell_owner(card) -> str:
    owners = card["owner"]
    base = (owners.get("notion") or owners.get("github") or {}).get("value", "")
    chat = owners.get("chat", {}).get("value")
    if chat and chat.split(",")[0].strip().lower() not in base.lower():
        return f"{ui.e(base)} {ui.pill('chat says ' + chat, 'warn')}" if base else ui.e(chat)
    return ui.e(base) or "<span class='tp-none'>—</span>"


def cell_due(card) -> str:
    dates = {s: c["value"] for s, c in card["deadline"].items()}
    if not dates:
        return "<span class='tp-none'>—</span>"
    if len(set(dates.values())) > 1:
        return " ".join(ui.pill(f"{ui.SOURCE_LABEL[s]} {d[5:]}", "warn") for s, d in dates.items())
    return ui.e(next(iter(dates.values())))


edge = {0: ui.CONFLICT, 1: ui.STALE, 2: ui.LINE}
ordered = sorted(cards, key=lambda c: (worst.get(c["id"], 9), c["number"] or 10**6))
body = []
for card in ordered:
    name = (f"#{card['number']} " if card["number"] else "") + card["title"]
    link = f"<a href='{ui.url(card['url'])}' target='_blank'>{ui.e(name)}</a>" if card["url"] else ui.e(name)
    color = edge.get(worst.get(card["id"]), ui.LINE)
    cells = "".join(f"<td>{ui.status_pill(card['status'].get(s, {}).get('value'))}</td>" for s in ("github", "notion", "chat"))
    body.append(f"<tr><td class='tp-task' style='border-left-color:{color}'>{link}</td>{cells}"
                f"<td>{cell_owner(card)}</td><td>{cell_due(card)}</td></tr>")
st.markdown("<table class='tp-strip'><thead><tr><th>Task</th><th>GitHub</th><th>Notion</th><th>Chat</th>"
            f"<th>Owner</th><th>Due</th></tr></thead><tbody>{''.join(body)}</tbody></table>"
            f"<div class='tp-legend'>Edge:<span style='background:{ui.CONFLICT}'></span>serious conflict"
            f"<span style='background:{ui.STALE}'></span>conflict<span style='background:{ui.LINE}'></span>none or minor</div>",
            unsafe_allow_html=True)

if auth.can(actor, "data.refresh"):
    with st.sidebar:
        if st.button("Refresh now", icon=":material/refresh:", width="stretch",
                     help="Read new chat, relink tasks and compare sources again"):
            with st.spinner("Refreshing…"):
                actions.refresh_all(conn, actor, agent.index, agent.llm)
            st.rerun()
