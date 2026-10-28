"""My account: change your password (required after a one-time password), and sign out everywhere."""
import streamlit as st

from teampulse import auth, ui, webapp

agent = webapp.get_agent()
conn = agent.conn
actor = webapp.current_actor(conn)
if actor is None:
    st.stop()

if actor.must_change:
    ui.header("Choose your own password", "You signed in with a one-time password from your team leader. Pick "
              "one only you know before you continue.")
else:
    ui.header("My account", f"{actor.display_name}, {auth.ROLE_NAME[actor.role].lower()}"
              + (f", linked to {actor.member_name} in the team data." if actor.member_name else "."))
    if not webapp.auth_enabled():
        ui.empty("Sign-in is off.", "Accounts are only used when TEAMPULSE_AUTH is on.")
        st.stop()

left, _ = st.columns([2, 1])
with left:
    with st.form("password", clear_on_submit=True):
        current = st.text_input("One-time password" if actor.must_change else "Current password", type="password",
                                autocomplete="current-password")
        new = st.text_input("New password", type="password", autocomplete="new-password",
                            help=f"At least {auth.MIN_PASSWORD} characters. A short phrase of unrelated words works well.")
        again = st.text_input("Type the new password again", type="password", autocomplete="new-password")
        if st.form_submit_button("Save password", type="primary", icon=":material/key:"):
            if new != again:
                st.error("The two new passwords don't match.")
            else:
                try:
                    auth.change_password(conn, actor, st.session_state.get(webapp.TOKEN_KEY), current, new)
                    if actor.must_change:
                        st.session_state.go_home = True     # first password chosen: continue into the app
                    st.success("Password saved. You've been signed out on every other device.")
                    st.rerun()
                except auth.AuthError as exc:
                    st.error(str(exc))

    if actor.must_change:
        if st.button("Sign out", icon=":material/logout:"):
            webapp.sign_out(conn)
            st.rerun()
    else:
        st.subheader("Devices")
        st.caption("Signed in somewhere you shouldn't be? This ends every other session.")
        if st.button("Sign out of other devices", icon=":material/devices:"):
            auth.revoke_all(conn, actor.id, except_token=st.session_state.get(webapp.TOKEN_KEY))
            auth.audit(conn, actor, "auth.revoke_others", actor.username)
            st.success("Signed out of every other device.")
