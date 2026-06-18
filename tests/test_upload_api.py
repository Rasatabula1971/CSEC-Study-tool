"""
tests/test_upload_api.py
========================
Upload session 1 -- endpoint tests for the /api/upload + /api/staging routes.

Real in-memory SQLite driven through a Starlette TestClient (lifespan does NOT
run, so we own app.state.db, exactly like tests/test_api.py + test_feedback.py).
SSD_ROOT points at a per-test tempdir. The TestClient runs FastAPI BackgroundTasks
synchronously, so the upload's background extraction completes before the POST
returns.

  1. POST valid PDF -> 200, ok=True, staging_id int, extract_status 'pending'.
  2. POST .xlsx                 -> 400.
  3. POST file > 50 MB          -> 413.
  4. POST unlocked subject      -> 400.
  5. GET  /api/staging/{subj}   -> list, newest first.
  6. GET  /api/staging/{subj}/{id} -> detail with extracted_text.
  7. DELETE /api/staging/{subj}/{id} -> row + file gone.
  8. GET  /upload               -> 200 text/html.

Run: pytest tests/test_upload_api.py -v
"""

import io
import os
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


# --- fixtures --------------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping upload API tests")
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


# --- builders --------------------------------------------------------------
def make_pdf_bytes(text: str = "Hello upload API test") -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    # Plenty of native text so extraction stays off the session-2 OCR path.
    lines = [text] + [
        f"Business studies revision line {i} with extra descriptive words here."
        for i in range(6)
    ]
    page.insert_text((72, 72), "\n".join(lines), fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def insert_staging_row(db, original_name, created_at, *, extracted_text=None,
                       extract_status="pending", subject_id=SUBJECT):
    db.execute(
        """
        INSERT INTO upload_staging
            (subject_id, original_name, stored_path, file_type, file_size_bytes,
             extracted_text, extract_status, status, created_at)
        VALUES (?, ?, ?, 'pdf', ?, ?, ?, 'staged', ?)
        """,
        (subject_id, original_name, f"/tmp/{original_name}", 1234,
         extracted_text, extract_status, created_at),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


# --- tests -----------------------------------------------------------------
def test_post_valid_pdf_returns_staging_id(client, db):
    res = client.post(
        "/api/upload",
        data={"subject_id": SUBJECT},
        files={"file": ("lesson.pdf", make_pdf_bytes(), "application/pdf")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert isinstance(body["staging_id"], int)
    assert body["extract_status"] == "pending"

    # The TestClient runs the background task synchronously -> extraction done.
    row = db.execute(
        "SELECT extract_status, extracted_text FROM upload_staging WHERE staging_id = ?",
        (body["staging_id"],),
    ).fetchone()
    assert row["extract_status"] == "ready"
    assert "Hello upload API test" in row["extracted_text"]


def test_post_xlsx_returns_400(client):
    res = client.post(
        "/api/upload",
        data={"subject_id": SUBJECT},
        files={"file": ("sheet.xlsx", b"junk", "application/vnd.ms-excel")},
    )
    assert res.status_code == 400
    assert res.json()["ok"] is False


def test_post_oversize_returns_413(client):
    big = b"0" * (app_module.MAX_UPLOAD_BYTES + 1)
    res = client.post(
        "/api/upload",
        data={"subject_id": SUBJECT},
        files={"file": ("big.pdf", big, "application/pdf")},
    )
    assert res.status_code == 413
    body = res.json()
    assert body["ok"] is False
    assert "50" in body["error"]


def test_post_unlocked_subject_returns_400(client):
    res = client.post(
        "/api/upload",
        data={"subject_id": "Economics"},   # not a locked subject
        files={"file": ("lesson.pdf", make_pdf_bytes(), "application/pdf")},
    )
    assert res.status_code == 400
    assert res.json()["ok"] is False


def test_get_staging_list(client, db):
    insert_staging_row(db, "first.pdf", "2026-06-01 09:00:00")
    insert_staging_row(db, "second.pdf", "2026-06-02 09:00:00")
    insert_staging_row(db, "third.pdf", "2026-06-03 09:00:00")

    res = client.get(f"/api/staging/{SUBJECT}")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert len(body["items"]) == 3
    # created_at DESC -> newest first.
    assert [it["original_name"] for it in body["items"]] == \
        ["third.pdf", "second.pdf", "first.pdf"]


def test_get_staging_detail_includes_text(client, db):
    sid = insert_staging_row(db, "ready.pdf", "2026-06-03 09:00:00",
                             extracted_text="full extracted body text",
                             extract_status="ready")
    res = client.get(f"/api/staging/{SUBJECT}/{sid}")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["extracted_text"] == "full extracted body text"
    assert body["extract_status"] == "ready"


def test_delete_staging_removes_row_and_file(client, db):
    import uploads
    # Stage a real file so there is something on disk to delete.
    sid = uploads.stage_file(db, SUBJECT, "to_delete.pdf", make_pdf_bytes(), "pdf")
    stored = db.execute(
        "SELECT stored_path FROM upload_staging WHERE staging_id = ?", (sid,)
    ).fetchone()["stored_path"]
    assert os.path.exists(stored)

    res = client.delete(f"/api/staging/{SUBJECT}/{sid}")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    assert db.execute(
        "SELECT COUNT(*) FROM upload_staging WHERE staging_id = ?", (sid,)
    ).fetchone()[0] == 0
    assert not os.path.exists(stored)


def test_get_upload_page_returns_html(client):
    res = client.get("/upload")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
