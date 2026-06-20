"""
tests/test_app_state_api.py
===========================
UI overhaul session 1: the /api/state/* endpoints (sticky subject + welcome flag).

Uses a real in-memory DB (schema.sql) on app.state.db with check_same_thread=False
(the TestClient runs the sync endpoint in a worker thread); no Ollama, no SSD.
Run: pytest tests/test_app_state_api.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"


def _client():
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping app_state API tests")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
        "VALUES ('Principles_of_Business', 'Principles of Business', 1)"
    )
    conn.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
        "VALUES ('Economics', 'Economics', 0)"  # unlocked -> rejected
    )
    conn.commit()
    app_module.app.state.db = conn
    return TestClient(app_module.app)


def test_subject_state_round_trip():
    client = _client()
    # Default: the only locked subject.
    assert client.get("/api/state/subject").json() == {"subject_id": "Principles_of_Business"}
    # Persist it explicitly and read it back.
    res = client.post("/api/state/subject", json={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "subject_id": "Principles_of_Business"}
    assert client.get("/api/state/subject").json()["subject_id"] == "Principles_of_Business"


def test_subject_state_invalid_returns_400():
    client = _client()
    # Unlocked subject.
    res = client.post("/api/state/subject", json={"subject_id": "Economics"})
    assert res.status_code == 400
    assert res.json()["ok"] is False
    # Nonexistent subject.
    res = client.post("/api/state/subject", json={"subject_id": "Mathematics"})
    assert res.status_code == 400
    assert res.json()["ok"] is False
    # Nothing was persisted -> the GET still falls back to the locked default.
    assert client.get("/api/state/subject").json()["subject_id"] == "Principles_of_Business"


def test_welcome_seen_round_trip():
    client = _client()
    assert client.get("/api/state/welcome-seen").json() == {"seen": False}
    res = client.post("/api/state/welcome-seen")
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert client.get("/api/state/welcome-seen").json() == {"seen": True}
