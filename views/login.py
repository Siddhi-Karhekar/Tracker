"""Sign in. Also creates the very first team leader, but only with the setup code from the server's settings."""
import streamlit as st

from teampulse import auth, config, ui, webapp

agent = webapp.get_agent()
conn = agent.conn
if webapp.public_demo():
    webapp.banner()

left, _ = st.columns([2, 1])
with left:
    ui.header("Sign in to TeamPulse", "Accounts are created by your team leader. If you don't have one, ask them.")
    reason = st.session_state.pop("signed_out_reason", None)
    if reason:
        st.info(reason)

    if webapp.public_demo():
        st.markdown("**Explore the demo team** with one click. Everything here is fictional.")
        demo = [("neha", "Neha, team leader", ":material/admin_panel_settings:"),
                ("kabir", "Kabir, team member", ":material/person:"),
                ("aman", "Aman, team member", ":material/person:"),
                ("riya", "Riya, team member", ":material/person:")]
        cols = st.columns(2)
        for i, (username, label, icon) in enumerate(demo):
            if cols[i % 2].button(f"Explore as {label}", icon=icon, width="stretch", key=f"demo-{username}"):
                webapp.demo_sign_in(conn, username)
                st.rerun()

    with st.form("sign-in"):
        username = st.text_input("Username", autocomplete="username", max_chars=64)
        password = st.text_input("Password", type="password", autocomplete="current-password", max_chars=128)
        if st.form_submit_button("Sign in", type="primary", icon=":material/login:"):
            if not username.strip() or not password:
                st.error("Enter your username and password.")
            else:
                try:
                    webapp.sign_in(conn, username, password)
                    st.rerun()
                except auth.AuthError as exc:
                    st.error(str(exc))
    st.caption("Forgot your password? Your team leader can issue a new one-time password.")

    has_leader = conn.execute("SELECT 1 FROM users WHERE role = 'leader'").fetchone() is not None
    if not has_leader and not webapp.public_demo():
        st.divider()
        st.subheader("First time setting up?")
        setup_code = config.get("TEAMPULSE_SETUP_CODE")
        if not setup_code:
            st.markdown("Create the team leader account from the computer running TeamPulse:")
            st.code('python -m teampulse.auth create-leader --name "Your Name"', language="bash")
            st.caption("Or set TEAMPULSE_SETUP_CODE in the app's settings to create it here.")
        else:
            with st.form("first-leader"):
                code = st.text_input("Setup code", type="password", help="TEAMPULSE_SETUP_CODE from the app's settings")
                name = st.text_input("Your name", max_chars=60)
                new_user = st.text_input("Choose a username", max_chars=32, help="Lowercase letters, digits, . - _")
                pw1 = st.text_input("Choose a password", type="password", autocomplete="new-password")
                pw2 = st.text_input("Type it again", type="password", autocomplete="new-password")
                if st.form_submit_button("Create team leader account", type="primary"):
                    try:
                        auth.check_setup_code(conn, code, setup_code)   # throttled, minimum length enforced
                        if pw1 != pw2:
                            raise auth.AuthError("The two passwords don't match.")
                        u, _ = auth.create_first_leader(conn, name, new_user or None, pw1, must_change=False)
                        st.success(f"Team leader account {u} created. Sign in above.")
                    except auth.AuthError as exc:
                        st.error(str(exc))
