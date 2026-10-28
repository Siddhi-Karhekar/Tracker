"""Ask: chat with the team's GitHub, Notion and Discord. Every claim links to its source."""
import json
import re

import streamlit as st

from teampulse import actions, auth, ui
from teampulse.webapp import demo_question_limit, get_agent, public_demo, require_page

actor = require_page("ask")
agent = get_agent()

SUGGESTIONS = [
    "What is blocking us right now?",
    "kya sab sync me hai?",
    "Who is really doing payments?",
    "What did we finish this week?",
    "login ka kya scene hai?",
    "What did Kabir do this week?",
    "When is our final review?",
]
KIND = {"issue": "issue", "pr": "pull request", "commit": "commit", "issue_comment": "comment", "review": "review",
        "notion_page": "page", "message": "message"}


def linkify(answer: str, evidence: list[dict]) -> str:
    """Citation numbers become links to their source. AI text is untrusted: no embedded images, http(s) only."""
    def repl(match):
        n = int(match.group(1))
        link = ui.url(evidence[n - 1]["url"]) if 0 < n <= len(evidence) else "#"
        return f"[[{n}]]({link})" if link != "#" else match.group(0)
    return re.sub(r"\[(\d+)\]", repl, ui.md_safe(answer))


def source_rows(numbers: list[int], evidence: list[dict]) -> str:
    out = []
    for n in numbers:
        ev = evidence[n - 1]
        status = ev["status"] or ""
        title = f"[{n}] {ui.source_mark(ev['source'])} {ui.e(KIND.get(ev['kind'], ev['kind']))}"
        if ev["url"]:
            title += f" <a href='{ui.url(ev['url'])}' target='_blank'>open</a>"
        meta = ", ".join(ui.e(x) for x in (ev["author"], ev["date"], status) if x)
        text = ui.e((ev["text"] or "").replace("\n", " ")[:220])
        if ev.get("parent"):
            text = f"<span class='tp-meta'>re: {ui.e(ev['parent'][:80])}</span><br>{text}"
        out.append(ui.row(title, text, "quiet", meta))
    return "".join(out)


def render_trace(trace: list[dict], depth: int = 0):
    for step in trace:
        pad = "&nbsp;" * 6 * depth
        st.markdown(f"{pad}**{step['step']}**, {step['ms']} ms", unsafe_allow_html=True)
        details = {k: v for k, v in step.items() if k not in ("step", "ms", "trace")}
        if details:
            st.code(json.dumps(details, indent=2, ensure_ascii=False), language="json")
        if step.get("trace"):
            render_trace(step["trace"], depth + 1)


def render_result(result: dict):
    evidence = result.get("evidence", [])
    st.markdown(linkify(result["answer"], evidence))
    for warning in result.get("warnings", []):
        st.warning(ui.md_safe(warning))
    repaired = sorted({g["guard"].replace("_", " ") for g in result.get("guardrails", []) if g["action"] == "repair"})
    if repaired:
        st.caption("Safety checks removed part of this answer: " + ", ".join(repaired) + ".")
    notes = []
    check = result.get("verification") or {}
    if check.get("statements"):
        ok = sum(1 for x in check["statements"] if x["verdict"] == "supported")
        how = "checked by AI" if check["method"] == "llm" else "rough word-overlap check"
        tone = "ok" if ok == len(check["statements"]) else "warn"
        notes.append(ui.pill(f"{ok} of {len(check['statements'])} statements match their sources", tone)
                     + f" <span class='tp-meta'>{how}</span>")
    route = result.get("route") or {}
    parts = route.get("parts") or []
    if len(parts) > 1:
        notes.append("<span class='tp-meta'>Answered in parts: " + "; ".join(ui.e(p["question"]) for p in parts) + "</span>")
    if route.get("standalone_question") and route["standalone_question"] != result.get("question"):
        notes.append(f"<span class='tp-meta'>Understood as: {ui.e(route['standalone_question'])}</span>")
    if notes:
        st.markdown("<br>".join(notes), unsafe_allow_html=True)
    for mem in result.get("memories", []):
        tone = {"confirmed": "ok", "differs": "warn", "unchecked": "quiet"}[mem["status"]]
        st.markdown(ui.row(f"Team memory: {ui.e(mem['fact'])}", ui.e(mem["note"]), tone), unsafe_allow_html=True)
    cited = result.get("cited", [])
    if cited:
        with st.expander(f"Sources ({len(cited)})", expanded=True):
            st.markdown(source_rows(cited, evidence), unsafe_allow_html=True)
    if evidence:
        with st.expander(f"Everything that was considered ({len(evidence)})"):
            st.markdown(source_rows(list(range(1, len(evidence) + 1)), evidence), unsafe_allow_html=True)
    with st.expander("How this was answered"):
        render_trace(result.get("trace", []))


def use_suggestion():
    st.session_state.pending = st.session_state.get("suggest")
    st.session_state.suggest = None


ui.header("Ask about the project", "Answers come only from your GitHub, Notion and Discord. Every claim links "
          "to the message, issue or page it came from.")

with st.sidebar:
    if st.button("Clear conversation", icon=":material/delete_sweep:", width="stretch"):
        st.session_state.history = []
    available = agent.llm.available()
    st.caption("AI: " + (", ".join(available) if available else "none set up, so answers fall back to evidence lists"))
    if any(p in available for p in ("groq", "gemini")) and not public_demo():
        st.caption("Free hosted AI may keep prompts for training. Use Ollama for real team data.")

st.session_state.setdefault("history", [])
if not st.session_state.history:
    st.pills("Try one of these", SUGGESTIONS, key="suggest", on_change=use_suggestion)

for item in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(ui.md_safe(item["question"]))
    with st.chat_message("assistant"):
        render_result(item["result"])

question = st.chat_input("Ask about progress, blockers, owners or deadlines") or st.session_state.pop("pending", None)
if question and public_demo() and len(st.session_state.history) >= demo_question_limit():
    st.warning(f"This public demo answers {demo_question_limit()} questions per visit to protect its free AI quota. "
               "Run TeamPulse on your own machine for unlimited questions.")
    question = None
if question:
    with st.chat_message("user"):
        st.markdown(ui.md_safe(question))
    with st.chat_message("assistant"):
        with st.spinner("Reading GitHub, Notion and chat…"):
            history = [{"question": h["question"], "answer": h["result"]["answer"]} for h in st.session_state.history]
            try:
                result = actions.ask(agent.conn, actor, agent, question, history)
            except (auth.AuthError, auth.PermissionDenied) as exc:
                result = None
                st.warning(str(exc))
        if result is not None:
            render_result(result)
    if result is not None:
        st.session_state.history.append({"question": question, "result": result})
