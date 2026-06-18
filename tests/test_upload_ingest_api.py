"""
tests/test_upload_ingest_api.py
===============================
Upload session 4 -- endpoint tests for the ingestion + stale-lesson routes.

Real in-memory SQLite through a Starlette TestClient (lifespan does NOT run, so we
own app.state.db). Background ingestion is monkeypatched to a no-op so no files move
and no Ollama runs; the synchronous queued COUNT / status writes are what these
tests assert. The regenerate test patches ingest_lessons so the real is_stale-clear
logic runs without composing a lesson.

  1. POST /api/staging/{id}/ingest -> 400 when the classification is not accepted.
  2. POST /api/staging/{id}/ingest -> 200 + status 'queued' for an accepted file.
  3. POST /api/staging/{subject}/ingest-all counts only eligible files.
  4. GET  /api/staging/{subject}/ingestion-status returns totals + items.
  5. GET  /api/lessons/stale/{subject} lists stale lessons with caused_by_files.
  6. POST /api/lessons/{objective}/regenerate clears is_stale on success.

Run: pytest tests/test_upload_ingest_api.py -v
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

import app as app_module      # noqa: E402
import ingest_lessons         # noqa: E402

SUBJECT = "Principles_of_Business"


# --- fixtures --------------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping ingest API tests")
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
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, command_words) VALUES ('POB-1.2', 'SEC-1', ?, '1.2', "
        "'Discuss the role of business in development', '[\"Discuss\"]')",
        (SUBJECT,),
    )
    db.commit()


def stage(db, name, *, decision=None, ingestion_status="not_started",
          recommended_folder="02_PAST_PAPERS"):
    db.execute(
        "INSERT INTO upload_staging (subject_id, original_name, stored_path, file_type, "
        "file_size_bytes, extract_status, status, ingestion_status, created_at) "
        "VALUES (?, ?, '/tmp/x', 'pdf', 100, 'ready', 'staged', ?, '2026-06-18 09:00:00')",
        (SUBJECT, name, ingestion_status),
    )
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO upload_classifications (staging_id, recommended_folder, "
        "folder_confidence, objectives_json, model_used, review_decision) "
        "VALUES (?, ?, 90, '[]', 'gemini', ?)",
        (sid, recommended_folder, decision),
    )
    db.commit()
    return sid


def seed_stale_lesson(db, objective_id="POB-1.2"):
    db.execute(
        "INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
        "recall_questions, source_chunk_ids, confidence, is_stale, stale_reason, staled_at) "
        "VALUES (?, ?, ?, 'body', '[]', '[]', 80, 1, 'new_source_material_added', "
        "'2026-06-18 03:15:00')",
        (f"L-{objective_id}", objective_id, SUBJECT),
    )
    db.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "kb"))
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(db, monkeypatch):
    app_module.app.state.db = db
    # Neutralise the real background workers so no files move / no Ollama runs.
    monkeypatch.setattr(app_module, "_run_ingest_one", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_run_ingest_all", lambda *a, **k: None)
    return TestClient(app_module.app)


# --- Test 1 ----------------------------------------------------------------
def test_ingest_one_400_when_not_accepted(client, db):
    sid = stage(db, "unreviewed.pdf", decision=None)   # classified, not reviewed
    res = client.post(f"/api/staging/{sid}/ingest")
    assert res.status_code == 400
    assert res.json()["ok"] is False


# --- Test 2 ----------------------------------------------------------------
def test_ingest_one_queues(client, db):
    sid = stage(db, "ok.pdf", decision="accepted")
    res = client.post(f"/api/staging/{sid}/ingest")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["ingestion_status"] == "queued"
    assert db.execute(
        "SELECT ingestion_status FROM upload_staging WHERE staging_id = ?", (sid,)
    ).fetchone()[0] == "queued"


# --- Test 3 ----------------------------------------------------------------
def test_ingest_all_counts_eligible(client, db):
    stage(db, "a.pdf", decision="accepted")
    stage(db, "b.pdf", decision="overridden")
    stage(db, "c.pdf", decision="rejected")        # not eligible
    stage(db, "d.pdf", decision=None)              # not eligible
    stage(db, "e.pdf", decision="accepted", ingestion_status="ingested")  # already done

    res = client.post(f"/api/staging/{SUBJECT}/ingest-all", json={})
    assert res.status_code == 200
    assert res.json()["queued"] == 2


# --- Test 4 ----------------------------------------------------------------
def test_ingestion_status_totals(client, db):
    stage(db, "n.pdf", decision="accepted", ingestion_status="not_started")
    stage(db, "q.pdf", decision="accepted", ingestion_status="queued")
    stage(db, "i.pdf", decision="accepted", ingestion_status="ingested")
    stage(db, "f.pdf", decision="accepted", ingestion_status="failed")

    res = client.get(f"/api/staging/{SUBJECT}/ingestion-status")
    assert res.status_code == 200
    body = res.json()
    t = body["totals"]
    assert t["not_started"] == 1 and t["queued"] == 1
    assert t["ingested"] == 1 and t["failed"] == 1
    assert len(body["items"]) == 4


# --- Test 5 ----------------------------------------------------------------
def test_stale_lessons_lists_with_cause(client, db):
    seed_stale_lesson(db, "POB-1.2")
    sid = stage(db, "lecture-8.docx", decision="accepted", ingestion_status="ingested")
    db.execute(
        "INSERT INTO ingestion_log (staging_id, started_at, finished_at, success, "
        "chunks_created, objectives_hit, lessons_staled) "
        "VALUES (?, '2026-06-18 03:15:00', '2026-06-18 03:16:00', 1, 4, "
        "'[\"POB-1.2\"]', '[\"POB-1.2\"]')",
        (sid,),
    )
    db.commit()

    res = client.get(f"/api/lessons/stale/{SUBJECT}")
    assert res.status_code == 200
    stale = res.json()["stale_lessons"]
    assert len(stale) == 1
    assert stale[0]["objective_id"] == "POB-1.2"
    assert stale[0]["caused_by_files"] == ["lecture-8.docx"]


# --- Test 6 ----------------------------------------------------------------
def test_regenerate_clears_is_stale(client, db, monkeypatch):
    seed_stale_lesson(db, "POB-1.2")

    # Stub composition: report the objective as written, write nothing else.
    def fake_regen(db_, subject_id, *, regenerate, objective_ids=None, **kw):
        return {"rows": [{"objective_id": oid, "status": "written"}
                         for oid in (objective_ids or [])]}

    monkeypatch.setattr(ingest_lessons, "ingest_lessons_for_subject", fake_regen)

    res = client.post("/api/lessons/POB-1.2/regenerate")
    assert res.status_code == 200
    assert res.json()["queued_for"] == "POB-1.2"
    # Background task ran synchronously in the TestClient -> is_stale cleared.
    assert db.execute(
        "SELECT is_stale FROM objective_lessons WHERE objective_id = 'POB-1.2'"
    ).fetchone()[0] == 0
