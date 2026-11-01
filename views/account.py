"""My account: change your password (required after a one-time password), and sign out everywhere."""
import streamlit as st

import json
import sys
from pathlib import Path

from teampulse import auth, config, mfa, ui, webapp


def two_factor_section(conn, actor):
    st.subheader("Two-factor sign-in")
    if mfa.enabled(conn, actor.id):
        left = mfa.recovery_left(conn, actor.id)
        st.success(f"On. Signing in needs a code from your authenticator app. {left} recovery code"
                   f"{'s' if left != 1 else ''} left.")
        codes = st.session_state.pop("mfa_codes", None)
        if codes:
            st.warning("Save these recovery codes somewhere safe now (each works once if you lose your phone). They "
                       "won't be shown again.")
            st.code("\n".join(codes), language=None)
        if not mfa.required_for(actor.role):
            with st.expander("Turn it off"):
                with st.form("mfa-off", border=False):
                    pw = st.text_input("Password", type="password", autocomplete="current-password")
                    code = st.text_input("Code from your app", max_chars=12)
                    if st.form_submit_button("Turn off two-factor sign-in"):
                        try:
                            mfa.disable(conn, actor, pw, code)
                            st.rerun()
                        except auth.AuthError as exc:
                            st.error(str(exc))
        return
    if actor.needs_mfa:
        st.warning("Team leaders here must use two-factor sign-in. Set it up to continue.")
    else:
        st.caption("Protects your account even if your password leaks. Recommended for team leaders.")
    secret = st.session_state.setdefault("mfa_secret", mfa.new_secret())
    st.markdown("1. In an authenticator app (Google Authenticator, Microsoft Authenticator, Aegis, 1Password), add an "
                "account and type this key:")
    st.code(" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), language=None)
    st.caption("Or open this link on the phone: " + mfa.provisioning_uri(secret, actor.username))
    st.markdown("2. Type the 6-digit code it shows, and your password:")
    with st.form("mfa-on", border=False):
        c1, c2 = st.columns(2)
        code = c1.text_input("Code from your app", max_chars=8, autocomplete="one-time-code")
        pw = c2.text_input("Password", type="password", autocomplete="current-password")
        if st.form_submit_button("Turn on two-factor sign-in", type="primary", icon=":material/verified_user:"):
            try:
                st.session_state["mfa_codes"] = mfa.enable(conn, actor, secret, code, pw)
                st.session_state.pop("mfa_secret", None)
                st.session_state.go_home = actor.needs_mfa
                st.rerun()
            except auth.AuthError as exc:
                st.error(str(exc))


def sessions_section(conn, actor):
    st.subheader("Where you're signed in")
    st.caption("Each browser you're signed in on. If a session suddenly comes from a different browser, TeamPulse "
               "ends it.")
    for s in auth.list_sessions(conn, actor):
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        device = (s["user_agent"] or "Unknown browser")[:90]
        c1.markdown(f"**{ui.e(device)}**" + (" " + ui.pill("this one", "info") if s["current"] else "")
                    + f"<br><span class='tp-meta'>signed in {ui.e(ui.ago(s['created_at']))} · last active "
                    f"{ui.e(ui.ago(s['last_seen']))}</span>", unsafe_allow_html=True)
        if not s["current"] and c2.button("Sign out", key=f"end-{s['id']}"):
            auth.revoke_session(conn, actor, s["id"])
            st.rerun()


def ai_assistant_section(conn, actor):
    """Connect Claude Desktop (or another MCP client) to TeamPulse as yourself."""
    st.subheader("Connect an AI assistant")
    st.caption("Lets Claude Desktop (or any MCP client) answer “what's on my plate?” or “what's blocking #4?” from "
               "TeamPulse, as you, with your permissions. Read-only by default. It runs on this computer; the "
               "assistant asks you before it changes anything. Removing a connection, a password reset or "
               "“sign out of other devices” cuts it off at once.")
    new = st.session_state.pop("new_api_token", None)
    if new:
        st.success("Connection created. Copy this now: it won't be shown again.")
        root = Path(__file__).resolve().parents[1]   # the TeamPulse folder, so it works from any directory
        env = {"TEAMPULSE_TOKEN": new, "TEAMPULSE_DB": str(Path(config.DB_PATH).resolve()), "PYTHONPATH": str(root)}
        snippet = {"mcpServers": {"teampulse": {"command": sys.executable, "args": ["-m", "teampulse.mcp_server"],
                                                "env": env}}}
        st.code(json.dumps(snippet, indent=2), language="json")
        st.caption("Paste it into Claude Desktop → Settings → Developer → Edit config (claude_desktop_config.json), "
                   "then restart Claude Desktop. Keep the token private like a password.")
    with st.form("new-token", clear_on_submit=True, border=False):
        c1, c2 = st.columns([2, 2])
        name = c1.text_input("Name", value="Claude Desktop", max_chars=60)
        scope = c2.radio("Access", list(auth.TOKEN_SCOPES), format_func=auth.TOKEN_SCOPES.get, horizontal=True)
        if st.form_submit_button("Create connection", icon=":material/key:"):
            try:
                st.session_state["new_api_token"] = auth.create_api_token(conn, actor, name, scope)
                st.rerun()
            except auth.AuthError as exc:
                st.error(str(exc))
    for t in auth.list_api_tokens(conn, actor):
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"**{ui.e(t['name'])}** {ui.pill(auth.TOKEN_SCOPES[t['scope']], 'quiet')}<br>"
                    f"<span class='tp-meta'>created {ui.e(ui.ago(t['created_at']))} · last used "
                    f"{ui.e(ui.ago(t['last_used']))} · expires {ui.e(t['expires_at'][:10])}</span>",
                    unsafe_allow_html=True)
        if c2.button("Remove", key=f"revoke-{t['id']}"):
            auth.revoke_api_token(conn, actor, t["id"])
            st.rerun()


agent = webapp.get_agent()
conn = agent.conn
actor = webapp.current_actor(conn)
if actor is None:
    st.stop()

if actor.needs_mfa and not actor.must_change:
    ui.header("Set up two-factor sign-in", "Team leaders here sign in with a password and a code from an "
              "authenticator app. It takes a minute.")
    two_factor_section(conn, actor)
    if st.button("Sign out", icon=":material/logout:"):
        webapp.sign_out(conn)
        st.rerun()
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
        if webapp.auth_enabled() and not webapp.public_demo():
            two_factor_section(conn, actor)
        sessions_section(conn, actor)
        st.caption("Signed in somewhere you shouldn't be? This ends every other session.")
        if st.button("Sign out of other devices", icon=":material/devices:"):
            auth.revoke_all(conn, actor.id, except_token=st.session_state.get(webapp.TOKEN_KEY))
            auth.audit(conn, actor, "auth.revoke_others", actor.username)
            st.success("Signed out of every other device, and removed every AI assistant connection.")

        if webapp.auth_enabled() and not webapp.public_demo():
            ai_assistant_section(conn, actor)
