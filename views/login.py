"""Sign in. Also creates the very first team leader, but only with the setup code from the server's settings."""
import hmac

import streamlit as st

from teampulse import auth, config, ui, webapp

agent = webapp.get_agent()
conn = agent.conn

left, _ = st.columns([2, 1])
with left:
    ui.header("Sign in to TeamPulse", "Accounts are created by your team leader. If you don't have one, ask them.")
    reason = st.session_state.pop("signed_out_reason", None)
    if reason:
        st.info(reason)

    if webapp.public_demo():
        st.markdown("**Explore the demo team** with one click. Everything here is fictional.")
        c1, c2 = st.columns(2)
        if c1.button("Explore as Neha, team leader", icon=":material/admin_panel_settings:", width="stretch"):
            webapp.demo_sign_in(conn, "neha")
            st.rerun()
        if c2.button("Explore as Kabir, team member", icon=":material/visibility:", width="stretch"):
            webapp.demo_sign_in(conn, "kabir")
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
                    if not hmac.compare_digest(code.encode(), setup_code.encode()):
                        auth.audit(conn, None, "account.create_first_leader", "", "denied", "wrong setup code")
                        st.error("That setup code isn't right.")
                    elif pw1 != pw2:
                        st.error("The two passwords don't match.")
                    else:
                        try:
                            u, _ = auth.create_first_leader(conn, name, new_user or None, pw1, must_change=False)
                            st.success(f"Team leader account {u} created. Sign in above.")
                        except auth.AuthError as exc:
                            st.error(str(exc))
