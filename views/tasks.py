"""Tasks: every task with what each source says about it, everything linked to it, and chat-link corrections."""
import pandas as pd
import streamlit as st

from teampulse import actions, auth, claims, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("tasks")
agent = get_agent()
conn = agent.conn

ui.header("Tasks", "One row per piece of work, merged from GitHub issues and Notion tasks, with what each source "
          "says about it and everything linked to it.")

refreshed = (conn.execute("SELECT value FROM sync_state WHERE key = 'tasks:refreshed_at'").fetchone() or [None])[0]
can_edit = auth.can(actor, "data.edit")
with st.sidebar:
    st.caption(f"Tasks rebuilt {ui.ago(refreshed)}.")
    if auth.can(actor, "data.refresh"):
        use_ai = st.toggle("Use AI to link chat", value=True, help="Off: rules only, fewer chat messages linked")
        if st.button("Rebuild tasks", icon=":material/refresh:", width="stretch"):
            with st.spinner("Reading chat, linking it to tasks, collecting what each source says…"):
                stats = actions.rebuild_tasks(conn, actor, agent.index, agent.llm if use_ai else None)
            st.success(f"{stats['registry']['tasks']} tasks rebuilt.")
if not refreshed:
    with st.spinner("Reading chat, linking it to tasks, collecting what each source says…"):
        actions.first_build(conn, agent.index, agent.llm)

cards = claims.task_cards(conn)
if not cards:
    ui.empty("No tasks yet.", "Tasks come from GitHub issues and Notion task pages. Connect them on the Data sources page.")
    st.stop()


def cell(card, attr, source):
    c = card[attr].get(source)
    if not c:
        return ""
    return ui.STATUS_WORD.get(c["value"], c["value"]) if attr == "status" else c["value"]


def differs(card, attr):
    """A quick visual hint; the Conflicts page applies the real rules."""
    values = {c["value"] for c in card[attr].values()}
    if attr == "status":
        finished = {v in ("done", "dropped") for v in values}
        return len(finished) > 1 or ("blocked" in values and bool(values - {"blocked", "open"}))
    if attr == "owner":
        values = {frozenset(n.strip() for n in v.split(",")) for v in values}
    return len(values) > 1


tabs = st.tabs(["All tasks", "Fix chat links"] if can_edit else ["All tasks"])
overview, links = tabs[0], (tabs[1] if can_edit else None)

with overview:
    rows = []
    for card in cards:
        row = {"Task": (f"#{card['number']} " if card["number"] else "") + card["title"]}
        for attr in ("status", "owner", "deadline"):
            flag = "⚠ " if differs(card, attr) else ""
            for source in ("github", "notion", "chat"):
                value = cell(card, attr, source)
                row[f"{attr.title()} ({ui.SOURCE_LABEL[source]})"] = (flag + value) if value else ""
        row["Did the work"] = ", ".join(f"{w} ({n})" for w, n in card["workers"].most_common())
        row["Chat messages"] = card["links"].get("chat", 0)
        rows.append(row)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"Task": st.column_config.TextColumn(width="medium")})
    st.caption("⚠ marks a column where the sources disagree. The Conflicts page explains each one.")

    for card in cards:
        title = (f"#{card['number']} " if card["number"] else "") + card["title"]
        with st.expander(title):
            facts = []
            for attr in ("status", "owner", "deadline"):
                for source, c in card[attr].items():
                    ev = conn.execute("SELECT url FROM events WHERE id = ?", (c["event_id"],)).fetchone()
                    link = f" <a href='{ui.url(ev['url'])}' target='_blank'>source</a>" if ev and ev["url"] else ""
                    value = ui.status_pill(c["value"]) if attr == "status" else f"<b>{ui.e(c['value'])}</b>"
                    facts.append(f"<tr><td class='tp-meta'>{attr.title()}</td><td>{ui.source_mark(source)}</td>"
                                 f"<td>{value}</td><td class='tp-meta'>{ui.e((c['observed_at'] or '')[:10])}{link}</td></tr>")
            if facts:
                st.markdown(f"<table class='tp-strip'>{''.join(facts)}</table>", unsafe_allow_html=True)
            items = conn.execute(
                "SELECT tl.role, tl.method, e.source, e.title, e.body, e.url, e.author_name, e.updated_at "
                "FROM task_links tl JOIN events e ON e.id = tl.event_id WHERE tl.task_id = ? ORDER BY e.updated_at DESC",
                (card["id"],)).fetchall()
            st.markdown(f"<p class='tp-meta'>{len(items)} linked items</p>", unsafe_allow_html=True)
            st.markdown("".join(
                ui.row(f"{ui.source_mark(it['source'])} {ui.e(it['role'])}: "
                       f"<a href='{ui.url(it['url'])}' target='_blank'>{ui.e((it['title'] or it['body'] or '').splitlines()[0][:90])}</a>",
                       "", "quiet", f"{ui.e(it['author_name'])}, {ui.e((it['updated_at'] or '')[:10])}, linked by {ui.e(it['method'])}")
                for it in items), unsafe_allow_html=True)

if links is not None:
    with links:
        st.markdown("<p class='tp-meta'>Is each chat message attached to the right task? Your corrections always win "
                    "over the AI and become the test data for measuring linking accuracy.</p>", unsafe_allow_html=True)
        task_titles = {c["id"]: ((f"#{c['number']} " if c["number"] else "") + c["title"]) for c in cards}
        options = ["(not about a task)"] + list(task_titles)
        by_primary = {r["primary_event"]: r["id"] for r in conn.execute("SELECT id, primary_event FROM tasks")}
        only_unchecked = st.toggle("Only messages nobody has checked", value=True)
        rows = conn.execute(
            "SELECT s.event_id, s.type, s.task_id, s.link_method, e.body, e.author_name, e.created_at "
            "FROM signals s JOIN events e ON e.id = s.event_id WHERE s.type != 'noise' "
            "ORDER BY e.created_at DESC LIMIT 40").fetchall()
        shown = 0
        for r in rows:
            checked = r["link_method"] == "human"
            if only_unchecked and checked:
                continue
            shown += 1
            current = by_primary.get(r["task_id"])
            with st.container(border=True):
                a, b, c = st.columns([5, 4, 1], vertical_alignment="center")
                a.markdown(f"<b>{ui.e(r['author_name'])}</b> <span class='tp-meta'>{ui.ago(r['created_at'])}</span><br>"
                           f"{ui.e(r['body'][:200])}", unsafe_allow_html=True)
                choice = b.selectbox("Task", options, index=options.index(current) if current in options else 0,
                                     format_func=lambda o: task_titles.get(o, o), key=f"link-{r['event_id']}",
                                     label_visibility="collapsed")
                b.caption(f"Linked by {r['link_method'] or 'nothing yet'}" + (", checked" if checked else ""))
                if c.button("Save", key=f"save-{r['event_id']}"):
                    actions.correct_link(conn, actor, r["event_id"], None if choice == options[0] else choice, agent.index)
                    st.rerun()
        if not shown:
            ui.empty("All caught up.", "Every recent chat link has been checked.")
