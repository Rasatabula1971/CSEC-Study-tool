"""
tests/test_uploads_ocr.py
=========================
Upload session 2 -- unit tests for the OCR-fallback + chunking extractors in
backend/uploads.py.

Tesseract is MOCKED throughout (monkeypatching pytesseract.image_to_data) so the
tests are deterministic and don't depend on the bundled binary. fitz is real for
the page-rendering tests, mocked for the very-long-text chunking test. The DB-side
tests use a real in-memory SQLite (schema.sql + apply_runtime_migrations) with
SSD_ROOT pointed at a tempdir.

  1. scanned-only PDF -> full-file OCR (every page).
  2. mixed PDF -> page-level OCR only on the empty page.
  3. very long native text -> truncated + chunks (<=100k each).
  4. _extract_image -> single-page OCR dict.
  5. _ocr_page with zero detected words -> ('', None), no ZeroDivision.
  6. extract_text on a truncated result writes upload_staging_chunks rows.
  7. extract_text on a non-truncated result writes no chunks.

Run: pytest tests/test_uploads_ocr.py -v
"""

import io
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module   # noqa: E402
import uploads             # noqa: E402

SUBJECT = "Principles_of_Business"


# --- mock plumbing ---------------------------------------------------------
def _fake_image_to_data(words, confs):
    """Build a pytesseract.image_to_data DICT-style return."""
    def _inner(img, lang=None, output_type=None):
        return {"text": list(words), "conf": list(confs)}
    return _inner


@pytest.fixture(autouse=True)
def _no_real_ocr_config(monkeypatch):
    # Skip pointing pytesseract at the real binary -- the call itself is mocked.
    monkeypatch.setattr(uploads, "_ensure_ocr", lambda: None)


# --- builders --------------------------------------------------------------
def blank_pdf_bytes(n_pages: int) -> bytes:
    import fitz
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()  # no text layer
    data = doc.tobytes()
    doc.close()
    return data


def mixed_pdf_bytes() -> bytes:
    """3 pages: 1 and 3 have ample native text, 2 is blank."""
    import fitz
    doc = fitz.open()
    long_lines = "\n".join(f"Real native content line {i} on this page." for i in range(8))
    p1 = doc.new_page(); p1.insert_text((72, 72), long_lines, fontsize=11)
    doc.new_page()  # blank page 2
    p3 = doc.new_page(); p3.insert_text((72, 72), long_lines, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


# --- tests -----------------------------------------------------------------
def test_scanned_only_pdf_triggers_full_file_ocr(monkeypatch, tmp_path):
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_data",
                        _fake_image_to_data(["HELLO", "WORLD"], ["90", "80"]))
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(blank_pdf_bytes(2))

    result = uploads._extract_pdf(str(pdf))

    assert result["ocr_pages"] == [1, 2]          # every page OCR'd
    assert result["total_pages"] == 2
    assert result["ocr_confidence_avg"] == 85     # mean of [90,80] per page
    assert "[Page 1 - OCR]" in result["text"]
    assert "HELLO WORLD" in result["text"]


def test_mixed_pdf_triggers_page_level_ocr(monkeypatch, tmp_path):
    import pytesseract
    monkeypatch.setattr(pytesseract, "image_to_data",
                        _fake_image_to_data(["PAGE", "TWO", "OCR"], ["75", "75", "75"]))
    pdf = tmp_path / "mixed.pdf"
    pdf.write_bytes(mixed_pdf_bytes())

    result = uploads._extract_pdf(str(pdf))

    assert result["ocr_pages"] == [2]             # only the blank page
    assert "[Page 1]" in result["text"]
    assert "[Page 2 - OCR]" in result["text"]
    assert "[Page 3]" in result["text"]
    assert "PAGE TWO OCR" in result["text"]


