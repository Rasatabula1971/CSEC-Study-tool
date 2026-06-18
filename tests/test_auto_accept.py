"""
tests/test_auto_accept.py
=========================
Upload follow-up -- POST /api/staging/{subject}/auto-accept-and-ingest.

Source-authority workflow: bulk-accept unreviewed classifications above a
confidence threshold, then ingest. Real in-memory SQLite via TestClient; the
background ingest worker is monkeypatched to a no-op (the synchronous accept +
queued count is what these tests assert).

  1. Auto-accepts unreviewed classifications at/above the default threshold only.
  2. A custom threshold is respected.
  3. Response reports the ingestion queue count.

Run: pytest tests/test_auto_accept.py -v
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

SUBJECT = "Principles_of_Business"


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping auto-accept tests")
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


def seed_subject(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.commit()


def stage_class(db, name, *, confidence, decision=None, skip=0,
                ingestion_status="not_started"):
    """Insert a staging row + its classification. Returns staging_id."""
    db.execute(
        "INSERT INTO upload_staging (subject_id, original_name, stored_path, file_type, "
        "file_size_bytes, extract_status, status, skip_classification, ingestion_status) "
        "VALUES (?, ?, '/tmp/x', 'pdf', 100, 'ready', 'staged', ?, ?)",
        (SUBJECT, name, skip, ingestion_status),
    )
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO upload_classifications (staging_id, recommended_folder, "
        "folder_confidence, objectives_json, model_used, review_decision) "
        "VALUES (?, '02_PAST_PAPERS', ?, '[]', 'gemini', ?)",
        (sid, confidence, decision),
    )
    db.commit()
    return sid


def decision_of(db, sid):
    return db.execute(
        "SELECT review_decision, review_notes FROM upload_classifications WHERE staging_id = ?",
        (sid,),
    ).fetchone()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "kb"))
    conn = open_test_db()
    seed_subject(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(db, monkeypatch):
    app_module.app.state.db = db
    monkeypatch.setattr(app_module, "_run_ingest_all", lambda *a, **k: None)
    return TestClient(app_module.app)


# --- Test 1 ----------------------------------------------------------------
def test_auto_accepts_above_threshold_only(client, db):
    a = stage_class(db, "A.pdf", confidence=95)                       # accept
    b = stage_class(db, "B.pdf", confidence=80)                       # accept
    c = stage_class(db, "C.pdf", confidence=60)                       # below 70
    d = stage_class(db, "D.pdf", confidence=95, decision="rejected")  # already decided
    e = stage_class(db, "E.pdf", confidence=95, skip=1)               # skipped staging

    res = client.post(f"/api/staging/{SUBJECT}/auto-accept-and-ingest", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["auto_accepted"] == 2
    assert body["skipped_low_confidence"] == 1
    assert body["already_decided"] == 1

    for sid in (a, b):
        row = decision_of(db, sid)
        assert row["review_decision"] == "accepted"
        assert row["review_notes"] == "auto_accepted_source_authority"
    assert decision_of(db, c)["review_decision"] is None          # below threshold
    assert decision_of(db, d)["review_decision"] == "rejected"     # untouched
    assert decision_of(db, e)["review_decision"] is None          # skipped staging


# --- Test 2 ----------------------------------------------------------------
def test_custom_threshold_respected(client, db):
    a = stage_class(db, "A.pdf", confidence=95)
    b = stage_class(db, "B.pdf", confidence=80)

    res = client.post(f"/api/staging/{SUBJECT}/auto-accept-and-ingest",
                      json={"min_folder_confidence": 90})
    assert res.status_code == 200
    body = res.json()
    assert body["auto_accepted"] == 1
    assert body["skipped_low_confidence"] == 1

    assert decision_of(db, a)["review_decision"] == "accepted"
    assert decision_of(db, b)["review_decision"] is None   # 80 < 90


# --- Test 3 ----------------------------------------------------------------
def test_returns_ingestion_queue_count(client, db):
    stage_class(db, "A.pdf", confidence=95)
    stage_class(db, "B.pdf", confidence=88)
    stage_class(db, "C.pdf", confidence=72)

    res = client.post(f"/api/staging/{SUBJECT}/auto-accept-and-ingest", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["auto_accepted"] == 3
    assert body["queued_for_ingestion"] == 3
    # The three accepted files are now marked queued for the background worker.
    assert db.execute(
        "SELECT COUNT(*) FROM upload_staging WHERE ingestion_status = 'queued'"
    ).fetchone()[0] == 3
