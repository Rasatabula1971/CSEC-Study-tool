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


def test_lesson_text_guard_replaces_json_stringify():
    # Fix 5: JSON.stringify(data) as a visible fallback was removed. Instead, missing
    # lesson fields are caught early with a console.error + friendly message.
    assert "JSON.stringify(data)" not in HTML
    assert "console.error('Unexpected teach response shape:" in HTML
    assert "data.lesson_text" in HTML


def test_bubble_preserves_paragraph_breaks():
    # Paragraph breaks (\n\n) render because the AI bubble is white-space: pre-wrap.
    # This is what makes the corrected lesson_text read as formatted text, not one blob.
    assert "white-space: pre-wrap" in HTML


# ---------------------------------------------------------------------------
# Stage V3: watch section wiring in chat.html
# ---------------------------------------------------------------------------

STUDY_PLAN_HTML = (ROOT / "backend" / "static" / "study_plan.html").read_text(encoding="utf-8")


def test_chat_appends_video_cards_on_teach():
    # appendVideoCards must be called in the teach branch with the objective id.
    assert "appendVideoCards(data.objective_id, lessonMsg)" in HTML


def test_study_plan_renders_video_section():
    # renderVideoSection must be awaited after renderLessonCard in renderObjectiveLesson.
    assert "await renderVideoSection(objective.objective_id,host)" in STUDY_PLAN_HTML
    # The function must exist.
    assert "async function renderVideoSection(" in STUDY_PLAN_HTML


# ---------------------------------------------------------------------------
# Visualize feature in chat.html (ported from study_plan.html)
# ---------------------------------------------------------------------------

def test_chat_appends_visualize_button_on_teach():
    # appendVisualizeButton must be called in the teach branch before appendVideoCards.
    assert "appendVisualizeButton(data.objective_id, lessonMsg)" in HTML
    # Must appear before the video cards call in the same teach block.
    assert HTML.index("appendVisualizeButton(data.objective_id, lessonMsg)") < HTML.index("appendVideoCards(data.objective_id, lessonMsg)")


def test_chat_has_visual_dialog_html():
    # The shared <dialog> element must exist (single overlay, shared across all lesson bubbles).
    assert 'id="visualDialog"' in HTML
    assert 'id="visualIframe"' in HTML
    assert 'id="visualDialogClose"' in HTML


def test_chat_has_open_visual_dialog_function():
    # The openVisualDialog function and its dynamic-element wiring must be present.
    assert "function openVisualDialog(objectiveId, btn, hint)" in HTML
    assert "function appendVisualizeButton(objectiveId, msgEl)" in HTML
    # Close wiring IIFE must reference the dialog by ID (not study_plan.html's $ shorthand).
    assert "document.getElementById('visualDialog')" in HTML
