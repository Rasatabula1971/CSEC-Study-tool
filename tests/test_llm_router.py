"""
tests/test_llm_router.py
========================
Unit tests for backend/llm_router.py -- the grading-call router. Both backends
(gemini_chat, ollama_chat) and the availability flag are monkeypatched, so no
network is touched.

Run: pytest tests/test_llm_router.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import llm_router  # noqa: E402


def _boom(*args, **kwargs):
    raise AssertionError("this backend must not be called in this path")


# ---------------------------------------------------------------------------
# chat_for_grading
# ---------------------------------------------------------------------------
def test_chat_for_grading_uses_gemini_when_available(monkeypatch):
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: True)
    monkeypatch.setattr(llm_router, "gemini_chat", lambda m, s, schema=None: "GEMINI")
    monkeypatch.setattr(llm_router, "ollama_chat", _boom)  # must not fall back
    out = llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys")
    assert out == "GEMINI"


def test_chat_for_grading_falls_back_to_ollama_on_gemini_error(monkeypatch):
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: True)

    def gemini_boom(*a, **k):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(llm_router, "gemini_chat", gemini_boom)
    monkeypatch.setattr(llm_router, "ollama_chat", lambda m, s, schema=None: "OLLAMA")
    out = llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys")
    assert out == "OLLAMA"  # silent fallback, no exception surfaced


def test_chat_for_grading_uses_ollama_when_gemini_unavailable(monkeypatch):
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: False)
    monkeypatch.setattr(llm_router, "gemini_chat", _boom)  # must not be called
    monkeypatch.setattr(llm_router, "ollama_chat", lambda m, s, schema=None: "OLLAMA")
    out = llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys")
    assert out == "OLLAMA"


# ---------------------------------------------------------------------------
# chat_local
# ---------------------------------------------------------------------------
def test_chat_local_always_uses_ollama(monkeypatch):
    # Even with Gemini available, chat_local stays on Ollama.
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: True)
    monkeypatch.setattr(llm_router, "gemini_chat", _boom)
    monkeypatch.setattr(llm_router, "ollama_chat", lambda m, s, schema=None: "OLLAMA")
    out = llm_router.chat_local([{"role": "user", "content": "x"}], "sys")
    assert out == "OLLAMA"
