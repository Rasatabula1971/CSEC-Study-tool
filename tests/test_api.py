"""
tests/test_api.py
=================
Stage 6 tests for the FastAPI layer. app.state.db and controller.handle_request
are mocked throughout, so these tests need no SSD, no real database, and no Ollama.

The app's lifespan (SSD check + DB open) is NOT triggered: a plain TestClient
(used without the `with` context manager) does not run lifespan events, so we
set app.state.db ourselves and patch the controller/health hooks.

Run: pytest tests/test_api.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.state.db = MagicMock()
    return TestClient(app_module.app)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_returns_200(client, monkeypatch):
    monkeypatch.setattr(app_module, "ollama_health", lambda: False)  # no network wait
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body == {"status": "ok", "ollama": False, "db": True}


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------
def test_chat_valid_payload_returns_json(client, monkeypatch):
    captured = {}

    def fake_handle(db, req, *args, **kwargs):
        captured["req"] = req
        return {"route": "teach", "objective_id": "POB-1.1", "lesson": "A business..."}

    monkeypatch.setattr(app_module, "handle_request", fake_handle)

    res = client.post("/api/chat", json={
        "message": "explain business",
        "subject_id": "Principles_of_Business",
        "route": "teach",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["objective_id"] == "POB-1.1"
    # the message is mapped onto the controller's request shape
    assert captured["req"]["query"] == "explain business"
    assert captured["req"]["student_answer"] == "explain business"
    assert captured["req"]["route"] == "teach"


def test_chat_missing_subject_id_returns_422(client):
    res = client.post("/api/chat", json={"message": "hi", "route": "teach"})
    assert res.status_code == 422


def test_chat_empty_message_returns_422(client):
    res = client.post("/api/chat", json={
        "message": "",
        "subject_id": "Principles_of_Business",
        "route": "teach",
    })
    assert res.status_code == 422


def test_chat_plan_result_is_aliased_for_ui(client, monkeypatch):
    """The UI reads `objectives`; the controller returns `tasks`. app.py bridges."""
    monkeypatch.setattr(app_module, "handle_request", lambda db, req, *a, **k: {
        "route": "plan", "subject_id": "Principles_of_Business", "due_count": 1,
        "tasks": [{"objective_id": "POB-1.1", "leitner_box": 1, "next_review": "2026-06-14"}],
    })
    res = client.post("/api/chat", json={
        "message": "(plan)", "subject_id": "Principles_of_Business", "route": "plan",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["objectives"] == body["tasks"]  # alias present, original kept


# ---------------------------------------------------------------------------
# GET /api/subjects and /api/due/{subject_id}
# ---------------------------------------------------------------------------
def test_subjects_returns_list(client):
    row = {"subject_id": "Principles_of_Business", "display_name": "Principles of Business"}
    app_module.app.state.db.execute.return_value.fetchall.return_value = [row]
    res = client.get("/api/subjects")
    assert res.status_code == 200
    assert res.json() == [row]


def test_subjects_empty_is_ok(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/subjects")
    assert res.status_code == 200
    assert res.json() == []


def test_due_returns_list(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/due/Principles_of_Business")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


# ---------------------------------------------------------------------------
# GET /api/questions/{subject_id}
# ---------------------------------------------------------------------------
def test_questions_returns_labelled_list(client):
    row = {
        "question_id": "POB-2026Jan-P2-q1a",
        "objective_id": "POB-1.14",
        "question_text": "List THREE careers ... (3 marks)",
        "question_num": "1(a)",
        "paper": "Paper 2 - January",
        "year": 2026,
        "marks": 3,
    }
    app_module.app.state.db.execute.return_value.fetchall.return_value = [row]
    res = client.get("/api/questions/Principles_of_Business")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    q = body[0]
    assert q["question_id"] == "POB-2026Jan-P2-q1a"   # the key grade.py needs
    assert q["marks"] == 3
    assert q["label"] == "2026 · Paper 2 - January · Q1(a)"  # built for display


def test_questions_empty_is_ok(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/questions/Principles_of_Business")
    assert res.status_code == 200
    assert res.json() == []


# ---------------------------------------------------------------------------
# GET /quiz  and  GET /api/questions?subject_id=...  (quiz page additions)
# ---------------------------------------------------------------------------
def test_quiz_page_returns_200(client):
    res = client.get("/quiz")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_questions_query_param_returns_list(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/questions", params={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    assert res.json() == []
    assert isinstance(res.json(), list)


# ---------------------------------------------------------------------------
# GET /api/filters?subject_id=...
# ---------------------------------------------------------------------------
def test_filters_returns_papers_and_years(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/filters", params={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    body = res.json()
    assert "papers" in body and "years" in body
    assert isinstance(body["papers"], list)
    assert isinstance(body["years"], list)
