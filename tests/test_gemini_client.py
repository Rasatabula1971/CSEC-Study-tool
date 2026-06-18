"""
tests/test_gemini_client.py
===========================
Unit tests for backend/gemini_client.py. The Google SDK is never contacted: the
availability check reads a module global (monkeypatched), and the failure test
mocks genai so no network call is made.

Run: pytest tests/test_gemini_client.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import gemini_client  # noqa: E402


# ---------------------------------------------------------------------------
# is_gemini_available
# ---------------------------------------------------------------------------
def test_is_gemini_available_false_when_empty(monkeypatch):
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "")
    assert gemini_client.is_gemini_available() is False


def test_is_gemini_available_false_when_none(monkeypatch):
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", None)
    assert gemini_client.is_gemini_available() is False


def test_is_gemini_available_true_when_set(monkeypatch):
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "AIza-test-key")
    assert gemini_client.is_gemini_available() is True


# ---------------------------------------------------------------------------
# gemini_chat
# ---------------------------------------------------------------------------
def test_gemini_chat_raises_on_invalid_key(monkeypatch):
    """A GenerativeModel construction/generation failure propagates (the router
    catches it and falls back to Ollama -- tested in test_llm_router)."""
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "bad-key")
    monkeypatch.setattr(gemini_client.genai, "configure", lambda **k: None)

    def boom(*args, **kwargs):
        raise RuntimeError("API key not valid")

    monkeypatch.setattr(gemini_client.genai, "GenerativeModel", boom)

    with pytest.raises(Exception):
        gemini_client.gemini_chat([{"role": "user", "content": "hi"}], system="sys")


# ---------------------------------------------------------------------------
# gemini_key_valid  (live-validity check used by the UI indicator)
# ---------------------------------------------------------------------------
def test_gemini_key_valid_false_when_no_key(monkeypatch):
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "")
    # gemini_chat must not even be attempted without a key.
    monkeypatch.setattr(gemini_client, "gemini_chat",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert gemini_client.gemini_key_valid() is False


def test_gemini_key_valid_false_when_chat_raises(monkeypatch):
    """A present-but-invalid key: the live ping raises -> reported invalid, no raise."""
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "bad-key")

    def boom(*a, **k):
        raise RuntimeError("API key not valid")

    monkeypatch.setattr(gemini_client, "gemini_chat", boom)
    assert gemini_client.gemini_key_valid() is False


def test_gemini_key_valid_true_when_chat_succeeds(monkeypatch):
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "good-key")
    monkeypatch.setattr(gemini_client, "gemini_chat", lambda *a, **k: "ok")
    assert gemini_client.gemini_key_valid() is True


def test_gemini_chat_returns_text_on_success(monkeypatch):
    """On success gemini_chat returns response.text and maps roles correctly."""
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "good-key")
    monkeypatch.setattr(gemini_client.genai, "configure", lambda **k: None)

    captured = {}

    class FakeResponse:
        text = '{"ok": true}'

    class FakeModel:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def generate_content(self, messages):
            captured["messages"] = messages
            return FakeResponse()

    monkeypatch.setattr(gemini_client.genai, "GenerativeModel", FakeModel)

    out = gemini_client.gemini_chat(
        [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
        system="be terse", schema={"type": "object"},
    )
    assert out == '{"ok": true}'
    # system prompt is passed through unchanged
    assert captured["init"]["system_instruction"] == "be terse"
    # schema present -> JSON mime requested
    assert captured["init"]["generation_config"]["response_mime_type"] == "application/json"
    # assistant role maps to "model"; user stays "user"
    assert [m["role"] for m in captured["messages"]] == ["user", "model"]


# ---------------------------------------------------------------------------
# _to_gemini_schema  (JSON-Schema -> Gemini response_schema subset)
# ---------------------------------------------------------------------------
def test_to_gemini_schema_strips_unsupported_keywords():
    """minimum/maximum/minItems/maxItems are not in Gemini's OpenAPI subset and would
    make the API reject the schema -- they must be dropped while structure survives."""
    src = {
        "type": "object",
        "required": ["objectives"],
        "properties": {
            "folder_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "objectives": {
                "type": "array",
                "maxItems": 15,
                "items": {
                    "type": "object",
                    "required": ["objective_id", "confidence"],
                    "properties": {
                        "objective_id": {"type": "string"},
                        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                },
            },
        },
    }
    out = gemini_client._to_gemini_schema(src)
    # structure preserved
    assert out["type"] == "object"
    assert out["required"] == ["objectives"]
    assert out["properties"]["objectives"]["type"] == "array"
    assert out["properties"]["objectives"]["items"]["properties"]["objective_id"]["type"] == "string"
    # validation keywords stripped at every level
    assert "maxItems" not in out["properties"]["objectives"]
    assert "minimum" not in out["properties"]["folder_confidence"]
    assert "maximum" not in out["properties"]["folder_confidence"]
    conf = out["properties"]["objectives"]["items"]["properties"]["confidence"]
    assert conf == {"type": "integer"}


def test_gemini_chat_passes_response_schema(monkeypatch):
    """A schema arg now produces a response_schema (sanitised) in the generation config."""
    monkeypatch.setattr(gemini_client, "GEMINI_API_KEY", "good-key")
    monkeypatch.setattr(gemini_client.genai, "configure", lambda **k: None)
    captured = {}

    class FakeResponse:
        text = "{}"

    class FakeModel:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def generate_content(self, messages):
            return FakeResponse()

    monkeypatch.setattr(gemini_client.genai, "GenerativeModel", FakeModel)
    gemini_client.gemini_chat(
        [{"role": "user", "content": "q"}], system="s",
        schema={"type": "object", "properties": {"a": {"type": "integer", "minimum": 0}}},
    )
    gc = captured["init"]["generation_config"]
    assert gc["response_mime_type"] == "application/json"
    assert gc["response_schema"] == {"type": "object", "properties": {"a": {"type": "integer"}}}
    assert gc["max_output_tokens"] == 8192   # generous cap for the thinking model


# ---------------------------------------------------------------------------
# _response_text  (robust extraction when .text raises on a thinking model)
# ---------------------------------------------------------------------------
def test_response_text_falls_back_to_parts():
    """When response.text raises (thinking model multi-part response), concatenate the
    text parts instead of losing the answer."""
    class Part:
        def __init__(self, t):
            self.text = t

    class Content:
        parts = [Part('{"ok":'), Part(" true}")]

    class Candidate:
        content = Content()

    class Resp:
        candidates = [Candidate()]

        @property
        def text(self):
            raise ValueError("could not convert part to text")

    assert gemini_client._response_text(Resp()) == '{"ok": true}'
