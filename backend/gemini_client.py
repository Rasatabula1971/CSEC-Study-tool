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


def gemini_chat(messages: list, system: str, schema: dict | None = None) -> str:
    """Send a chat request to Gemini. Matches ollama_chat(messages, system, schema).

    The exact same system prompt and user message that would go to Ollama go here
    unchanged (the prompts carry the JSON-output instructions). When `schema` is
    supplied we only ask Gemini for JSON output (response_mime_type) -- the prompt
    already describes the required shape, so we do not translate the JSON Schema.

    Returns the model's response text. Raises on any failure so the caller
    (chat_for_grading) can fall back to Ollama.
    """
    genai.configure(api_key=GEMINI_API_KEY)

    generation_config = {
        "temperature": 0.3,
        "max_output_tokens": 2048,
    }
    if schema:
        generation_config["response_mime_type"] = "application/json"

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
    return response.text
