"""
tests/test_lesson_status.py
===========================
Read-only lesson-generation status endpoint + page.

Real in-memory SQLite through a Starlette TestClient (lifespan does NOT run, so we
own app.state.db, like the other API tests). Pure DB read -- no Ollama, no network.

  1. GET /api/lessons/status/{subject} -> 200.
  2. Response carries all required fields.
  3. queue_by_reason is a dict of int values (grouped on the reason prefix).
  4. recent_activity is a list of at most 10 items.
  5. GET /lessons/status -> 200 text/html.

Run: pytest tests/test_lesson_status.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module   # noqa: E402
import ingest_lessons as il  # noqa: E402

SUBJECT = "Principles_of_Business"


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping lesson status tests")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    app_module.apply_runtime_migrations(db)
    il.ensure_lesson_tables(db)
    return db


def seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    for n in range(1, 6):  # 5 objectives POB-1.1 .. POB-1.5
        oid = f"POB-1.{n}"
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt, command_words) VALUES (?, 'SEC-1', ?, ?, ?, '[\"Explain\"]')",
            (oid, SUBJECT, f"1.{n}", f"Content for {oid}"),
        )
    # one written lesson
    db.execute(
        "INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
        "recall_questions, source_chunk_ids, confidence, generated_at) "
        "VALUES ('L1', 'POB-1.1', ?, 'body', '[]', '[]', 80, '2026-06-18 18:42:00')",
        (SUBJECT,),
    )
    # one stale lesson
    db.execute(
        "INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
        "recall_questions, source_chunk_ids, confidence, generated_at, is_stale, "
        "stale_reason, staled_at) VALUES ('L2', 'POB-1.2', ?, 'body', '[]', '[]', 80, "
        "'2026-06-18 18:40:00', 1, 'new_source_material_added', '2026-06-18 18:43:00')",
        (SUBJECT,),
    )
    # queue rows: prefixes 'insufficient_sources' and 'quality_check_failed'
    for oid, reason in (("POB-1.3", "insufficient_sources"),
                        ("POB-1.4", "quality_check_failed: pre-existing"),
                        ("POB-1.5", "served_placeholder")):
        db.execute(
            "INSERT INTO lesson_generation_queue (objective_id, reason, created_at) "
            "VALUES (?, ?, '2026-06-18 18:41:00')",
            (oid, reason),
        )
    db.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    app_module.app.state.db = db
    return TestClient(app_module.app)


def test_status_returns_200(client):
    res = client.get(f"/api/lessons/status/{SUBJECT}")
    assert res.status_code == 200


def test_status_has_all_required_fields(client):
    body = client.get(f"/api/lessons/status/{SUBJECT}").json()
    for key in ("subject_id", "total_objectives", "lessons_written", "lessons_stale",
                "lessons_queued", "queue_by_reason", "recent_activity"):
        assert key in body, f"missing field: {key}"
    assert body["subject_id"] == SUBJECT
    assert body["total_objectives"] == 5
    assert body["lessons_written"] == 2
    assert body["lessons_stale"] == 1
    assert body["lessons_queued"] == 3


def test_queue_by_reason_is_dict_of_ints(client):
    qbr = client.get(f"/api/lessons/status/{SUBJECT}").json()["queue_by_reason"]
    assert isinstance(qbr, dict)
    assert all(isinstance(v, int) for v in qbr.values())
    # the reason prefix is grouped: 'quality_check_failed: pre-existing' -> 'quality_check_failed'
    assert qbr.get("insufficient_sources") == 1
    assert qbr.get("quality_check_failed") == 1
    assert qbr.get("served_placeholder") == 1


def test_recent_activity_is_capped_list(client):
    acts = client.get(f"/api/lessons/status/{SUBJECT}").json()["recent_activity"]
    assert isinstance(acts, list)
    assert len(acts) <= 10
    # newest-first ordering: the staled event (18:43) precedes the written one (18:42)
    assert acts[0]["timestamp"] >= acts[-1]["timestamp"]
    for a in acts:
        assert {"objective_id", "event", "reason", "timestamp"} <= set(a.keys())


def test_status_page_served(client):
    res = client.get("/lessons/status")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
