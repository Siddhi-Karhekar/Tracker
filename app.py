"""TeamPulse web app.   streamlit run app.py

Navigation lives here; each page is a file in views/. What appears depends on who is signed in:
  signed out           → Sign in
  one-time password    → Choose your password (nothing else until it's changed)
  team member          → the pages the leader allows, read-only, plus My account
  team leader          → everything, plus Team & access and Label chat
Each page also checks access itself (webapp.require_page), and every change goes through teampulse.actions,
which checks permissions on the server. Hiding a page here is a convenience, not the protection.
"""
import streamlit as st

from teampulse import auth, ui, webapp

st.set_page_config(page_title="TeamPulse", page_icon=":material/monitor_heart:", layout="wide")
ui.inject_css()

agent = webapp.get_agent()
actor = webapp.current_actor(agent.conn)

if actor is None:
    st.navigation([st.Page("views/login.py", title="Sign in", icon=":material/login:")], position="hidden").run()
    st.stop()
if actor.must_change:
    st.navigation([st.Page("views/account.py", title="Choose your password", icon=":material/key:")],
                  position="hidden").run()
    st.stop()

PAGES = {   # key: (file, title, icon, group)
    "home": ("views/home.py", "Home", ":material/monitor_heart:", "Today"),
    "ask": ("views/ask.py", "Ask", ":material/forum:", "Today"),
    "workspace": ("views/workspace.py", "My work", ":material/person_check:", "Plan"),
    "board": ("views/board.py", "Project board", ":material/view_kanban:", "Plan"),
    "dashboard": ("views/dashboard.py", "Team dashboard", ":material/space_dashboard:", "Plan"),
    "blockers": ("views/blockers.py", "Blockers", ":material/block:", "Team health"),
    "conflicts": ("views/discrepancies.py", "Conflicts", ":material/compare_arrows:", "Team health"),
    "tasks": ("views/tasks.py", "Source tasks", ":material/checklist:", "Team health"),
    "contributions": ("views/contributions.py", "Contributions", ":material/diversity_3:", "People"),
    "team": ("views/team.py", "Team & access", ":material/admin_panel_settings:", "Admin"),
    "security": ("views/security.py", "Security", ":material/shield:", "Admin"),
    "sources": ("views/sources.py", "Data sources", ":material/cable:", "Setup"),
    "memory": ("views/memory.py", "Team memory", ":material/push_pin:", "Setup"),
    "label": ("views/label.py", "Label chat", ":material/label:", "Setup"),
}
groups: dict[str, list] = {}
first = True
for key, (file, title, icon, group) in PAGES.items():
    if auth.page_allowed(agent.conn, actor, key):
        groups.setdefault(group, []).append(st.Page(file, title=title, icon=icon, default=first))
        first = False
groups.setdefault("Setup", []).append(st.Page("views/account.py", title="My account", icon=":material/person:",
                                              default=first))
navigation = st.navigation(groups, expanded=True)
if st.session_state.pop("go_home", False):
    st.switch_page(next(iter(groups.values()))[0])

if webapp.auth_enabled():
    with st.sidebar:
        role = auth.ROLE_NAME[actor.role]
        tone = "info" if actor.is_leader else "quiet"
        st.markdown(f"<div class='tp-meta'>Signed in as</div><b>{ui.e(actor.display_name)}</b> "
                    f"{ui.pill(role, tone)}", unsafe_allow_html=True)
        if not actor.is_leader:
            st.caption("You can update your own tasks and comment; everything else is view-only.")
        if st.button("Sign out", icon=":material/logout:", width="stretch"):
            webapp.sign_out(agent.conn)
            st.rerun()
else:
    st.sidebar.caption("Sign-in is off (TEAMPULSE_AUTH=off). Use this only on your own computer.")

navigation.run()
