"""Label chat: hand-label messages so you can measure how accurately TeamPulse reads chat (aim for ~300)."""
import json

import streamlit as st

from teampulse import actions, auth, ui
from teampulse.signals import TYPES, build_windows, load_messages
from teampulse.webapp import get_agent, require_page

actor = require_page("label")      # leaders only
conn = get_agent().conn
NAME = {"blocker": "Blocker", "help_request": "Help request", "decision": "Decision", "progress": "Progress",
        "noise": "Chatter"}

ui.header("Label chat", "Say what each message is, without looking at what TeamPulse guessed. Your labels are the "
          "answer key for measuring its accuracy, and they stay in your local database.")

total = conn.execute("SELECT COUNT(*) FROM events WHERE source='discord' AND kind='message'").fetchone()[0]
done = conn.execute("SELECT COUNT(*) FROM labels l JOIN events e ON e.id = l.event_id").fetchone()[0]
st.progress(min(done / 300, 1.0), text=f"{done} of 300 labelled ({total} messages available)")

with st.sidebar:
    st.markdown("**Blocker**: stuck, broken, or waiting on something they can't get.  \n"
                "**Help request**: asks someone to do, check, review or answer something.  \n"
                "**Decision**: the team settles a choice.  \n"
                "**Progress**: work done or under way, or who is doing what.  \n"
                "**Chatter**: greetings, jokes, \"ok\", off-topic.  \n\n"
                "Progress and a request in one message counts as a help request.")

row = conn.execute(   # next unlabelled message, in a stable shuffled order across channels
    "SELECT e.* FROM events e LEFT JOIN labels l ON l.event_id = e.id "
    "WHERE e.source='discord' AND e.kind='message' AND l.event_id IS NULL "
    "ORDER BY substr(e.native_id, -3) || e.native_id LIMIT 1").fetchone()

if row is None:
    ui.empty("Everything is labelled.", "Run the evaluation below to see how accurate TeamPulse is.")
else:
    context = conn.execute(
        "SELECT author_name, body FROM events WHERE source='discord' AND channel = ? AND created_at < ? "
        "ORDER BY created_at DESC LIMIT 3", (row["channel"], row["created_at"])).fetchall()[::-1]
    earlier = "".join(f"<p class='tp-meta' style='margin:.15rem 0'><b>{ui.e(c['author_name'])}</b>: {ui.e(c['body'])}</p>"
                      for c in context)
    st.markdown(f"<div class='tp-row'>{earlier}<p class='tp-display' style='font-size:1.35rem;margin:.6rem 0 .2rem 0'>"
                f"{ui.e(row['author_name'])}: {ui.e(row['body'])}</p><p class='tp-meta'>#{ui.e(row['channel'])}, "
                f"{ui.ago(row['created_at'])}</p></div>", unsafe_allow_html=True)
    cols = st.columns(len(TYPES) + 1)
    for col, t in zip(cols, TYPES):
        if col.button(NAME[t], key=f"label-{t}", width="stretch"):
            actions.add_label(conn, actor, row["id"], t)
            st.rerun()
    if cols[-1].button("Skip", icon=":material/skip_next:", width="stretch"):
        actions.add_label(conn, actor, row["id"], "skip")
        st.rerun()

recent = conn.execute("SELECT l.event_id, l.label, e.body FROM labels l JOIN events e ON e.id = l.event_id "
                      "ORDER BY l.labelled_at DESC LIMIT 5").fetchall()
if recent:
    with st.expander("Your last labels"):
        for r in recent:
            c1, c2 = st.columns([6, 1])
            c1.markdown(f"{ui.pill(NAME.get(r['label'], r['label']))} {ui.e(r['body'][:100])}", unsafe_allow_html=True)
            if c2.button("Undo", key=f"undo-{r['event_id']}"):
                actions.undo_label(conn, actor, r["event_id"])
                st.rerun()

st.subheader("Measure accuracy")
c1, c2 = st.columns(2)
which = c1.segmented_control("Compare", ["keyword", "llm", "both"], default="keyword",
                             format_func={"keyword": "Rules", "llm": "AI", "both": "Both"}.get,
                             help="AI uses about one call per 30 messages")
if c1.button("Run evaluation on my labels", icon=":material/analytics:", disabled=done == 0):
    with st.spinner("Classifying your labelled messages…"):
        try:
            report = actions.run_evaluation(conn, actor, which or "keyword", get_agent().llm)
            st.markdown(report)
        except SystemExit as exc:
            st.warning(str(exc))

messages = load_messages(conn)
labels = {r[0]: r[1] for r in conn.execute("SELECT event_id, label FROM labels WHERE label != 'skip'")}
lines = []
for w, window in enumerate(build_windows(messages)):
    start = window[0].created_at
    for m in window:
        if m.id in labels:
            lines.append(json.dumps({
                "conversation": f"w{w}", "channel": m.channel, "id": m.id.split(":", 1)[1], "author": m.author,
                "minutes": int((m.created_at - start).total_seconds() // 60),
                "reply_to": m.reply_to.split(":", 1)[1] if m.reply_to else None, "text": m.text,
                "label": labels[m.id]}, ensure_ascii=False))
c2.download_button("Download labels", "\n".join(lines) + "\n", file_name="chat_labels.jsonl", icon=":material/download:",
                   disabled=not lines, help="Contains message text, so keep it within your team")
