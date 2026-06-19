"""
tests/test_lesson_prompt_v2.py
==============================
PDR v3.2 lesson-prompt-v2 tests:

  * _normalize_subject_id -- the subject guard for the Lesson Structurer input.
  * chat_for_lesson_composition routing -- Anthropic when a key is set, Ollama
    fallback when not, NEVER Gemini (reserved for student-side classification).
  * _validate_lesson_quality -- the v2 contract: exactly one recall question and a
    300-word floor.

No network: anthropic / ollama / gemini are all monkeypatched. No DB needed.

Run: pytest tests/test_lesson_prompt_v2.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import ingest_lessons as il  # noqa: E402
import llm_router  # noqa: E402
import anthropic_client  # noqa: E402
import gemini_client  # noqa: E402


# ---------------------------------------------------------------------------
# Test 1 -- _normalize_subject_id behaviours
# ---------------------------------------------------------------------------
def test_normalize_subject_id_canonical_passes_through():
    assert il._normalize_subject_id("Principles_of_Business") == "Principles_of_Business"
    assert il._normalize_subject_id("Information_Technology") == "Information_Technology"


def test_normalize_subject_id_accepts_spaces_and_case():
    assert il._normalize_subject_id("principles of business") == "Principles_of_Business"
    assert il._normalize_subject_id("INTEGRATED SCIENCE") == "Integrated_Science"
    assert il._normalize_subject_id("  mathematics  ") == "Mathematics"


def test_normalize_subject_id_rejects_unknown():
    with pytest.raises(ValueError):
        il._normalize_subject_id("Underwater_Basketweaving")
    with pytest.raises(ValueError):
        il._normalize_subject_id("")


# ---------------------------------------------------------------------------
# Test 2/3/4 -- chat_for_lesson_composition routing
# ---------------------------------------------------------------------------
def test_lesson_composition_uses_anthropic_when_key_set(monkeypatch):
    """With ANTHROPIC_API_KEY present, lesson composition calls Anthropic Sonnet."""
    calls = {}

    def fake_anthropic(messages, system, schema=None):
        calls["anthropic"] = (messages, system, schema)
        return '{"status": "ok"}'

    def fail_ollama(*a, **k):
        raise AssertionError("Ollama must not be called when Anthropic is available")

    monkeypatch.setattr(anthropic_client, "is_anthropic_available", lambda: True)
    monkeypatch.setattr(anthropic_client, "anthropic_chat", fake_anthropic)
    monkeypatch.setattr(llm_router, "ollama_chat", fail_ollama)

    out = llm_router.chat_for_lesson_composition(
        [{"role": "user", "content": "x"}], system="s")
    assert out == '{"status": "ok"}'
    assert "anthropic" in calls, "anthropic_chat was invoked"


def test_lesson_composition_falls_back_to_ollama_without_key(monkeypatch):
    """With no ANTHROPIC_API_KEY, it falls back to Ollama (no exception raised)."""
    calls = {}

    def fake_ollama(messages, system, schema=None):
        calls["ollama"] = True
        return "ollama-output"

    def fail_anthropic(*a, **k):
        raise AssertionError("Anthropic must not be called without a key")

    monkeypatch.setattr(anthropic_client, "is_anthropic_available", lambda: False)
    monkeypatch.setattr(anthropic_client, "anthropic_chat", fail_anthropic)
    monkeypatch.setattr(llm_router, "ollama_chat", fake_ollama)

    out = llm_router.chat_for_lesson_composition(
        [{"role": "user", "content": "x"}], system="s")
    assert out == "ollama-output"
    assert calls.get("ollama") is True


def test_lesson_composition_does_not_route_to_gemini(monkeypatch):
    """Lesson composition must NEVER touch Gemini (it is reserved for classification)."""
    gemini_mock = MagicMock(side_effect=AssertionError("Gemini must not be used for lessons"))
    monkeypatch.setattr(gemini_client, "gemini_chat", gemini_mock)
    monkeypatch.setattr(llm_router, "gemini_chat", gemini_mock)

    # Anthropic path (key present): Gemini still untouched.
    monkeypatch.setattr(anthropic_client, "is_anthropic_available", lambda: True)
    monkeypatch.setattr(anthropic_client, "anthropic_chat",
                        lambda m, s, schema=None: '{"status": "ok"}')
    out = llm_router.chat_for_lesson_composition(
        [{"role": "user", "content": "x"}], system="s")
    assert out == '{"status": "ok"}'
    gemini_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5/6/7 -- _validate_lesson_quality: one question + 300-word floor
# ---------------------------------------------------------------------------
def _clean_body(n_words: int) -> str:
    """A boilerplate-free, section-citation-free lesson body of n words."""
    return " ".join(["word"] * n_words)


def test_validate_accepts_single_recall_question():
    ok, why = il._validate_lesson_quality(_clean_body(350), ["What is a business?"])
    assert ok is True and why is None


def test_validate_rejects_lesson_under_300_words():
    ok, why = il._validate_lesson_quality(_clean_body(250), ["What is a business?"])
    assert ok is False
    assert "too short" in why.lower()
    assert "250" in why


def test_validate_accepts_500_word_lesson():
    ok, why = il._validate_lesson_quality(_clean_body(500), ["What is a business?"])
    assert ok is True and why is None
