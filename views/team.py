"""Team & access (leaders only): create accounts, reset passwords, activate or deactivate, choose what members see."""
import pandas as pd
import streamlit as st

from teampulse import actions, auth, ui, webapp

actor = webapp.require_page("team")
conn = webapp.get_agent().conn

ui.header("Team & access", "Create an account for each teammate, hand over their one-time password privately, "
          "and choose what team members can see. Team members update their own tasks and comment; everything "
          "else is view-only for them.")


def show_credentials():
    creds = st.session_state.get("issued")
    if not creds:
        return
    with st.container(border=True):
        st.markdown(f"**Sign-in details for {ui.e(creds['name'])}**. Share them privately, in person or in a direct "
                    "message, not in a group chat. They'll choose their own password when they first sign in. "
                    "This password won't be shown again.", unsafe_allow_html=True)
        st.code(f"Username: {creds['username']}\nOne-time password: {creds['password']}", language=None)
        if st.button("I've shared it, hide this", icon=":material/visibility_off:"):
            del st.session_state["issued"]
            st.rerun()


locked = not auth.can(actor, "users.manage")   # e.g. the public demo, where every visitor is the same leader
if locked:
    st.info("Account and access changes are switched off in the public demo, so one visitor can't change them "
            "for everyone else. You can still look around; it all works on your own copy.")
show_credentials()
add, people, visibility, activity = st.tabs(["Add a teammate", "Accounts", "What members can see", "Activity"])

with add:
    members = [r[0] for r in conn.execute("SELECT display_name FROM members ORDER BY display_name")]
    taken = {r[0] for r in conn.execute("SELECT member_name FROM users WHERE member_name IS NOT NULL")}
    with st.form("create", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", max_chars=60, placeholder="Riya Mehta")
        username = c2.text_input("Username (optional)", max_chars=32, placeholder="Made from the name if empty")
        c3, c4 = st.columns(2)
        role = c3.radio("Role", ["member", "leader"], format_func=auth.ROLE_NAME.get, horizontal=True,
                        help="Team leaders get full control, including managing accounts.")
        link = c4.selectbox("Who are they in the team data?", ["(not linked)"] + [m for m in members if m not in taken],
                            help="Links the account to their GitHub, Notion and Discord activity, so they can review "
                                 "their own contribution report.")
        if st.form_submit_button("Create account", type="primary", icon=":material/person_add:", disabled=locked):
            try:
                new_user, password = actions.create_user(conn, actor, name, role, username or None,
                                                         None if link == "(not linked)" else link)
                st.session_state.issued = {"name": name.strip(), "username": new_user, "password": password}
                st.rerun()
            except (auth.AuthError, auth.PermissionDenied) as exc:
                st.error(str(exc))

with people:
    users = auth.list_users(conn)
    for u in users:
        with st.container(border=True):
            info, buttons = st.columns([3, 2], vertical_alignment="center")
            status = ("Deactivated", "quiet") if not u["active"] else ("Locked for a few minutes", "warn") if u["locked"] \
                else ("Waiting for first sign-in", "info") if u["must_change"] else ("Active", "ok")
            you = " (you)" if u["id"] == actor.id else ""
            linked = f", linked to {ui.e(u['member_name'])}" if u["member_name"] else ", not linked to team data"
            info.markdown(f"<b>{ui.e(u['display_name'])}</b>{you} {ui.pill(auth.ROLE_NAME[u['role']], 'info' if u['role'] == 'leader' else 'quiet')} "
                          f"{ui.pill(status[0], status[1])}<br><span class='tp-meta'>{ui.e(u['username'])}{linked}. "
                          f"Last signed in {ui.ago(u['last_login'])}.</span>", unsafe_allow_html=True)
            b1, b2 = buttons.columns(2)
            if b1.button("Reset password", key=f"reset-{u['id']}", icon=":material/key:", width="stretch", disabled=locked,
                         help="Issues a new one-time password and signs them out everywhere"):
                try:
                    name_, password = actions.reset_credentials(conn, actor, u["id"])
                    st.session_state.issued = {"name": u["display_name"], "username": name_, "password": password}
                    st.rerun()
                except (auth.AuthError, auth.PermissionDenied) as exc:
                    st.error(str(exc))
            if u["id"] != actor.id:
                label, icon = ("Deactivate", ":material/block:") if u["active"] else ("Reactivate", ":material/check_circle:")
                if b2.button(label, key=f"active-{u['id']}", icon=icon, width="stretch", disabled=locked):
                    try:
                        actions.set_active(conn, actor, u["id"], not u["active"])
                        st.rerun()
                    except (auth.AuthError, auth.PermissionDenied) as exc:
                        st.error(str(exc))

with visibility:
    st.markdown("<p class='tp-meta'>Team members can open these pages, read-only. My work and Project board are always "
                "open to everyone; Label chat, Team dashboard and this page are always for leaders only.</p>", unsafe_allow_html=True)
    current = set(auth.member_pages(conn))
    with st.form("visibility"):
        chosen = [key for key, title in auth.PAGES.items() if st.checkbox(title, value=key in current, key=f"page-{key}")]
        if st.form_submit_button("Save", icon=":material/save:", disabled=not auth.can(actor, "settings.manage")):
            try:
                actions.set_member_pages(conn, actor, chosen)
                st.success("Saved. Members see the change on their next click.")
            except (auth.AuthError, auth.PermissionDenied) as exc:
                st.error(str(exc))

with activity:
    rows = conn.execute("SELECT at, actor_name, action, target, outcome FROM audit_log WHERE action != 'ask' "
                        "ORDER BY id DESC LIMIT 100").fetchall()
    if rows:
        frame = pd.DataFrame([{"When": ui.ago(r["at"]), "Who": r["actor_name"] or "(not signed in)",
                               "What": r["action"], "On": r["target"] or "", "Result": r["outcome"]} for r in rows])
        st.dataframe(frame, hide_index=True, width="stretch")
        st.caption("Sign-ins, account changes, data edits and refused attempts. Passwords and message text are never logged.")
    else:
        ui.empty("No activity yet.", "Sign-ins and changes will appear here.")
