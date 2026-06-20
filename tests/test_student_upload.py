"""
tests/test_student_upload.py
============================
UI overhaul session 2: the student-facing POST /api/student-upload endpoint.

Real in-memory DB + TestClient (lifespan does NOT run, so we own app.state.db).
Heavy boundaries are stubbed: uploads.extract_text (no PyMuPDF/Tesseract) and
classify_uploads.chat_for_classification (no Gemini). The high-confidence path uses
a 00_SYLLABUS placement so ingest_staged_file ARCHIVES the file (no chunk/embed),
keeping Ollama out of the test while still exercising real ingestion bookkeeping.

  1. High-confidence file -> outcome 'added', file ingested.
  2. Low-confidence file  -> outcome 'needs_review', stays staged + unreviewed.
  3. Unsupported file      -> outcome 'error', non-technical message, no stack trace.
  4. Distinct from the builder batch: a single student upload does not touch another
     staged file's classification_status.

Run: pytest tests/test_student_upload.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module          # noqa: E402
import uploads                    # noqa: E402
import classify_uploads           # noqa: E402

SUBJECT = "Principles_of_Business"


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping student-upload tests")
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
        "'Discuss the role of business', '[\"Discuss\"]')",
        (SUBJECT,),
    )
    db.commit()


def _fake_extract(extracted_text="A worksheet about the role of business."):
    def fake_extract_text(staging_id, db):
        db.execute(
            "UPDATE upload_staging SET extracted_text = ?, extract_status = 'ready', "
            "status = 'staged' WHERE staging_id = ?",
            (extracted_text, staging_id),
        )
        db.commit()
    return fake_extract_text


def _fake_chat(folder, folder_conf, objectives):
    """A stand-in for chat_for_classification returning a fixed classification JSON."""
    def fake_chat(messages, system, schema=None):
        return json.dumps({
            "recommended_folder": folder,
            "folder_confidence": folder_conf,
            "objectives": objectives,
            "rationale": "Test classification.",
        })
    return fake_chat


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_ROOT", str(tmp_path / "kb"))
    # uploads reads the staging root from SSD_ROOT at call time via _staging_root().
    db = open_test_db()
    seed(db)
    monkeypatch.setattr(uploads, "extract_text", _fake_extract())
    app_module.app.state.db = db
    client = TestClient(app_module.app)
    yield client, db, monkeypatch
    db.close()


def _post(client, name, content=b"%PDF-1.4 fake pdf bytes", ctype="application/pdf"):
    return client.post(
        "/api/student-upload",
        data={"subject_id": SUBJECT},
        files={"file": (name, content, ctype)},
    )


# --- Test 1: high confidence -> added + ingested -----------------------------
def test_high_confidence_adds_and_ingests(ctx):
    client, db, monkeypatch = ctx
    # 00_SYLLABUS placement -> archive ingestion (no embed/Ollama).
    monkeypatch.setattr(classify_uploads, "chat_for_classification",
                        _fake_chat("00_SYLLABUS", 95, [{"objective_id": "POB-1.2", "confidence": 92}]))
    res = _post(client, "worksheet.pdf")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["outcome"] == "added"
    assert body["section"] == "Syllabus"
    # The file was really ingested (archived) -- one staged row, status 'ingested'.
    row = db.execute(
        "SELECT ingestion_status, c.review_decision FROM upload_staging s "
        "JOIN upload_classifications c ON c.staging_id = s.staging_id"
    ).fetchone()
    assert row["ingestion_status"] == "ingested"
    assert row["review_decision"] == "accepted"


# --- Test 2: low confidence -> needs_review, not ingested --------------------
def test_low_confidence_needs_review(ctx):
    client, db, monkeypatch = ctx
    monkeypatch.setattr(classify_uploads, "chat_for_classification",
                        _fake_chat("04_NOTES", 60, [{"objective_id": "POB-1.2", "confidence": 55}]))
    res = _post(client, "blurry.pdf")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["outcome"] == "needs_review"
    # No confidence score / section is leaked to the student.
    assert "section" not in body
    row = db.execute(
        "SELECT s.ingestion_status, c.review_decision, c.review_notes "
        "FROM upload_staging s JOIN upload_classifications c ON c.staging_id = s.staging_id"
    ).fetchone()
    assert row["ingestion_status"] == "not_started"      # NOT ingested
    assert row["review_decision"] is None                # left for the builder
    assert row["review_notes"] == "pending_student_upload_review"


# --- Test 3: unsupported file -> error, no technical leak --------------------
def test_unsupported_file_returns_clean_error(ctx):
    client, db, monkeypatch = ctx
    res = client.post(
        "/api/student-upload",
        data={"subject_id": SUBJECT},
        files={"file": ("notes.txt", b"just some text", "text/plain")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["outcome"] == "error"
    assert isinstance(body.get("message"), str) and body["message"]
    # Nothing technical leaked.
    raw = json.dumps(body).lower()
    for leak in ("traceback", "exception", "sqlite", "stack", "staging_id"):
        assert leak not in raw
    # Nothing was staged.
    assert db.execute("SELECT COUNT(*) FROM upload_staging").fetchone()[0] == 0


# --- Test 4: distinct from the builder's batch ------------------------------
def test_does_not_touch_other_staged_files(ctx):
    client, db, monkeypatch = ctx
    # A pre-existing builder file awaiting classification.
    db.execute(
        "INSERT INTO upload_staging (subject_id, original_name, stored_path, file_type, "
        "file_size_bytes, extract_status, status, classification_status, created_at) "
        "VALUES (?, 'builder.pdf', '/tmp/b', 'pdf', 100, 'ready', 'staged', "
        "'unclassified', '2026-06-19 09:00:00')",
        (SUBJECT,),
    )
    builder_sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()

    monkeypatch.setattr(classify_uploads, "chat_for_classification",
                        _fake_chat("04_NOTES", 60, [{"objective_id": "POB-1.2", "confidence": 55}]))
    res = _post(client, "student.pdf")
    assert res.json()["outcome"] == "needs_review"

    # The builder's other file is completely untouched by the single-file run.
    assert db.execute(
        "SELECT classification_status FROM upload_staging WHERE staging_id = ?",
        (builder_sid,),
    ).fetchone()[0] == "unclassified"
    # And it got no classification row.
    assert db.execute(
        "SELECT COUNT(*) FROM upload_classifications WHERE staging_id = ?",
        (builder_sid,),
    ).fetchone()[0] == 0
