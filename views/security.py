"""Security (leaders, or members granted audit.read): measured, not claimed. Every number is counted from the activity
log, the database or the code when the page opens."""
import pandas as pd
import streamlit as st

from teampulse import queries, security, ui
from teampulse.webapp import get_agent, require_page

actor = require_page("security")
conn = get_agent().conn
ui.header("Security", "How TeamPulse is protecting your team right now. Everything here is measured when the page "
          "opens; nothing is a target or an estimate.")

days = st.sidebar.selectbox("Period", [7, 30, 90], index=1, format_func=lambda d: f"Last {d} days")
m = security.metrics(conn, days)

if not m["auth_on"]:
    st.error("Sign-in is switched off (TEAMPULSE_AUTH=off). Anyone who can open this app has full control. Use this "
             "only on your own computer.")
if m["public_demo"]:
    st.info("Public demo: fictional data, and account changes are switched off.")
if not m["audit"]["ok"]:
    st.error(f"The activity log has been tampered with: {m['audit']['reason']} (entry {m['audit']['first_bad_id']}, "
             f"{m['audit']['at'][:16]}). Compare with your latest backup.")

cov = m["coverage"]
cols = st.columns(5)
cols[0].metric("Actions checked", f"{cov['percent']}%", help=f"{cov['covered']} of {cov['functions']} "
               "functions that act for someone reach a check (read from the code, see the Code tab)")
cols[1].metric("Activity log", "Intact" if m["audit"]["ok"] else "Tampered",
               help=f"{m['audit'].get('checked', 0)} entries re-checked against their hash chain")
cols[2].metric("Refused requests", m["denied"], help="Attempts the permission checks stopped. A refusal means the "
               "controls worked; it isn't a problem score.")
cols[3].metric("Secrets found in the log", m["secrets_in_log"], help="API keys or tokens anywhere in the activity log")
cols[4].metric("Secrets kept from the AI", m["secrets_redacted"] or 0,
               help="Keys or tokens removed from text before it was sent to an AI provider")

access, ai, data, code, log = st.tabs(["Access", "AI safety", "Data and backups", "Code checks", "Activity log"])

with access:
    c = st.columns(4)
    c[0].metric("Leaders with two-factor", f"{m['mfa_leaders']} of {m['leaders']}",
                help=f"{m['mfa_users']} accounts in all use two-factor sign-in. TEAMPULSE_MFA=leaders makes it required.")
    c[1].metric("Signed-in sessions", m["sessions_active"],
                help=f"{m['sessions_moved']} ended because they moved to a different browser")
    c[2].metric("Failed sign-ins", m["failed_signins"], help=f"Also {m['failed_reauth']} failed password confirmations")
    c[3].metric("Accounts locked now", m["locked_now"])
    st.subheader("Extra capabilities granted")
    if m["grants"]:
        st.dataframe(pd.DataFrame(m["grants"]).rename(columns={"display_name": "Member", "permission": "Capability",
                                                               "granted_at": "Since"}), hide_index=True, width="stretch")
    else:
        st.caption("None. Every member has only their own work.")
    st.subheader("AI assistant connections")
    c = st.columns(3)
    c[0].metric("Active", m["tokens_active"])
    c[1].metric("Can update tasks", m["tokens_write"])
    c[2].metric("Expire within 14 days", m["tokens_expiring"])
    st.subheader("Account and permission changes")
    if m["privileged_changes"]:
        st.dataframe(pd.DataFrame(m["privileged_changes"]), hide_index=True, width="stretch")
    else:
        st.caption(f"None in the last {days} days.")
    if m["denied_by_action"]:
        st.subheader("What was refused")
        st.dataframe(pd.DataFrame(m["denied_by_action"]).rename(columns={"action": "Permission or action", "n": "Times"}),
                     hide_index=True, width="stretch")

