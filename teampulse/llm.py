"""Free LLM access: Groq, Gemini and Ollama through their OpenAI-compatible APIs, with fallback and caching.

LLM_PROVIDERS sets the order to try, e.g. "groq,gemini,ollama". If one fails (rate limit, no key,
offline), the next is used. Identical prompts are answered from a SQLite cache.

Privacy: use only "ollama" for real team data — free hosted tiers may use prompts for training.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass

from teampulse import config
from teampulse.models import utc_now

log = logging.getLogger("teampulse.llm")

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL", "llama-3.3-70b-versatile"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "GEMINI_MODEL",
               "gemini-2.5-flash"),
    "ollama": (None, None, "OLLAMA_MODEL", "qwen2.5:7b"),
}


@dataclass
class LLMReply:
    text: str
    provider: str
    cached: bool = False


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """Parse JSON even if the model wrapped it in prose or ``` fences."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


class LLM:
    """Usage counters live in `stats` (calls made, cache hits, failures, tokens, seconds) for the evaluation.

    Free tiers limit requests per minute: TEAMPULSE_LLM_MIN_INTERVAL (seconds, default 0) spaces calls out,
    and the client retries rate-limited calls with backoff before falling back to the next provider."""

    _lock = threading.Lock()
    _last_call = 0.0

    def __init__(self, conn: sqlite3.Connection | None = None, providers: list[str] | None = None):
        self.conn = conn
        self.providers = providers or config.get_list("LLM_PROVIDERS") or ["groq", "gemini", "ollama"]
        self._clients: dict = {}
        self.min_interval = float(config.get("TEAMPULSE_LLM_MIN_INTERVAL", "0"))
        self.stats = {"calls": 0, "cached": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}

    def reset_stats(self) -> dict:
        old, self.stats = self.stats, {k: 0 if k != "seconds" else 0.0 for k in self.stats}
        return old

    def _throttle(self):
        if self.min_interval <= 0:
            return
        with LLM._lock:
            wait = LLM._last_call + self.min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            LLM._last_call = time.time()

    def _client(self, name: str):
        if name in self._clients:
            return self._clients[name]
        from openai import OpenAI

        base_url, key_env, model_env, default_model = PROVIDERS[name]
        if name == "ollama":
            base_url = config.get("OLLAMA_URL", "http://localhost:11434").rstrip("/") + "/v1"
            api_key = "ollama"
        else:
            api_key = config.get(key_env)
            if not api_key:
                return None
        client = OpenAI(base_url=base_url, api_key=api_key, max_retries=3, timeout=60)   # retries 429s with backoff
        self._clients[name] = (client, config.get(model_env, default_model))
        return self._clients[name]

    def available(self) -> list[str]:
        return [p for p in self.providers if p in PROVIDERS and self._client(p) is not None]

    def _cache_key(self, messages, json_mode, temperature) -> str:
        blob = json.dumps([messages, json_mode, temperature, self.providers], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def chat(self, messages: list[dict], json_mode: bool = False, temperature: float = 0.0) -> LLMReply:
        key = self._cache_key(messages, json_mode, temperature)
        if self.conn is not None:
            row = self.conn.execute("SELECT response, provider FROM llm_cache WHERE key = ?", (key,)).fetchone()
            if row:
                self.stats["cached"] += 1
                return LLMReply(row[0], row[1], cached=True)

        errors = []
        for name in self.providers:
            setup = self._client(name) if name in PROVIDERS else None
            if setup is None:
                errors.append(f"{name}: not configured")
                continue
            client, model = setup
            try:
                kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
                self._throttle()
                started = time.time()
                response = client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature, **kwargs)
                text = response.choices[0].message.content or ""
                self.stats["calls"] += 1
                self.stats["seconds"] += time.time() - started
                usage = getattr(response, "usage", None)
                if usage is not None:
                    self.stats["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
                    self.stats["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
            except Exception as exc:  # rate limit, network, bad model name... try the next provider
                self.stats["failed"] += 1
                log.warning("%s failed: %s", name, exc)
                errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            if self.conn is not None:
                with self.conn:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO llm_cache (key, response, provider, created_at) VALUES (?, ?, ?, ?)",
                        (key, text, name, utc_now()))
            return LLMReply(text, name)
        raise LLMError("No LLM provider worked. " + " | ".join(errors))
