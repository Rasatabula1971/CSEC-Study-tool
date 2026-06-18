"""
tests/test_uploads_api_session_2.py
===================================
Upload session 2 -- endpoint tests for image upload + the re-extract routes.

Real in-memory SQLite through a Starlette TestClient (lifespan does NOT run; we
own app.state.db). SSD_ROOT -> tempdir. Tesseract is never called: image
extraction is mocked, and the re-extract tests stub the background task so the
row stays in the state the endpoint set.

  1. POST /api/upload with .png succeeds, file_type='image'.
  2. POST /api/upload with .gif -> 400.
  3. POST /api/staging/{id}/reextract resets a 'ready' row to 'pending'.
  4. POST /api/staging/{id}/reextract while 'extracting' -> 409.
  5. POST /api/staging/{subject}/reextract-all only_low_quality queues only the
     low-text files.
  6. GET /api/staging/{subject}/{id} returns the new session-2 fields.

Run: pytest tests/test_uploads_api_session_2.py -v
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
import uploads             # noqa: E402

SUBJECT = "Principles_of_Business"


# --- fixtures --------------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed")
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
    app_module.apply_runtime_migrations(db)   # m012 + m013
    return db


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    conn = open_test_db()
    conn.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    app_module.app.state.db = db
    return TestClient(app_module.app)


def insert_ready_row(db, *, original_name="paper.pdf", file_type="pdf",
                     extracted_text="some text", ocr_used=0, total_pages=None):
    db.execute(
        """
        INSERT INTO upload_staging
            (subject_id, original_name, stored_path, file_type, file_size_bytes,
             extracted_text, extract_status, status, ocr_used, total_pages)
        VALUES (?, ?, ?, ?, ?, ?, 'ready', 'staged', ?, ?)
        """,
        (SUBJECT, original_name, f"/tmp/{original_name}", file_type, 1234,
         extracted_text, ocr_used, total_pages),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


# --- tests -----------------------------------------------------------------
def test_post_png_succeeds(client, db, monkeypatch):
    monkeypatch.setattr(uploads, "_extract_image", lambda path: {
        "text": "OCR TEXT FROM IMAGE", "total_pages": 1, "ocr_pages": [1],
        "ocr_confidence_avg": 88, "truncated": False, "chunks": None,
    })
    res = client.post(
        "/api/upload",
        data={"subject_id": SUBJECT},
        files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n fake", "image/png")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert isinstance(body["staging_id"], int)

    row = db.execute(
        "SELECT file_type, extract_status, ocr_used FROM upload_staging WHERE staging_id = ?",
        (body["staging_id"],),
    ).fetchone()
    assert row["file_type"] == "image"
    assert row["extract_status"] == "ready"   # background task ran (mocked extractor)
    assert row["ocr_used"] == 1


def test_post_gif_returns_400(client):
    res = client.post(
        "/api/upload",
        data={"subject_id": SUBJECT},
        files={"file": ("animation.gif", b"GIF89a", "image/gif")},
    )
    assert res.status_code == 400
    assert res.json()["ok"] is False


def test_reextract_resets_to_pending(client, db, monkeypatch):
    # Stub the background task so the row stays in the state the endpoint set.
    monkeypatch.setattr(app_module, "_run_extraction", lambda *a, **k: None)
    sid = insert_ready_row(db, extracted_text="old text", ocr_used=1, total_pages=20)
    # give it a chunk to prove reextract clears them
    db.execute("INSERT INTO upload_staging_chunks (staging_id, chunk_index, chunk_text) "
               "VALUES (?, 0, 'x')", (sid,))
    db.commit()

    res = client.post(f"/api/staging/{sid}/reextract")
    assert res.status_code == 200
    assert res.json()["extract_status"] == "pending"

    row = db.execute(
        "SELECT extract_status, extracted_text, ocr_used FROM upload_staging WHERE staging_id = ?",
        (sid,),
    ).fetchone()
    assert row["extract_status"] == "pending"
    assert row["extracted_text"] is None
    assert row["ocr_used"] == 0
    assert uploads.count_chunks(db, sid) == 0


def test_reextract_while_extracting_returns_409(client, db):
    db.execute(
        "INSERT INTO upload_staging (subject_id, original_name, stored_path, file_type, "
        "file_size_bytes, extract_status, status) "
        "VALUES (?, 'busy.pdf', '/tmp/busy.pdf', 'pdf', 10, 'extracting', 'staged')",
        (SUBJECT,),
    )
    db.commit()
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    res = client.post(f"/api/staging/{sid}/reextract")
    assert res.status_code == 409
    assert res.json()["ok"] is False


def test_reextract_all_only_low_quality(client, db, monkeypatch):
    monkeypatch.setattr(app_module, "_run_extraction", lambda *a, **k: None)
    # 3 low-quality scanned PDFs (avg ~18 chars/page) + 2 clean PDFs (avg ~600) +
    # 1 DOCX-like row with NO page markers (digital text -> must be EXCLUDED, not
    # treated as avg=0). All ready, pre-session-2 (ocr_used=0, total_pages NULL).
    low_ids, high_ids = [], []
    for i in range(3):
        low_ids.append(insert_ready_row(
            db, original_name=f"scan{i}.pdf", extracted_text="[Page 1]\n" + "x" * 10))
    for i in range(2):
        high_ids.append(insert_ready_row(
            db, original_name=f"clean{i}.pdf", extracted_text="[Page 1]\n" + "x" * 600))
    docx_id = insert_ready_row(
        db, original_name="lecture.docx", file_type="docx",
        extracted_text="Plenty of clean digital text with no page markers at all.")

    res = client.post(f"/api/staging/{SUBJECT}/reextract-all",
                      json={"only_low_quality": True})
    assert res.status_code == 200
    body = res.json()
    assert body["queued"] == 3
    assert set(body["staging_ids"]) == set(low_ids)
    assert docx_id not in body["staging_ids"]   # no markers -> not an OCR candidate

    for sid in low_ids:
        st = db.execute(
            "SELECT extract_status FROM upload_staging WHERE staging_id = ?", (sid,)
        ).fetchone()["extract_status"]
        assert st == "pending"
    for sid in high_ids:
        st = db.execute(
            "SELECT extract_status FROM upload_staging WHERE staging_id = ?", (sid,)
        ).fetchone()["extract_status"]
        assert st == "ready"   # untouched


def test_detail_returns_session2_fields(client, db):
    sid = insert_ready_row(db, extracted_text="hello", ocr_used=1, total_pages=3)
    res = client.get(f"/api/staging/{SUBJECT}/{sid}")
    assert res.status_code == 200
    body = res.json()
    for key in ("ocr_used", "ocr_pages_count", "ocr_confidence_avg",
                "total_pages", "truncated", "has_chunks", "chunk_count"):
        assert key in body, f"missing {key}"
    assert body["ocr_used"] is True
    assert body["total_pages"] == 3
    assert body["has_chunks"] is False
    assert body["chunk_count"] == 0
