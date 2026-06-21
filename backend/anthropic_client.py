# PHASE: build
"""
backend/anthropic_client.py
===========================
Build-time cloud LLM: Anthropic Claude Sonnet, used ONLY for lesson composition
(an architectural decision in PDR v3.2). Lessons are pre-generated once per
objective on the BUILDER's machine, where the builder pays Anthropic; the
student's machine never reaches this module (runtime is Ollama-only, offline).

anthropic_chat mirrors ollama_client.ollama_chat's signature exactly
(messages, system, schema) so the two are drop-in interchangeable. When `schema`
is supplied, Anthropic's tool-use pattern forces structured JSON output.

Env (from .env): ANTHROPIC_API_KEY (presence is what enables Anthropic),
ANTHROPIC_MODEL.
"""

import json
import logging
import os

from anthropic import Anthropic, APIError

# .strip(): a pasted key often carries a trailing newline/space, which makes the
# HTTP Authorization header illegal (httpx LocalProtocolError). Strip defensively so
# a stray newline in .env never breaks the build run.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

logger = logging.getLogger(__name__)

# Token usage from the most recent anthropic_chat call. The Anthropic response
# already carries resp.usage (what Anthropic bills on); we were discarding it.
# Stored here as a side channel so the caller (ingest_lessons) can surface it on
# its per-objective summary line WITHOUT changing anthropic_chat's str return type
# (it must stay a drop-in for ollama_chat). Reset at the start of every call so a
# failed/Ollama-fallback call never reports a stale previous count.
LAST_USAGE = {"input_tokens": None, "output_tokens": None}


def _record_usage(resp) -> None:
    """Capture resp.usage into LAST_USAGE and echo one line to stdout per call.

    Best-effort: a response object without .usage must never break the call.
    """
    try:
        LAST_USAGE["input_tokens"] = resp.usage.input_tokens
        LAST_USAGE["output_tokens"] = resp.usage.output_tokens
        print(f"[tokens] in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
    except Exception:
        LAST_USAGE["input_tokens"] = None
        LAST_USAGE["output_tokens"] = None


def is_anthropic_available() -> bool:
    """True if a non-empty ANTHROPIC_API_KEY is configured."""
    return bool(ANTHROPIC_API_KEY)


def anthropic_chat(messages: list[dict], system: str, schema: dict | None = None) -> str:
    """Call Claude Sonnet via the Anthropic API. Mirrors ollama_chat(messages, system, schema).

    messages: [{"role": "user", "content": "..."}]
    system: system prompt string
    schema: optional JSON schema for structured output (uses Anthropic tool-use)

    Returns the model's text response (the JSON string of the tool input when a
    schema is supplied). Raises RuntimeError on a missing key or any API error so
    the caller (chat_for_lesson_composition) can decide whether to fall back.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured in .env")

    # Reset per call: if create() below raises, usage stays None rather than
    # carrying the previous call's count.
    LAST_USAGE["input_tokens"] = None
    LAST_USAGE["output_tokens"] = None

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # For structured output, wrap the schema as a tool the model MUST call, then
    # return the tool_use input as a JSON string (drop-in for ollama_chat's
    # schema-constrained JSON response).
    if schema:
        tool = {
            "name": "submit_lesson",
            "description": "Submit the composed lesson",
            "input_schema": schema,
        }
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                system=system,
                messages=messages,
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_lesson"},
            )
            _record_usage(resp)
            for block in resp.content:
                if block.type == "tool_use":
                    return json.dumps(block.input)
            raise RuntimeError("Anthropic response missing tool_use block")
        except APIError as e:
            raise RuntimeError(f"Anthropic API error: {e}")
    else:
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                system=system,
                messages=messages,
            )
            _record_usage(resp)
            return resp.content[0].text
        except APIError as e:
            raise RuntimeError(f"Anthropic API error: {e}")
