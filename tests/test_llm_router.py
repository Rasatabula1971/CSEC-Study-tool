"""
tests/test_llm_router.py
========================
Unit tests for backend/llm_router.py -- the grading-call router.

Optional Cloud Mode (CLAUDE.md): CLOUD_MODE is read fresh on every grading call.
  * CLOUD_MODE=0 (default): chat_for_grading uses Ollama only -- no cloud call,
    no fallback. gemini_chat is never invoked.
  * CLOUD_MODE=1: chat_for_grading uses Gemini when available; if Gemini is
    unreachable it raises RuntimeError -- it must NEVER silently fall back to
    Ollama (and vice versa).
chat_local is ALWAYS Ollama regardless of CLOUD_MODE.

Every backend (ollama_chat, gemini_chat) and the availability predicate are
monkeypatched, and CLOUD_MODE is set explicitly per test, so no network is
touched and the result never depends on the developer's real .env.

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
# chat_for_grading -- CLOUD_MODE routing
# ---------------------------------------------------------------------------
def test_cloud_mode_off_uses_ollama_only(monkeypatch):
    """CLOUD_MODE=0: chat_for_grading calls ollama_chat and NEVER gemini_chat.

    (Renamed from the former test_router_has_no_cloud_path: the router may import
    gemini_chat now, but it must not call it in offline mode.)
    """
    monkeypatch.setenv("CLOUD_MODE", "0")
    monkeypatch.setattr(llm_router, "ollama_chat", lambda m, s, schema=None: "OLLAMA")
    monkeypatch.setattr(llm_router, "gemini_chat", _boom)  # must not be called
    out = llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys")
    assert out == "OLLAMA"


def test_cloud_mode_off_forwards_schema_to_ollama(monkeypatch):
    """The schema arg still reaches the (only) backend unchanged in offline mode."""
    seen = {}

    def fake_ollama(messages, system, schema=None):
        seen["schema"] = schema
        return "OLLAMA"

    monkeypatch.setenv("CLOUD_MODE", "0")
    monkeypatch.setattr(llm_router, "ollama_chat", fake_ollama)
    monkeypatch.setattr(llm_router, "gemini_chat", _boom)
    grading_schema = {"type": "object"}
    llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys",
                                schema=grading_schema)
    assert seen["schema"] is grading_schema


def test_cloud_mode_on_uses_gemini_when_available(monkeypatch):
    """CLOUD_MODE=1 and Gemini available -> gemini_chat called, ollama_chat not."""
    monkeypatch.setenv("CLOUD_MODE", "1")
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: True)
    monkeypatch.setattr(llm_router, "gemini_chat", lambda m, s, schema=None: "GEMINI")
    monkeypatch.setattr(llm_router, "ollama_chat", _boom)  # no fallback to local
    out = llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys")
    assert out == "GEMINI"


def test_cloud_mode_on_unreachable_raises_no_fallback(monkeypatch):
    """CLOUD_MODE=1 and Gemini unreachable -> RuntimeError, NEITHER backend called."""
    monkeypatch.setenv("CLOUD_MODE", "1")
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: False)
    monkeypatch.setattr(llm_router, "gemini_chat", _boom)   # must not be called
    monkeypatch.setattr(llm_router, "ollama_chat", _boom)   # must NOT silently fall back
    with pytest.raises(RuntimeError, match="CLOUD_MODE=1 but Gemini is unreachable"):
        llm_router.chat_for_grading([{"role": "user", "content": "x"}], "sys")


# ---------------------------------------------------------------------------
# chat_local -- always Ollama, regardless of CLOUD_MODE
# ---------------------------------------------------------------------------
def test_chat_local_always_uses_ollama(monkeypatch):
    # Even with CLOUD_MODE=1 and Gemini available, chat_local stays on Ollama.
    monkeypatch.setenv("CLOUD_MODE", "1")
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: True)
    monkeypatch.setattr(llm_router, "gemini_chat", _boom)
    monkeypatch.setattr(llm_router, "ollama_chat", lambda m, s, schema=None: "OLLAMA")
    out = llm_router.chat_local([{"role": "user", "content": "x"}], "sys")
    assert out == "OLLAMA"
