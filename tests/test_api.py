"""
tests/test_api.py
=================
Stage 6 tests for the FastAPI layer. The DB and controller are mocked, so these
tests need no SSD, no real database, and no Ollama.

The app's lifespan (SSD check + DB open) is NOT triggered: a plain TestClient
(used without the `with` context manager) does not run lifespan events, so we
set app.state.db ourselves and patch the controller/health hooks.

Run: pytest tests/test_api.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.state.db = MagicMock()
    return TestClient(app_module.app)


def test_health_returns_200(client, monkeypatch):
    monkeypatch.setattr(app_module, "ollama_health", lambda: False)  # no network wait
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert body["ollama"] is False


def test_chat_valid_payload_returns_json(client, monkeypatch):
    captured = {}

    def fake_handle(db, req, *args, **kwargs):
        captured["req"] = req
        return {"route": "teach", "objective_id": "POB-1.1", "lesson": "A business..."}

    monkeypatch.setattr(app_module, "handle_request", fake_handle)

    res = client.post("/api/chat", json={
        "message": "nature of business",
        "subject_id": "Principles_of_Business",
        "route": "teach",
    })
    assert res.status_code == 200
    assert res.json()["objective_id"] == "POB-1.1"
    # the message is mapped onto the controller's request shape
    assert captured["req"]["query"] == "nature of business"
    assert captured["req"]["student_answer"] == "nature of business"
    assert captured["req"]["route"] == "teach"


def test_chat_missing_subject_id_returns_422(client):
    res = client.post("/api/chat", json={"message": "hi", "route": "teach"})
    assert res.status_code == 422


def test_subjects_lists_locked_only(client):
    # configure the mocked DB to return one locked subject row
    row = {"subject_id": "Principles_of_Business", "display_name": "Principles of Business"}
    app_module.app.state.db.execute.return_value.fetchall.return_value = [row]
    res = client.get("/api/subjects")
    assert res.status_code == 200
    assert res.json() == [row]
