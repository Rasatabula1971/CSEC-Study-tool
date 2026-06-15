"""
backend/llm_router.py
=====================
Chooses which model serves a call (CLAUDE.md "Deterministic vs LLM" + the v3.x
cloud-grading upgrade):

  * chat_for_grading -- grading calls where quality matters (the syllabus and
    synthesis graders, explain_missed). Prefers Gemini Flash when a key is
    configured; on ANY Gemini failure (no key, network, rate limit, bad key) it
    falls back to local Ollama SILENTLY -- a warning is logged, the student never
    sees an error.
  * chat_local -- always Ollama. For teach lessons, question generation,
    classify_notes, and every other non-grading call (quality matters less and
    staying local avoids burning API quota).

Both mirror ollama_chat(messages, system, schema) so callers swap freely.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gemini_client import is_gemini_available, gemini_chat  # noqa: E402
from ollama_client import ollama_chat  # noqa: E402

logger = logging.getLogger(__name__)


def chat_for_grading(messages: list, system: str, schema: dict | None = None) -> str:
    """Route a grading call to Gemini (preferred) or Ollama (silent fallback)."""
    if is_gemini_available():
        try:
            result = gemini_chat(messages, system, schema)
            logger.info("Grading call routed to Gemini")
            return result
        except Exception as exc:  # noqa: BLE001 -- any failure must fall back
            logger.warning("Gemini call failed (%s), falling back to Ollama", exc)

    return ollama_chat(messages, system, schema)


def chat_local(messages: list, system: str, schema: dict | None = None) -> str:
    """Always Ollama. For non-grading calls (teach, classify, question gen)."""
    return ollama_chat(messages, system, schema)