def test_long_native_text_produces_chunks(monkeypatch):
    import fitz

    class FakePage:
        def __init__(self, txt): self._t = txt
        def get_text(self, _): return self._t
        def get_pixmap(self, dpi=300): raise AssertionError("no OCR expected for long text")

    class FakeDoc:
        def __init__(self, pages): self._p = pages; self.page_count = len(pages)
        def load_page(self, i): return self._p[i]
        def close(self): pass

    monkeypatch.setattr(fitz, "open", lambda path: FakeDoc([FakePage("x" * 600_000)]))

    result = uploads._extract_pdf("ignored.pdf")

    assert result["truncated"] is True
    assert isinstance(result["chunks"], list)
    assert all(len(c) <= uploads.CHUNK_SIZE for c in result["chunks"])
    # chunks hold the FULL text (page marker + 600k chars), >= the native length
    assert sum(len(c) for c in result["chunks"]) >= 600_000
    # the stored preview is capped at 500k + the marker
    assert len(result["text"]) == uploads.MAX_EXTRACT_CHARS + len(uploads.TRUNCATE_PREVIEW_MARKER)


def test_extract_image_runs_tesseract(monkeypatch, tmp_path):
    import pytesseract
    from PIL import Image
    monkeypatch.setattr(pytesseract, "image_to_data",
                        _fake_image_to_data(["SCANNED", "NOTE"], ["88", "92"]))
    img_path = tmp_path / "note.png"
    Image.new("RGB", (40, 20), "white").save(img_path)

    result = uploads._extract_image(str(img_path))

    assert result["text"] == "SCANNED NOTE"
    assert result["ocr_confidence_avg"] == 90
    assert result["total_pages"] == 1
    assert result["ocr_pages"] == [1]
    assert result["truncated"] is False


def test_ocr_page_handles_zero_words(monkeypatch, tmp_path):
    import pytesseract
    import fitz
    monkeypatch.setattr(pytesseract, "image_to_data",
                        _fake_image_to_data(["", "  "], ["-1", "-1"]))
    doc = fitz.open(); page = doc.new_page()
    try:
        text, conf = uploads._ocr_page(page)
    finally:
        doc.close()
    assert text == ""
    assert conf is None   # no division by zero


# --- DB-side: chunk persistence -------------------------------------------
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


def test_extract_text_truncated_writes_chunks(db, monkeypatch):
    sid = uploads.stage_file(db, SUBJECT, "huge.pdf", b"%PDF-fake", "pdf")
    monkeypatch.setattr(uploads, "_extract_pdf", lambda path: {
        "text": "preview" + uploads.TRUNCATE_PREVIEW_MARKER,
        "total_pages": 10,
        "ocr_pages": [1, 2],
        "ocr_confidence_avg": 55,
        "truncated": True,
        "chunks": ["chunk-%d" % i for i in range(7)],
    })

    uploads.extract_text(sid, db)

    assert uploads.count_chunks(db, sid) == 7
    row = db.execute(
        "SELECT extract_status, truncated, ocr_used, ocr_pages_count, ocr_confidence_avg "
        "FROM upload_staging WHERE staging_id = ?", (sid,)
    ).fetchone()
    assert row["extract_status"] == "ready"
    assert row["truncated"] == 1
    assert row["ocr_used"] == 1
    assert row["ocr_pages_count"] == 2
    assert row["ocr_confidence_avg"] == 55


def test_extract_text_no_truncation_writes_no_chunks(db, monkeypatch):
    sid = uploads.stage_file(db, SUBJECT, "small.pdf", b"%PDF-fake", "pdf")
    monkeypatch.setattr(uploads, "_extract_pdf", lambda path: {
        "text": "short text",
        "total_pages": 2,
        "ocr_pages": [],
        "ocr_confidence_avg": None,
        "truncated": False,
        "chunks": None,
    })

    uploads.extract_text(sid, db)

    assert uploads.count_chunks(db, sid) == 0
    row = db.execute(
        "SELECT truncated, ocr_used FROM upload_staging WHERE staging_id = ?", (sid,)
    ).fetchone()
    assert row["truncated"] == 0
    assert row["ocr_used"] == 0
