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

import json
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


def test_validate_accepts_math_command_recall_prompts():
    """Mathematics rollout: Application-skill recall prompts open with Math command
    words (Solve/Determine/Compute/Convert/Represent...). These imperative prompts do
    not end in '?', so the Knowledge/Understanding + POA whitelist rejected 11 valid
    Math lessons until the Math command band was added to _RECALL_COMMAND_WORDS."""
    for prompt in [
        "Solve the equation 2x + 3 = 11 for x.",
        "Determine the gradient of the line through (1, 2) and (3, 8).",
        "Compute the H.C.F. of 24 and 36.",
        "Convert 0.75 to a fraction in its simplest form.",
        "Represent the set {1, 2, 3} on a Venn diagram.",
    ]:
        ok, why = il._validate_lesson_quality(_clean_body(350), [prompt])
        assert ok is True and why is None, f"rejected valid Math prompt: {prompt!r} ({why})"


def test_validate_accepts_english_command_recall_prompts():
    """English rollout: recall prompts open with CSEC English command words
    (Extract/Analyse/Present/Formulate/Recognise...). The Math/POA-era whitelist
    rejected 6 valid English lessons until the English command band was added."""
    for prompt in [
        "Extract two pieces of explicit information from the passage above.",
        "Analyse how the writer creates a tense atmosphere in this extract.",
        "Present a counter-argument to the writer's main claim.",
        "Formulate a topic sentence for a paragraph on the dangers of social media.",
        "Recognise the text structure used in the passage and name it.",
    ]:
        ok, why = il._validate_lesson_quality(_clean_body(350), [prompt])
        assert ok is True and why is None, f"rejected valid English prompt: {prompt!r} ({why})"


def test_validate_still_rejects_junk_recall_prompt():
    """The widened whitelist must not let genuine junk through (no '?' / no command word)."""
    ok, why = il._validate_lesson_quality(_clean_body(350), ["multiple-choice"])
    assert ok is False
    assert "not a question or command prompt" in why


# ---------------------------------------------------------------------------
# Tool-use composition: _compose_lesson passes a schema, so the SDK guarantees
# valid JSON (the POB-6.6 unescaped-quote failure class is eliminated).
# ---------------------------------------------------------------------------
def _objective(oid="POB-1.1", cmd='["Define"]', stmt="Define a business"):
    return {"objective_id": oid, "content_stmt": stmt, "command_words": cmd,
            "skill_type": "Knowledge", "objective_num": "1.1", "exam_weight": "P1",
            "section_title": "Nature of Business"}


def test_compose_lesson_passes_tool_use_schema():
    """_compose_lesson must pass a schema (not None) so anthropic_chat uses tool-use."""
    captured = {}

    def fake_chat(messages, system, schema=None):
        captured["schema"] = schema
        return json.dumps({"status": "ok", "subject": "Principles_of_Business",
                           "objective_ref": "1.1", "lesson_text": "body",
                           "active_recall_question": "Define a business?",
                           "sources_used": []})

    out = il._compose_lesson("Principles_of_Business", _objective(), [], fake_chat)
    assert captured["schema"] is not None
    assert captured["schema"] == il.LESSON_OUTPUT_SCHEMA
    assert out["status"] == "ok" and out["lesson_text"] == "body"


def test_compose_lesson_handles_literal_quotes_via_tool_use():
    """POB-6.6 class: a lesson legitimately quoting a phrase. Tool-use returns
    json.dumps(...) with quotes escaped, so parsing succeeds and quotes survive --
    under the old schema=None text path this broke json.loads."""
    lesson = 'Bundling, for example, "two for the price of one." encourages buying more.'

    def fake_chat(messages, system, schema=None):
        # Mirror anthropic_chat's tool-use return: json.dumps of the structured dict.
        return json.dumps({"status": "ok", "subject": "Principles_of_Business",
                           "objective_ref": "6.6", "lesson_text": lesson,
                           "active_recall_question": "Describe two sales methods?",
                           "sources_used": []})

    out = il._compose_lesson(
        "Principles_of_Business",
        _objective("POB-6.6", '["Describe"]', "Describe methods of promoting sales"),
        [], fake_chat)
    assert out is not None and out["status"] == "ok"
    assert out["lesson_text"] == lesson  # literal quotes preserved; parse not broken


def test_compose_lesson_handles_insufficient_source_shape():
    """The dual-shape schema also covers status='insufficient_source' (no lesson_text)."""
    def fake_chat(messages, system, schema=None):
        return json.dumps({"status": "insufficient_source",
                           "subject": "Principles_of_Business", "objective_ref": "1.7",
                           "reason": "source lacks the distinguishing characteristics"})

    out = il._compose_lesson(
        "Principles_of_Business",
        _objective("POB-1.7", '["Distinguish"]', "Distinguish economic systems"),
        [], fake_chat)
    assert out["status"] == "insufficient_source"
    assert "source" in out["reason"].lower()
