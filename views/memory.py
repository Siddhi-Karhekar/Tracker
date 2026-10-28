"""Team memory: facts the team asked TeamPulse to remember, re-checked against live data every time."""
import streamlit as st

from teampulse import actions, auth, memory, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("memory")
agent = get_agent()
conn = agent.conn

ui.header("Team memory", "Only facts someone asked TeamPulse to remember. A newer fact on the same topic replaces "
          "the old one, and dates are re-checked against Notion and GitHub every time they're shown.")

can_write = auth.can(actor, "memory.write")
if can_write:
    with st.form("add", clear_on_submit=True, border=True):
        fact = st.text_input("Remember that…", placeholder="Our final review is on 15 Nov", max_chars=300)
        if st.form_submit_button("Remember", icon=":material/push_pin:") and fact.strip():
            try:
                rec = actions.remember(conn, actor, fact, agent.llm)
                st.success(f"Remembered: {rec['fact']}" + (f". Replaces: {'; '.join(rec['replaced'])}" if rec["replaced"] else ""))
            except auth.AuthError as exc:
                st.error(str(exc))
    st.caption("You can also say \"remember that …\" or \"forget …\" on the Ask page.")

items = memory.active(conn)
if not items:
    ui.empty("Nothing remembered yet.", "Add a fact above, like a review date or the team's top priority.")
for m in items:
    check = memory.verify(conn, m)
    tone = {"confirmed": "ok", "differs": "warn", "unchecked": "quiet"}[check["status"]]
    word = {"confirmed": "Matches live data", "differs": "Live data says otherwise", "unchecked": "Nothing to check against"}
    a, b = st.columns([8, 1], vertical_alignment="center")
    link = f" <a href='{ui.url(check['url'])}' target='_blank'>source</a>" if check.get("url") else ""
    who_added = f" by {ui.e(m['created_by'])}" if m["created_by"] else ""
    a.markdown(ui.row(ui.e(m["fact"]), f"{ui.pill(word[check['status']], tone)} {ui.e(check['note'])}{link}", tone,
                      f"Added {ui.ago(m['created_at'])}{who_added}."), unsafe_allow_html=True)
    if can_write and b.button("Forget", key=f"forget-{m['id']}", icon=":material/delete:"):
        actions.forget_memory(conn, actor, m["id"])
        st.rerun()

old = conn.execute("SELECT fact, created_at FROM memory WHERE active = 0 ORDER BY created_at DESC LIMIT 20").fetchall()
if old:
    with st.expander(f"Forgotten or replaced ({len(old)})"):
        for r in old:
            st.markdown(f"- ~~{r['fact']}~~ ({ui.ago(r['created_at'])})")
