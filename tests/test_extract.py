"""
tests/test_extract.py
=====================
Unit tests for backend/extract.py -- the notes-upload text extractor.

TXT extraction and mime-type detection run with no external binaries. PDF/DOCX
extraction is exercised end-to-end through the API tests; image OCR needs the
Tesseract binary, so it's not asserted here (the missing-binary path returns a
clear ValueError, covered by the API/manual tests).

Run: pytest tests/test_extract.py -v
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import extract  # noqa: E402


# ---------------------------------------------------------------------------
# extract_text
# ---------------------------------------------------------------------------
def test_extract_text_txt_returns_content(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("A sole trader is a business owned by one person.", encoding="utf-8")
    out = extract.extract_text(str(p), "text/plain")
    assert out == "A sole trader is a business owned by one person."


def test_extract_text_txt_latin1_fallback(tmp_path):
    """A non-utf-8 byte still decodes (latin-1 fallback) instead of crashing."""
    p = tmp_path / "notes.txt"
    p.write_bytes(b"caf\xe9 economics")  # 0xe9 is invalid utf-8, valid latin-1
    out = extract.extract_text(str(p), "text/plain")
    assert "caf" in out and "economics" in out


def test_extract_text_pdf_returns_content(tmp_path):
    """A PDF round-trips through the PyMuPDF extractor (text we wrote comes back)."""
    fitz = pytest.importorskip("fitz")  # PyMuPDF
    p = tmp_path / "notes.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Sole traders bear unlimited liability.")
    doc.save(str(p))
    doc.close()

    out = extract.extract_text(str(p), "application/pdf")
    assert "unlimited liability" in out


def test_extract_text_unsupported_mime_raises(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")
    with pytest.raises(ValueError):
        extract.extract_text(str(p), "application/zip")


# ---------------------------------------------------------------------------
# detect_mime_type
# ---------------------------------------------------------------------------
def test_detect_mime_type_maps_known_extensions():
    assert extract.detect_mime_type("notes.pdf") == "application/pdf"
    assert extract.detect_mime_type("essay.docx") == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert extract.detect_mime_type("notes.txt") == "text/plain"
    assert extract.detect_mime_type("photo.jpg") == "image/jpeg"
    assert extract.detect_mime_type("photo.jpeg") == "image/jpeg"
    assert extract.detect_mime_type("scan.png") == "image/png"


def test_detect_mime_type_is_case_insensitive():
    assert extract.detect_mime_type("NOTES.PDF") == "application/pdf"


def test_detect_mime_type_unsupported_raises():
    with pytest.raises(ValueError):
        extract.detect_mime_type("spreadsheet.xlsx")
