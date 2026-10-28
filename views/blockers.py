"""Blockers: what people said they're stuck on or waiting for in chat, linked to the task it's about."""
import streamlit as st

from teampulse import actions, auth, signals, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("blockers")
agent = get_agent()
conn = agent.conn

LABEL = {"blocker": "Blocked", "help_request": "Waiting for help", "decision": "Decision", "progress": "Update",
         "noise": "Chatter"}
TONE = {"blocker": "bad", "help_request": "warn", "decision": "info", "progress": "quiet", "noise": "quiet"}

ui.header("Blockers", "What people said in chat that they're stuck on, waiting for, or decided, with the task "
          "each message is about.")

if auth.can(actor, "data.refresh"):   # leaders only; actions.scan_chat checks again on the server
    with st.sidebar:
        pending = len(signals.pending_targets(conn))
        st.caption(f"{pending} new or edited message{'s' if pending != 1 else ''} not read yet.")
        rules_only = st.toggle("Rules only, no AI", help="Free and offline, but less accurate on Hinglish")
        classifier = signals.KeywordClassifier() if rules_only else signals.LLMClassifier(agent.llm)
        if st.button("Read new messages", icon=":material/mark_chat_read:", disabled=pending == 0, width="stretch"):
            with st.spinner("Reading chat…"):
                agent.index.sync()
                stats = actions.scan_chat(conn, actor, classifier, agent.index)
            st.success(f"Read {stats['classified']} messages; {stats['linked']} linked to a task.")
        if st.button("Read everything again", icon=":material/restart_alt:", width="stretch"):
            with st.spinner("Re-reading all chat…"):
                stats = actions.scan_chat(conn, actor, classifier, agent.index, rescan=True)
            st.success(f"Re-read {stats['classified']} messages.")

c1, c2, c3 = st.columns([4, 1.4, 1.2], vertical_alignment="bottom")
types = c1.pills("Show", ["blocker", "help_request", "decision", "progress"], selection_mode="multi",
                 default=["blocker", "help_request", "decision"], format_func=LABEL.get)
days = c2.number_input("From the last … days", 1, 365, 21)
show_resolved = c3.toggle("Include resolved")

if not types:
    st.stop()
data = agent.blockers.collect_for(types, int(days), limit=200)
items = [i for i in data["items"] if show_resolved or not i["state"].startswith(("resolved", "possibly"))]

if not items:
    total = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if total:
        ui.empty("Nothing here.", "No messages of these kinds in this period.")
    else:
        ui.empty("Chat hasn't been read yet.", "Use Read new messages in the sidebar." if auth.can(actor, "data.refresh")
                 else "Your team leader can read new messages from the sidebar.")
    st.stop()

counts = ", ".join(f"{sum(1 for i in items if i['type'] == t)} {LABEL[t].lower()}" for t in types)
st.markdown(f"<p class='tp-meta'>{ui.e(counts)}.</p>", unsafe_allow_html=True)
html = []
for item in items:
    msg = data["evidence"][item["n"] - 1]
    state = f" {ui.pill(item['state'], 'ok' if item['state'].startswith(('resolved', 'possibly')) else TONE[item['type']])}" \
        if item["state"] else ""
    title = f"{LABEL[item['type']]}{state}"
    body = (f"<b>{ui.e(item['summary'])}</b><br>" if item["summary"] else "") + \
        f"<span class='tp-meta'>“{ui.e(msg['text'][:300])}”</span>"
    if item["task"]:
        task = data["evidence"][item["task"] - 1]
        name = ui.e(task["text"].splitlines()[0][:80])
        link = f"<a href='{ui.url(task['url'])}' target='_blank'>{name}</a>" if task["url"] else name
        owner = f", owner {ui.e(', '.join(task['assignees']))}" if task["assignees"] else ""
        how = f"linked by {item['link_method']}"
        body += f"<br>{ui.source_mark(task['source'])} {link} {ui.status_pill(task['status'])}" \
                f"<span class='tp-meta'>{owner}, {how}</span>"
    else:
        body += "<br><span class='tp-meta'>Not linked to a task yet.</span>"
    discord = f" <a href='{ui.url(msg['url'])}' target='_blank'>Open in Discord</a>" if msg["url"] else ""
    meta = f"{ui.e(item['author'])} in #{ui.e(msg['channel'])}, {ui.e(item['date'])}.{discord}"
    html.append(ui.row(title, body, TONE[item["type"]] if not item["state"].startswith(("resolved", "possibly")) else "ok", meta))
st.markdown("".join(html), unsafe_allow_html=True)
