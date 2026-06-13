"""
tests/test_ollama_client.py
===========================
Stage 3 tests for the Ollama httpx wrapper. These DO NOT require a running
Ollama server — httpx is monkeypatched, so the request shape and response
handling are verified in isolation.

Run: pytest tests/test_ollama_client.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import ollama_client as oc  # noqa: E402


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, raise_exc=None):
        self._json = json_data or {}
        self.status_code = status_code
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc:
            raise self._raise_exc

    def json(self):
        return self._json


# ---------------------------------------------------------------------------
# health / model listing
# ---------------------------------------------------------------------------

def test_health_true_on_200(monkeypatch):
    monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: FakeResponse(status_code=200))
    assert oc.ollama_health() is True


def test_health_false_on_exception(monkeypatch):
    def boom(*a, **k):
        raise oc.httpx.ConnectError("refused")
    monkeypatch.setattr(oc.httpx, "get", boom)
    assert oc.ollama_health() is False


def test_list_models_parses_names(monkeypatch):
    payload = {"models": [{"name": "llama3.2:3b"}, {"name": "nomic-embed-text:latest"}]}
    monkeypatch.setattr(oc.httpx, "get", lambda *a, **k: FakeResponse(payload))
    assert oc.list_models() == ["llama3.2:3b", "nomic-embed-text:latest"]


def test_list_models_empty_when_down(monkeypatch):
    def boom(*a, **k):
        raise oc.httpx.ConnectError("refused")
    monkeypatch.setattr(oc.httpx, "get", boom)
    assert oc.list_models() == []


# ---------------------------------------------------------------------------
# model name matching (the ':latest' tag quirk)
# ---------------------------------------------------------------------------

def test_name_matches_ignores_latest_tag():
    assert oc._name_matches("nomic-embed-text", ["nomic-embed-text:latest"]) is True
    assert oc._name_matches("llama3.2:3b", ["llama3.2:3b"]) is True


def test_name_matches_false_when_absent():
    assert oc._name_matches("llama3.2:3b", ["mistral:7b"]) is False


def test_verify_models_reports_missing(monkeypatch):
    # server up, but only the chat model pulled
    monkeypatch.setattr(oc, "ollama_health", lambda: True)
    monkeypatch.setattr(oc, "list_models", lambda: [oc.MODEL_CHAT])
    info = oc.verify_models()
    assert info["healthy"] is True
    assert oc.MODEL_EMBED in info["missing"]
    assert oc.MODEL_CHAT not in info["missing"]


def test_verify_models_down(monkeypatch):
    monkeypatch.setattr(oc, "ollama_health", lambda: False)
    info = oc.verify_models()
    assert info["healthy"] is False
    # both required models count as missing when the server is down
    assert oc.MODEL_CHAT in info["missing"] and oc.MODEL_EMBED in info["missing"]


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------

def test_embed_sends_keep_alive_zero_and_returns_vector(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **k):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse({"embedding": [0.1, 0.2, 0.3]})

    monkeypatch.setattr(oc.httpx, "post", fake_post)
    vec = oc.ollama_embed("hello")
    assert vec == [0.1, 0.2, 0.3]
    assert captured["url"].endswith("/api/embeddings")
    assert captured["json"]["keep_alive"] == 0
    assert captured["json"]["prompt"] == "hello"
    assert captured["json"]["model"] == oc.MODEL_EMBED


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

def test_chat_prepends_system_and_returns_content(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, **k):
        captured["json"] = json
        return FakeResponse({"message": {"content": "OK"}})

    monkeypatch.setattr(oc.httpx, "post", fake_post)
    out = oc.ollama_chat([{"role": "user", "content": "hi"}], system="be terse")
    assert out == "OK"
    msgs = captured["json"]["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert captured["json"]["stream"] is False
    assert "format" not in captured["json"]  # no schema → no format key


def test_chat_includes_schema_as_format(monkeypatch):
    captured = {}
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}

    def fake_post(url, json=None, timeout=None, **k):
        captured["json"] = json
        return FakeResponse({"message": {"content": "{}"}})

    monkeypatch.setattr(oc.httpx, "post", fake_post)
    oc.ollama_chat([{"role": "user", "content": "go"}], system="s", schema=schema)
    assert captured["json"]["format"] == schema
