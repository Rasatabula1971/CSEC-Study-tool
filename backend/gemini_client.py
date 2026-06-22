# PHASE: build
"""
backend/gemini_client.py
========================
Optional cloud LLM: Google Gemini Flash, used at BUILD time for grading and
upload classification where model quality matters most (CLAUDE.md keeps runtime
local on Ollama). The app runs perfectly with NO Gemini key configured -- this
is an upgrade, never a dependency.

This module is deliberately thin. It exposes only:
  * is_gemini_available() -- a pure PRESENCE check on the key (no network), and
  * gemini_chat()         -- the generation call, which RAISES on any failure.

The Gemini-vs-Ollama decision lives in the ROUTER (llm_router.py), NOT here:
  * chat_for_grading / chat_for_classification FAIL LOUD -- when CLOUD_MODE=1 and
    Gemini is unavailable they raise RuntimeError; they do NOT silently retry on
    Ollama (CLAUDE.md "Optional Cloud Mode": cloud mode must never silently fall
    back).
  * Only build_engine() / chat_for_build silently SELECTS Ollama, gated on
    is_gemini_available().
Because gemini_chat() raises on failure, the router can implement either policy;
preserving that raise-on-failure contract is what keeps both paths correct.

gemini_chat mirrors ollama_client.ollama_chat's signature exactly
(messages, system, schema) so the two are drop-in interchangeable.

Migrated to the client-based google.genai SDK (google-genai >= 1.20); the
deprecated google.generativeai package is no longer used.

Env (from .env): GEMINI_API_KEY (presence is what enables Gemini), GEMINI_MODEL.
"""

import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


def is_gemini_available() -> bool:
    """True if a non-empty GEMINI_API_KEY is configured.

    This is mere PRESENCE -- it's what the router uses to decide whether to ATTEMPT
    Gemini. For an accurate UI indicator, use gemini_key_valid() instead.
    """
    return bool(GEMINI_API_KEY)


def gemini_key_valid() -> bool:
    """True only if a configured key actually WORKS (one lightweight live call).

    Distinct from is_gemini_available (presence): an invalid/expired/wrong-type key
    returns False here. Intended to run once at startup so the UI can honestly show
    whether grading will really reach the cloud or fall back to Ollama.
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

    Returns the model's response text. RAISES on any failure (bad key, network,
    API error) -- the router (chat_for_grading / chat_for_classification /
    chat_for_build) owns whether that becomes a loud error or an Ollama fallback.
    No try/except is added here on purpose.
    """
    # google.genai is client-based: construct a client per call (cheap; the key is
    # read from the module global so tests can monkeypatch it).
    # NOTE: passing api_key= explicitly is LOAD-BEARING. Unlike the old
    # google.generativeai SDK, google.genai also auto-reads GOOGLE_API_KEY from the
    # environment and, if no key is passed, silently PREFERS GOOGLE_API_KEY over
    # GEMINI_API_KEY when both are set. Dropping this explicit arg would make the
    # client authenticate with a stray machine-level GOOGLE_API_KEY instead of our
    # .env GEMINI_API_KEY. Keep it explicit. (The SDK still prints a harmless
    # "Using GOOGLE_API_KEY" warning at construction; the explicit key overrides it.)
    client = genai.Client(api_key=GEMINI_API_KEY)

    config_kwargs = {
        "system_instruction": system,
        "temperature": 0.3,
        "max_output_tokens": 8192,
    }
    if schema:
        config_kwargs["response_mime_type"] = "application/json"
        config_kwargs["response_schema"] = _to_gemini_schema(schema)
    config = types.GenerateContentConfig(**config_kwargs)

    # Map the ollama-shaped messages to google.genai contents. assistant -> "model";
    # everything else -> "user" (same mapping as before). Dict form is accepted by
    # the SDK (ContentDict / PartDict).
    contents = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=config,
    )
    return _response_text(response)
