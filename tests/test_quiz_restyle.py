"""
tests/test_quiz_restyle.py
==========================
UI overhaul session 3: Quiz visual restyle (structural HTML checks).

The restyle is visual only -- the existing functional quiz tests in test_api.py
(load filters/questions, mode toggle, grade) cover behaviour and must still pass
unmodified. These assert the served markup adopted the shared palette + components:
the segmented mode toggle is intact, the shared feedback component is included, and
the old standalone grade-card markup was replaced by the shared feedback host.

Run: pytest tests/test_quiz_restyle.py -v
"""

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.state.db = MagicMock()
    return TestClient(app_module.app)


def test_quiz_includes_shared_feedback_component(client):
    html = client.get("/quiz").text
    assert '/static/feedback.js' in html
    # Grading renders through the shared component, not the old bespoke grade-card.
    assert "renderMissedFeedback" in html
    assert 'id="feedbackHost"' in html


def test_quiz_mode_toggle_still_present(client):
    """The Past Paper | Syllabus Practice segmented control is intact (functional)."""
    html = client.get("/quiz").text
    start = html.index('class="mode-toggle"')
    end = html.index("</div>", start)
    block = html[start:end]
    assert "Past Paper" in block and "Syllabus Practice" in block


def test_quiz_uses_header_subject_dropdown(client):
    html = client.get("/quiz").text
    # Subject moved to a small header dropdown (sticky), shared style with Study.
    assert "hdr-subject-select" in html
    assert "/api/state/subject" in html      # persists the sticky subject


def test_quiz_dropped_bespoke_grade_card(client):
    """The old standalone grade card markup is gone (replaced by the shared host)."""
    html = client.get("/quiz").text
    assert 'id="gradeCard"' not in html
    assert 'id="tryAnotherBtn"' not in html
