"""
tests/test_classify_uploads.py
==============================
Upload session 3 -- the m015 auto-skip backfill and backend/classify_uploads.py.

Real in-memory SQLite (schema.sql + apply_runtime_migrations + sqlite-vec, FKs ON)
so the upload_staging / upload_classifications constraints are genuinely exercised.
SSD_ROOT points at a per-test tempdir. The model is injected via chat_fn -- no
Gemini, no Ollama, no network.

  1. m015 backfill flags low-OCR-confidence files as skip.
  2. m015 marks format twins, preferring DOCX.
  3. m015 marks duplicate content (keeps the lowest staging_id).
  4. classify_uploads with a mocked model writes a classification.
  5. classify_uploads filters invented objective_ids.
  6. classify_uploads skips skip_classification=1 files (no model call).
  7. classify_uploads --force re-classifies an already-done file.

Run: pytest tests/test_classify_uploads.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module        # noqa: E402  (apply_runtime_migrations)
import classify_uploads         # noqa: E402

SUBJECT = "Principles_of_Business"


# --- fixtures --------------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping classify tests")
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


def seed_objectives(db: sqlite3.Connection, ids=("POB-1.1", "POB-1.2", "POB-3.4")) -> None:
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    for i, oid in enumerate(ids, 1):
        num = oid.split("-", 1)[1]
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt, skill_type, command_words) "
            "VALUES (?, 'SEC-1', ?, ?, ?, 'Understanding', '[\"Explain\"]')",
            (oid, SUBJECT, num, f"Content statement for {oid}"),
        )
    db.commit()


def insert_staging(db, name, *, file_type="pdf", extract_status="ready",
                   extracted_text="x", ocr_used=0, ocr_confidence_avg=None,
                   ocr_dpi_reduced=0, truncated=0, skip_classification=0,
                   classification_status="unclassified", created_at="2026-06-17 09:00:00"):
    db.execute(
        """
        INSERT INTO upload_staging
            (subject_id, original_name, stored_path, file_type, file_size_bytes,
             extracted_text, extract_status, status, created_at,
             ocr_used, ocr_confidence_avg, ocr_dpi_reduced, truncated,
             skip_classification, classification_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?, ?, ?, ?, ?)
        """,
        (SUBJECT, name, f"/tmp/{name}", file_type, 1234, extracted_text,
         extract_status, created_at, ocr_used, ocr_confidence_avg, ocr_dpi_reduced,
         truncated, skip_classification, classification_status),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    conn = open_test_db()
    seed_subject(conn)
    yield conn
    conn.close()


def _skip(db, sid):
    r = db.execute(
        "SELECT skip_classification, skip_reason FROM upload_staging WHERE staging_id = ?",
        (sid,),
    ).fetchone()
    return r["skip_classification"], r["skip_reason"]


# --- Test 1: low-OCR-confidence backfill -----------------------------------
def test_m015_flags_low_ocr_confidence(db):
    high = insert_staging(db, "high.pdf", ocr_used=1, ocr_confidence_avg=85)
    low = insert_staging(db, "low.pdf", ocr_used=1, ocr_confidence_avg=45)
    none = insert_staging(db, "digital.pdf", ocr_used=0)

    app_module.apply_runtime_migrations(db)   # re-run -> Layer 2 backfill flags rows

    assert _skip(db, high)[0] == 0
    assert _skip(db, none)[0] == 0
    skip_flag, reason = _skip(db, low)
    assert skip_flag == 1
    assert "low_ocr_confidence" in (reason or "")


# --- Test 2: format twins prefer DOCX --------------------------------------
def test_m015_marks_format_twin_keeps_docx(db):
    pdf = insert_staging(db, "lecture-1.pdf", file_type="pdf", extracted_text="a" * 200)
    docx = insert_staging(db, "lecture-1.docx", file_type="docx", extracted_text="b" * 300)

    app_module.apply_runtime_migrations(db)

    pdf_flag, pdf_reason = _skip(db, pdf)
    assert pdf_flag == 1
    assert "format_twin" in (pdf_reason or "")
    assert _skip(db, docx)[0] == 0


# --- Test 3: duplicate content ---------------------------------------------
def test_m015_marks_duplicate_content(db):
    text = "z" * 5000
    first = insert_staging(db, "copy-a.pdf", extracted_text=text)
    second = insert_staging(db, "copy-b.pdf", extracted_text=text)
    assert first < second

    app_module.apply_runtime_migrations(db)

    assert _skip(db, first)[0] == 0       # lowest staging_id kept
    flag, reason = _skip(db, second)
    assert flag == 1
    assert "duplicate_content" in (reason or "")


# --- Test 4: mocked model writes a classification --------------------------
def test_classify_writes_classification(db):
    seed_objectives(db)
    sid = insert_staging(db, "paper.pdf", extracted_text="A past paper about business.")

    chat_fn = MagicMock(return_value=json.dumps({
        "recommended_folder": "02_PAST_PAPERS",
        "folder_confidence": 90,
        "objectives": [{"objective_id": "POB-1.1", "confidence": 88}],
        "rationale": "Looks like a past paper on the nature of business.",
    }))

    summary = classify_uploads.classify_uploads(db, SUBJECT, chat_fn=chat_fn, verbose=False)

    assert summary["classified"] == 1
    chat_fn.assert_called_once()
    row = db.execute(
        "SELECT recommended_folder, objectives_json FROM upload_classifications "
        "WHERE staging_id = ?", (sid,),
    ).fetchone()
    assert row is not None
    assert row["recommended_folder"] == "02_PAST_PAPERS"
    assert json.loads(row["objectives_json"]) == [{"objective_id": "POB-1.1", "confidence": 88}]
    status = db.execute(
        "SELECT classification_status FROM upload_staging WHERE staging_id = ?", (sid,),
    ).fetchone()[0]
    assert status == "classified"


# --- Test 5: invented objective_ids are filtered ---------------------------
def test_classify_filters_invented_objective_ids(db):
    seed_objectives(db, ids=("POB-1.1", "POB-1.2"))
    sid = insert_staging(db, "notes.pdf", extracted_text="Notes about business.")

    chat_fn = MagicMock(return_value=json.dumps({
        "recommended_folder": "04_NOTES",
        "folder_confidence": 80,
        "objectives": [
            {"objective_id": "POB-1.1", "confidence": 90},
            {"objective_id": "POB-99.99", "confidence": 70},   # not in syllabus
        ],
        "rationale": "Business notes.",
    }))

    classify_uploads.classify_uploads(db, SUBJECT, chat_fn=chat_fn, verbose=False)

    row = db.execute(
        "SELECT objectives_json, rationale FROM upload_classifications WHERE staging_id = ?",
        (sid,),
    ).fetchone()
    ids = [o["objective_id"] for o in json.loads(row["objectives_json"])]
    assert "POB-99.99" not in ids
    assert ids == ["POB-1.1"]
    assert "Filtered" in (row["rationale"] or "")   # the drop was noted


# --- Test 6: skipped files are not classified ------------------------------
def test_classify_skips_skipped_files(db):
    seed_objectives(db)
    sid = insert_staging(db, "scan.pdf", skip_classification=1)

    chat_fn = MagicMock(return_value="{}")
    classify_uploads.classify_uploads(db, SUBJECT, chat_fn=chat_fn, verbose=False)

    chat_fn.assert_not_called()
    status = db.execute(
        "SELECT classification_status FROM upload_staging WHERE staging_id = ?", (sid,),
    ).fetchone()[0]
    assert status == "skipped"


# --- Test 7: --force re-classifies an already-classified file --------------
def test_classify_force_reclassifies(db):
    seed_objectives(db)
    sid = insert_staging(db, "paper.pdf", classification_status="classified",
                         extracted_text="A past paper.")
    db.execute(
        "INSERT INTO upload_classifications "
        "(staging_id, recommended_folder, folder_confidence, objectives_json, "
        " rationale, model_used) VALUES (?, '04_NOTES', 50, '[]', 'old', 'ollama')",
        (sid,),
    )
    db.commit()

    chat_fn = MagicMock(return_value=json.dumps({
        "recommended_folder": "02_PAST_PAPERS",
        "folder_confidence": 92,
        "objectives": [{"objective_id": "POB-1.1", "confidence": 80}],
        "rationale": "Re-classified as a past paper.",
    }))

    # Without --force: an already-classified file is skipped, model never called.
    classify_uploads.classify_uploads(db, SUBJECT, chat_fn=chat_fn, verbose=False)
    chat_fn.assert_not_called()
    assert db.execute(
        "SELECT recommended_folder FROM upload_classifications WHERE staging_id = ?", (sid,),
    ).fetchone()[0] == "04_NOTES"

    # With --force: the row is replaced with the new proposal.
    classify_uploads.classify_uploads(db, SUBJECT, force=True, chat_fn=chat_fn, verbose=False)
    chat_fn.assert_called_once()
    assert db.execute(
        "SELECT recommended_folder FROM upload_classifications WHERE staging_id = ?", (sid,),
    ).fetchone()[0] == "02_PAST_PAPERS"
    assert db.execute(
        "SELECT COUNT(*) FROM upload_classifications WHERE staging_id = ?", (sid,),
    ).fetchone()[0] == 1   # still one row (UNIQUE staging_id), not duplicated


# --- Test 8: _extract_json tolerates a markdown-fenced response -------------
def test_extract_json_strips_markdown_fence(db):
    seed_objectives(db)
    sid = insert_staging(db, "fenced.pdf", extracted_text="A business document.")

    fenced = ("```json\n"
              '{"recommended_folder": "04_NOTES", "folder_confidence": 75, '
              '"objectives": [{"objective_id": "POB-1.1", "confidence": 60}], '
              '"rationale": "Notes."}\n```')
    chat_fn = MagicMock(return_value=fenced)

    summary = classify_uploads.classify_uploads(db, SUBJECT, chat_fn=chat_fn, verbose=False)

    assert summary["classified"] == 1   # parsed despite the ```json fence
    row = db.execute(
        "SELECT recommended_folder FROM upload_classifications WHERE staging_id = ?", (sid,),
    ).fetchone()
    assert row["recommended_folder"] == "04_NOTES"


# --- Test 9: a bulk run self-heals a stuck 'classifying' row ----------------
def test_classify_self_heals_stuck_classifying(db):
    seed_objectives(db)
    # A previous run was interrupted, leaving this row stuck mid-flight.
    sid = insert_staging(db, "stuck.pdf", classification_status="classifying",
                         extracted_text="A business doc.")

    chat_fn = MagicMock(return_value=json.dumps({
        "recommended_folder": "04_NOTES", "folder_confidence": 70,
        "objectives": [], "rationale": "Recovered.",
    }))
    classify_uploads.classify_uploads(db, SUBJECT, chat_fn=chat_fn, verbose=False)

    chat_fn.assert_called_once()   # the stuck row was reset to eligible and reclassified
    status = db.execute(
        "SELECT classification_status FROM upload_staging WHERE staging_id = ?", (sid,),
    ).fetchone()[0]
    assert status == "classified"
