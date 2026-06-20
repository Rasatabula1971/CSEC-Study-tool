"""
tests/test_shared_tokens.py
===========================
UI overhaul session 3, Task 1: the dark/blue palette tokens were moved into
shared.css, and welcome.html / first_launch.html now reference shared.css instead
of redefining them locally (so Study, Quiz, Welcome and first-launch all draw from
ONE palette definition).

Run: pytest tests/test_shared_tokens.py -v
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "backend" / "static"

SHARED = (STATIC / "shared.css").read_text(encoding="utf-8")
WELCOME = (STATIC / "welcome.html").read_text(encoding="utf-8")
FIRST = (STATIC / "first_launch.html").read_text(encoding="utf-8")

TOKENS = ["--bg-page", "--bg-card", "--text-body", "--text-muted",
          "--accent-blue", "--accent-blue-text", "--accent-green", "--accent-red"]


def test_shared_css_defines_dark_palette_tokens():
    for tok in TOKENS:
        assert f"{tok}:" in SHARED, f"{tok} missing from shared.css"


def test_welcome_and_first_launch_reference_shared_css():
    assert '/static/shared.css' in WELCOME
    assert '/static/shared.css' in FIRST


def test_welcome_and_first_launch_no_longer_define_tokens_locally():
    # The duplicate per-page definitions were removed (the token now lives only in
    # shared.css). A page still USES var(--bg-page) but must not DEFINE it.
    assert "--bg-page:" not in WELCOME
    assert "--bg-page:" not in FIRST


def test_study_and_quiz_consume_the_palette():
    study = (STATIC / "study_plan.html").read_text(encoding="utf-8")
    quiz = (STATIC / "quiz.html").read_text(encoding="utf-8")
    # Both pages re-point the legacy surface tokens onto the shared dark/blue ones.
    assert "var(--bg-page)" in study and "var(--accent-blue)" in study
    assert "var(--bg-page)" in quiz and "var(--accent-blue)" in quiz
