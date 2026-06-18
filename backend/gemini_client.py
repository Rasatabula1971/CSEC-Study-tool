# PHASE: build
"""
backend/gemini_client.py
========================
Optional cloud LLM: Google Gemini Flash, used ONLY for grading calls where model
quality matters most (CLAUDE.md keeps everything else local on Ollama). The app
runs perfectly with NO Gemini key configured -- this is an upgrade, never a
dependency. llm_router.chat_for_grading prefers Gemini and falls back to Ollama
silently on any failure.

gemini_chat mirrors ollama_client.ollama_chat's signature exactly
(messages, system, schema) so the two are drop-in interchangeable.

Env (from .env): GEMINI_API_KEY (presence is what enables Gemini), GEMINI_MODEL.
"""

import logging
import os

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


def is_gemini_available() -> bool:
    """True if a non-empty GEMINI_API_KEY is configured.

    This is mere PRESENCE -- it's what the router uses to decide whether to ATTEMPT
    Gemini (a failed attempt falls back to Ollama, so trying is cheap and safe).
    For an accurate UI indicator, use gemini_key_valid() instead.
    """
    return bool(GEMINI_API_KEY)


def gemini_key_valid() -> bool:
    """True only if a configured key actually WORKS (one lightweight live call).

    Distinct from is_gemini_available (presence): an invalid/expired/wrong-type key
    returns False here. Intended to run once at startup so the UI can honestly show
    whether grading will really reach the cloud or silently fall back to Ollama.
    Never raises -- any failure is logged and reported as False.
    """
    if not is_gemini_available():
        return False
    try:
        gemini_chat([{"role": "user", "content": "ping"}], system="Reply with: ok")
        return True
    except Exception as exc:  # noqa: BLE001 -- any failure means "not usable"
        logger.warning("Gemini key validation failed (%s)", exc)
        return False


# Keys Gemini's response_schema (an OpenAPI 3.0 subset) accepts. JSON-Schema
# validation keywords like minimum/maximum/minItems/maxItems are NOT in that subset
# and make the API reject the schema, so they are stripped before the schema is sent.
_GEMINI_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum",
    "items", "properties", "required", "propertyOrdering",
}


def _to_gemini_schema(node):
    """Recursively reduce a JSON-Schema dict to the OpenAPI subset Gemini accepts as a
    response_schema. Drops unsupported validation keywords (minimum/maxItems/…) while
    preserving type/enum/items/properties/required so the structure is still enforced."""
    if not isinstance(node, dict):
        return node
    out = {}
    for key, val in node.items():
        if key not in _GEMINI_SCHEMA_KEYS:
            continue
        if key == "properties" and isinstance(val, dict):
            out[key] = {pk: _to_gemini_schema(pv) for pk, pv in val.items()}
        elif key == "items":
            out[key] = _to_gemini_schema(val)
        else:
            out[key] = val
    return out


def _response_text(response) -> str:
    """Robustly pull text out of a Gemini response. The .text quick-accessor raises on
    gemini-flash-latest (a thinking model) when the candidate carries a thought part,
    so fall back to concatenating every text part. Returns "" if no text part exists."""
    try:
        return response.text
    except Exception:  # noqa: BLE001 -- multi-part / thought response; extract manually
        try:
            parts = response.candidates[0].content.parts
            return "".join((getattr(p, "text", "") or "") for p in parts)
        except (AttributeError, IndexError):
            return ""


def gemini_chat(messages: list, system: str, schema: dict | None = None) -> str:
    """Send a chat request to Gemini. Matches ollama_chat(messages, system, schema).

    The exact same system prompt and user message that would go to Ollama go here
    unchanged. When `schema` is supplied we ask for JSON output (response_mime_type)
    AND pass the schema as a response_schema (reduced to Gemini's OpenAPI subset by
    _to_gemini_schema) -- with json-mime alone gemini-flash-latest emits malformed
    JSON on long objective lists; a response_schema makes the output conform.

    max_output_tokens is generous (8192) because gemini-flash-latest is a thinking
    model: the limit covers thinking + output together, and a tight cap truncates the
    JSON mid-array (finish_reason=MAX_TOKENS) before the answer is written.

    Returns the model's response text. Raises on any failure so the caller
    (chat_for_grading) can fall back to Ollama.
    """
    genai.configure(api_key=GEMINI_API_KEY)

    generation_config = {
        "temperature": 0.3,
        "max_output_tokens": 8192,
    }
    if schema:
        generation_config["response_mime_type"] = "application/json"
        generation_config["response_schema"] = _to_gemini_schema(schema)

    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=system,
        generation_config=generation_config,
    )

    gemini_messages = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        gemini_messages.append({"role": role, "parts": [msg["content"]]})

    response = model.generate_content(gemini_messages)
    return _response_text(response)
