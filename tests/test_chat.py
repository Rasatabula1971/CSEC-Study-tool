"""
tests/test_chat.py
==================
Structural guards for the Tutor Chat page (backend/static/chat.html).

Regression cover for the teach-render field-name bug (2026-06-19): chat.html read
the lesson off `data.lesson`, but the backend returns the v2 canonical-lesson field
as `data.lesson_text` (study_plan.html already read this correctly). The mismatch
made every `||` alias undefined, so the teach branch fell through to
`JSON.stringify(data)` and dumped the raw response object into the chat bubble.

Full JS interaction isn't exercised by this Python suite, so -- matching the
structural-check convention in tests/test_panel_shell.py and the jump-view checks in
tests/test_study_plan.py -- these assert the served markup reads the correct field
and no longer carries the raw-dump-first chain.

Run: pytest tests/test_chat.py -v
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHAT_HTML = ROOT / "backend" / "static" / "chat.html"
HTML = CHAT_HTML.read_text(encoding="utf-8")


def test_teach_branch_reads_lesson_text_first():
    # The fix: the teach render reads `lesson_text` (the backend field) ahead of the
    # legacy aliases, mirroring study_plan.html's `data.lesson_text||data.lesson`.
    assert "data.lesson_text || data.lesson" in HTML


def test_no_raw_dump_first_chain():
    # The old buggy line read `data.lesson` first and never `lesson_text`, so it
    # JSON.stringify'd the response. That exact chain must be gone.
    assert "const text = data.lesson ||" not in HTML


def test_lesson_text_precedes_json_stringify_fallback():
    # JSON.stringify(data) stays only as the last-resort fallback, and `lesson_text`
    # must come before it (so a real lesson is never stringified).
    assert "JSON.stringify(data)" in HTML
    assert HTML.index("data.lesson_text") < HTML.index("JSON.stringify(data)")


def test_bubble_preserves_paragraph_breaks():
    # Paragraph breaks (\n\n) render because the AI bubble is white-space: pre-wrap.
    # This is what makes the corrected lesson_text read as formatted text, not one blob.
    assert "white-space: pre-wrap" in HTML
