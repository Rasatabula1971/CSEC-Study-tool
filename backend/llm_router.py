# PHASE: dual
# ROUTING POLICY (v3.0 PDR)
# CLOUD_MODE=0 (default): all inference is Ollama-only. No cloud call, no fallback.
# CLOUD_MODE=1: grading uses Gemini. If Gemini is unreachable the request fails
#   loudly (RuntimeError) -- it must NEVER silently fall back to Ollama.
# The mode is explicit and user-controlled; it is read fresh from the environment
# on every grading call, so it can never be silently violated.
"""
backend/llm_router.py
=====================
Chooses which model serves a grading call (CLAUDE.md "Deterministic vs LLM" +
"Optional Cloud Mode"). Cloud Mode is an explicit, opt-in upgrade -- never a
silent fallback in either direction.

  * chat_for_grading -- grading calls (the syllabus and synthesis graders,
    explain_missed). CLOUD_MODE=0 -> Ollama only. CLOUD_MODE=1 -> Gemini, or a
    loud RuntimeError if Gemini is unreachable (no silent Ollama fallback).
  * chat_local -- ALWAYS Ollama, regardless of CLOUD_MODE. For teach lessons,
    question generation, classify_notes, and every other non-grading call.

Both mirror ollama_chat(messages, system, schema) so callers swap freely.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_chat  # noqa: E402
from gemini_client import gemini_chat, is_gemini_available  # noqa: E402
# Also keep a module handle so the build-phase functions below resolve gemini_chat
# / is_gemini_available at call time -- this is what lets ingestion-script tests
# patch gemini_client.* and have build routing honour it.
import gemini_client  # noqa: E402

logger = logging.getLogger("csec.llm_router")


def chat_for_grading(messages: list, system: str, schema: dict | None = None) -> str:
    """Route a grading call by CLOUD_MODE (read at call time).

    CLOUD_MODE=0 (default): Ollama only. No cloud call, no fallback.
    CLOUD_MODE=1: Gemini if available. If Gemini is unreachable, raise an explicit
    error -- never silently fall back to Ollama.
    """
    if os.getenv("CLOUD_MODE", "0") == "1":
        if not is_gemini_available():
            raise RuntimeError(
                "CLOUD_MODE=1 but Gemini is unreachable. "
                "Check GEMINI_API_KEY or set CLOUD_MODE=0."
            )
        return gemini_chat(messages, system, schema)
    return ollama_chat(messages, system, schema)


def chat_local(messages: list, system: str, schema: dict | None = None) -> str:
    """Always Ollama. For non-grading calls (teach, classify, question gen)."""
    return ollama_chat(messages, system, schema)


def chat_for_lesson_composition(messages: list, system: str,
                                schema: dict | None = None) -> str:
    """Route a lesson-composition call to Anthropic Claude Sonnet.

    ROUTING POLICY (v3.2 architecture decision)
    Lesson composition is build-time only. PHASE: build.

    Routes to Anthropic Claude Sonnet (chosen for prompt adherence
    and structured-output quality at low total volume).
    Costs ~$0.05 per lesson x ~800 lessons across all subjects
    = ~$20 one-time build cost.

    Falls back to Ollama when ANTHROPIC_API_KEY is absent (e.g. on
    student's machine -- but student should never be running
    build-time scripts).

    Does NOT fall back to Gemini. Gemini is reserved for student-
    side classification (free tier, no API key required).
    """
    from anthropic_client import anthropic_chat, is_anthropic_available

    if is_anthropic_available():
        return anthropic_chat(messages, system, schema)

    logger.warning(
        "ANTHROPIC_API_KEY not set. Lesson composition falling "
        "back to Ollama. Quality will be substantially lower. "
        "This is expected on the student's machine but unexpected "
        "on the builder's machine."
    )
    return ollama_chat(messages, system, schema)


def chat_for_classification(messages: list, system: str,
                            schema: dict | None = None) -> str:
    """Route an upload-classification call by CLOUD_MODE (read at call time).

    ROUTING POLICY (v3.1 PDR). Classification is build-time only (PHASE: build):
    it proposes which CSEC objectives a staged file covers and which archive folder
    it belongs in -- never on a student/runtime path.

    CLOUD_MODE=1: prefers Gemini (it knows the POB syllabus far better than the local
    3B model). If Gemini is unreachable, raise an explicit error -- classification
    must NEVER silently degrade to Ollama mid-run.
    CLOUD_MODE=0: warn loudly that quality will be substantially lower, then use
    Ollama. Mirrors ollama_chat(messages, system, schema) so callers swap freely.
    """
    if os.getenv("CLOUD_MODE", "0") == "1":
        if not is_gemini_available():
            raise RuntimeError(
                "CLOUD_MODE=1 but Gemini is unreachable. "
                "Classification needs Gemini; Ollama 3B is not "
                "reliable for this task. Check GEMINI_API_KEY."
            )
        return gemini_chat(messages, system, schema)

    # CLOUD_MODE=0: warn and use Ollama
    logger.warning(
        "Classification running on Ollama with CLOUD_MODE=0. "
        "Quality will be substantially lower than with Gemini. "
        "Set CLOUD_MODE=1 for better results."
    )
    return ollama_chat(messages, system, schema)


# ---------------------------------------------------------------------------
# Build-phase routing (PDR v3.1 Section 2.5)
# ---------------------------------------------------------------------------
# These are called ONLY by ingestion scripts (PHASE: build), never on a runtime
# student path. Cloud may be used at build time to fill gaps the local model
# cannot; the engine used is recorded as mark_points.source_model and every
# generated point is queued in ingest_review_queue for sign-off before going live.
def build_engine() -> str:
    """Which engine build-time generation uses: 'gemini' when CLOUD_MODE=1 and a
    Gemini key is configured, otherwise 'ollama'. Read fresh from the environment.

    Runtime never calls this -- so Gemini is reached at build time only, exactly as
    the offline-first guarantee requires.
    """
    if os.getenv("CLOUD_MODE", "0") == "1" and gemini_client.is_gemini_available():
        return "gemini"
    return "ollama"


def chat_for_build(messages: list, system: str, schema: dict | None = None) -> str:
    """Build-time generation call. Gemini when build_engine() is 'gemini', else
    Ollama. Mirrors ollama_chat(messages, system, schema) so callers swap freely."""
    if build_engine() == "gemini":
        return gemini_client.gemini_chat(messages, system, schema)
    return ollama_chat(messages, system, schema)
