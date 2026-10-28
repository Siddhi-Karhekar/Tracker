"""Optional tracing with Arize Phoenix (free, runs locally).

Turn on with TEAMPULSE_TRACING=phoenix in .env, start the Phoenix UI in another terminal:

    pip install arize-phoenix
    phoenix serve                       # UI at http://localhost:6006

Every LangGraph node and every LLM call (prompt, response, tokens, latency) then shows up as a nested
trace. If anything is missing, the app keeps working without tracing.
"""
import logging

from teampulse import config

log = logging.getLogger("teampulse.tracing")
_done = False


def setup() -> bool:
    global _done
    if _done or (config.get("TEAMPULSE_TRACING", "off") or "").lower() != "phoenix":
        return _done
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from phoenix.otel import register

        endpoint = config.get("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006").rstrip("/") + "/v1/traces"
        provider = register(project_name=config.get("PHOENIX_PROJECT", "teampulse"), endpoint=endpoint,
                            batch=True, verbose=False)
        LangChainInstrumentor().instrument(tracer_provider=provider)   # LangGraph nodes
        OpenAIInstrumentor().instrument(tracer_provider=provider)      # Groq / Gemini / Ollama calls
        _done = True
        log.info("Phoenix tracing on → %s", endpoint)
    except Exception as exc:
        log.warning("Tracing not started (%s). Install: pip install arize-phoenix-otel "
                    "openinference-instrumentation-langchain openinference-instrumentation-openai", exc)
    return _done


# ---- LangSmith (hosted; free Developer plan) ----------------------------------------------------------
# LangGraph sends traces to LangSmith by itself when LANGSMITH_TRACING=true and LANGSMITH_API_KEY are set.
# langsmith_wrap() adds the AI calls (prompt, reply, tokens) inside those traces. Traces include team chat and
# issue text, so it stays off unless both variables are set on purpose.

def langsmith_enabled() -> bool:
    return (config.get("LANGSMITH_TRACING", "") or "").lower() in ("true", "1", "yes") and bool(
        config.get("LANGSMITH_API_KEY"))


def langsmith_wrap(client):
    if not langsmith_enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai
        return wrap_openai(client)
    except Exception as exc:   # tracing must never break answering
        log.warning("LangSmith tracing of AI calls not started: %s", exc)
        return client