with ai:
    c = st.columns(5)
    c[0].metric("Guardrail detections", sum(g["n"] for g in m["guardrail_hits"]),
                help="Things the output checks caught or flagged in AI answers")
    c[1].metric("Injection attempts isolated", m["injection_flagged"],
                help="AI requests whose data contained text trying to instruct the AI; it was marked as quoted data")
    c[2].metric("Personal details kept from the AI", m["pii_redacted"] or 0,
                help="Email addresses and phone numbers removed before text was sent to an AI provider")
    c[3].metric("AI assistant calls", m["mcp_calls"])
    c[4].metric("AI assistant calls refused", m["mcp_denied"])
    if m["guardrail_hits"]:
        st.dataframe(pd.DataFrame(m["guardrail_hits"]).rename(columns={"guard": "Guardrail", "n": "Times"}),
                     hide_index=True, width="stretch")
    st.caption("Chat, issues and pages are passed to the AI as data, never as instructions; no AI can change the "
               "team's data by itself; and secrets are removed before any text leaves for an AI provider.")
    st.subheader("Source items that try to instruct the AI")
    st.caption("Messages, issues or pages containing text like “ignore previous instructions”. They stay in the data (a "
               "teammate may be quoting one), but the AI is told to treat them as quotes. Worth a look.")
    if not m["injection_sources"]:
        st.caption("None found.")
    for item in m["injection_sources"]:
        st.markdown(ui.row(f"{ui.e(item['author_name'] or '')}: {ui.e((item['title'] or item['body'] or '')[:120])}",
                           ui.e(", ".join(item["kinds"])), "warn",
                           f"{ui.e(item['source'])} · {ui.e((item['created_at'] or '')[:10])} · "
                           f"<a href='{ui.url(item['url'])}' target='_blank' rel='noopener noreferrer'>open</a>"),
                    unsafe_allow_html=True)

with data:
    c = st.columns(4)
    c[3].metric("Report approvals lapsed", m["approvals_lapsed"],
                help="Approved contribution reports whose evidence changed afterwards; the person is asked to look again")
    c[0].metric("People who left Discord opt-in", m["opted_out"])
    c[1].metric("Their messages still stored", m["opted_out_messages_left"],
                help="Should always be 0: leaving deletes their messages and every copy made from them")
    c[2].metric("Activity log entries checked", m["audit"].get("checked", 0))
    st.subheader("Backups")
    b = m["backup"]
    if not b:
        st.warning("No backup recorded yet. Set TEAMPULSE_BACKUP_DIR for daily backups, or run "
                   "`python -m teampulse.maintenance backup`.")
    else:
        st.markdown(ui.row(f"Last backup {ui.e(ui.ago(b['at']))}: {ui.e(b['file'])}",
                           ("Encrypted" if b["encrypted"] else "Not encrypted") + " · verified by reading it back",
                           "ok" if b["encrypted"] else "warn"), unsafe_allow_html=True)
    if not m["backup_key_set"]:
        st.caption("Backups aren't encrypted. Make a key with `python -m teampulse.maintenance new-key` and set "
                   "TEAMPULSE_BACKUP_KEY.")

with code:
    st.caption("Every function that acts for a signed-in person, read from the source code. 'checked' calls a "
               "permission check (directly or through another checked function); 'self-scoped' can only touch the "
               "caller's own data; 'mcp guard' runs only through the AI-assistant gate. The test suite fails if any "
               "function is unchecked.")
    if cov["unchecked"]:
        st.error("Unchecked: " + ", ".join(f"{r['module']}.{r['function']}" for r in cov["unchecked"]))
    st.dataframe(pd.DataFrame(cov["rows"]), hide_index=True, width="stretch")

with log:
    outcome = st.segmented_control("Show", ["all", "denied", "failed"], default="all")
    rows = queries.activity_log(conn, outcome if outcome in ("denied", "failed") else None, limit=300)
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("Passwords and message text are never logged. The newest 300 entries are shown.")
