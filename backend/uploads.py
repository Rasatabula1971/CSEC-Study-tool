# PHASE: build
"""
backend/uploads.py
==================
Upload-staging pipeline for the "Upload Material" feature (session 1 of 4).

This is build-phase content preparation -- the student-facing runtime never
touches it. A file dropped in the browser is *staged* (written to the SSD under
06_UPLOAD_STAGING and recorded in the upload_staging table) and its text is
extracted for preview. Nothing is ingested, classified, or embedded here; those
are sessions 2-4.

Public surface:
  * stage_file(db, subject_id, original_name, file_bytes, file_type) -> staging_id
  * extract_text(staging_id, db) -> extracted text (or None on failure)
  * get_staging_list(db, subject_id) -> list[dict]   (no full text -- list view)
  * get_staging_detail(db, staging_id) -> dict | None (full text included)

Extraction is deliberately its own, marker-rich implementation (page markers,
"[Page N - no text]" OCR signals for session 2, table markers, a 500k char cap)
rather than reusing backend/extract.py, whose output is plain joined text for the
notes-classify flow.
"""

import os
import re
from pathlib import Path

MAX_EXTRACT_CHARS = 500_000
TRUNCATION_MARKER = "\n[Truncated: file exceeds 500k char limit]"
VALID_FILE_TYPES = ("pdf", "docx")

# Everything outside this set is replaced with an underscore during sanitisation.
_BAD_CHARS = re.compile(r"[^a-zA-Z0-9._-]")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _staging_root() -> Path:
    """Return {SSD_ROOT}/06_UPLOAD_STAGING. Raises IOError if SSD_ROOT is unset."""
    ssd_root = os.getenv("SSD_ROOT")
    if not ssd_root:
        raise IOError("SSD_ROOT is not set -- cannot locate the upload-staging area.")
    return Path(ssd_root) / "06_UPLOAD_STAGING"


def _subject_is_locked(db, subject_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM subjects WHERE subject_id = ? AND syllabus_locked = 1",
        (subject_id,),
    ).fetchone()
    return row is not None


def safe_filename(original_name: str) -> str:
    """Produce a filesystem-safe filename that can never escape the staging dir.

    Rules:
      * path separators (/ and \\) become underscores
      * anything outside [a-zA-Z0-9._-] becomes an underscore
      * dots inside the stem are collapsed to underscores (kills '..' traversal),
        the single extension dot is preserved
      * extension is lower-cased and kept
      * capped at 100 chars, truncated FROM THE START so the extension survives
    """
    raw = (original_name or "").strip() or "file"
    raw = raw.replace("\\", "_").replace("/", "_")  # strip path separators

    ext = Path(raw).suffix.lower()                  # ".pdf" / ".docx" / "" / ...
    stem = raw[: len(raw) - len(ext)] if ext else raw

    stem = _BAD_CHARS.sub("_", stem)
    stem = stem.replace(".", "_")                   # no dots in the stem -> no '..'
    stem = stem.strip("._-") or "file"

    ext = _BAD_CHARS.sub("_", ext)                  # sanitise but keep the leading dot
    safe = stem + ext

    if len(safe) > 100:
        keep = 100 - len(ext)
        safe = (stem[-keep:] + ext) if keep > 0 else ext[-100:]
    return safe


def _cap(text: str) -> str:
    """Enforce the 500k-char extraction cap with a visible truncation marker."""
    if len(text) > MAX_EXTRACT_CHARS:
        return text[:MAX_EXTRACT_CHARS] + TRUNCATION_MARKER
    return text


# ---------------------------------------------------------------------------
# Extractors  (own marker-rich format -- see module docstring)
# ---------------------------------------------------------------------------
def _extract_pdf(path: str) -> str:
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    try:
        parts = []
        for pno in range(doc.page_count):
            n = pno + 1
            page_text = doc.load_page(pno).get_text("text")
            if page_text.strip():
                parts.append(f"\n[Page {n}]\n")
                parts.append(page_text)
            else:
                # Empty page -> the signal that OCR may be needed (session 2).
                parts.append(f"\n[Page {n} - no text]\n")
        return "".join(parts)
    finally:
        doc.close()


def _extract_docx(path: str) -> str:
    import docx  # python-docx

    document = docx.Document(path)
    body = "\n\n".join(p.text for p in document.paragraphs)

    table_blocks = []
    for table in document.tables:
        rows = []
        for row in table.rows:
            rows.append(" | ".join(cell.text for cell in row.cells))
        table_blocks.append("\n[Table]\n" + "\n".join(rows) + "\n[/Table]\n")

    if table_blocks:
        body = (body + "\n\n" if body else "") + "\n".join(table_blocks)
    return body


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def stage_file(db, subject_id: str, original_name: str, file_bytes: bytes,
               file_type: str) -> int:
    """Validate, write to the SSD, and record one staged file. Returns staging_id.

    The file lands at
        {SSD_ROOT}/06_UPLOAD_STAGING/{subject_id}/{staging_id}_{safe_name}
    The row is inserted first (to mint the autoincrement staging_id), then the
    bytes are written, then stored_path is set -- all in one transaction, so a
    failed SSD write rolls the row back (no orphan row, no orphan file).

    Raises ValueError for an unlocked subject or an unsupported file_type, and
    IOError if the SSD write fails.
    """
    file_type = (file_type or "").lower()
    if file_type not in VALID_FILE_TYPES:
        raise ValueError(
            f"Unsupported file_type '{file_type}'. Expected one of: "
            f"{', '.join(VALID_FILE_TYPES)}."
        )
    if not _subject_is_locked(db, subject_id):
        raise ValueError(f"Subject '{subject_id}' is not a locked subject.")

    safe_name = safe_filename(original_name)
    subject_dir = _staging_root() / subject_id

    cur = db.execute(
        """
        INSERT INTO upload_staging
            (subject_id, original_name, stored_path, file_type, file_size_bytes,
             extract_status, status, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', 'staged', datetime('now'))
        """,
        (subject_id, original_name, "", file_type, len(file_bytes)),
    )
    staging_id = cur.lastrowid
    stored_path = subject_dir / f"{staging_id}_{safe_name}"

    try:
        subject_dir.mkdir(parents=True, exist_ok=True)
        stored_path.write_bytes(file_bytes)
    except OSError as exc:
        db.rollback()  # discard the uncommitted row -- no orphan
        raise IOError(f"Failed to write staged file to {stored_path}: {exc}") from exc

    db.execute(
        "UPDATE upload_staging SET stored_path = ? WHERE staging_id = ?",
        (str(stored_path), staging_id),
    )
    db.commit()
    return staging_id


def extract_text(staging_id: int, db):
    """Extract text from a staged file, driving the extract_status state machine.

    pending -> extracting -> ready (extracted_text populated) | failed
    (extract_error populated, extracted_text left null). Returns the extracted
    text on success, or None if the row is missing or extraction fails.
    """
    row = db.execute(
        "SELECT stored_path, file_type FROM upload_staging WHERE staging_id = ?",
        (staging_id,),
    ).fetchone()
    if row is None:
        return None

    db.execute(
        "UPDATE upload_staging SET extract_status = 'extracting', "
        "updated_at = datetime('now') WHERE staging_id = ?",
        (staging_id,),
    )
    db.commit()

    try:
        if row["file_type"] == "pdf":
            text = _extract_pdf(row["stored_path"])
        elif row["file_type"] == "docx":
            text = _extract_docx(row["stored_path"])
        else:
            raise ValueError(f"Unsupported file_type '{row['file_type']}'.")
        text = _cap(text)
        db.execute(
            "UPDATE upload_staging SET extract_status = 'ready', "
            "extracted_text = ?, extract_error = NULL, "
            "updated_at = datetime('now') WHERE staging_id = ?",
            (text, staging_id),
        )
        db.commit()
        return text
    except Exception as exc:  # noqa: BLE001 -- recorded as a failed extraction
        db.rollback()
        db.execute(
            "UPDATE upload_staging SET extract_status = 'failed', "
            "extract_error = ?, extracted_text = NULL, "
            "updated_at = datetime('now') WHERE staging_id = ?",
            (str(exc)[:1000], staging_id),
        )
        db.commit()
        return None


def get_staging_list(db, subject_id: str) -> list:
    """List view for one subject, newest first. Excludes the full extracted text
    (too large for a list) -- only its length, null until extraction is ready."""
    rows = db.execute(
        """
        SELECT staging_id, original_name, file_type, file_size_bytes,
               extract_status, status, created_at,
               CASE WHEN extracted_text IS NULL THEN NULL
                    ELSE length(extracted_text) END AS extracted_text_length
        FROM   upload_staging
        WHERE  subject_id = ?
        ORDER  BY created_at DESC, staging_id DESC
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_staging_detail(db, staging_id: int):
    """Full row for one staged file, INCLUDING extracted_text. None if absent."""
    row = db.execute(
        """
        SELECT staging_id, subject_id, original_name, file_type, file_size_bytes,
               stored_path, extract_status, extract_error, extracted_text,
               status, created_at
        FROM   upload_staging
        WHERE  staging_id = ?
        """,
        (staging_id,),
    ).fetchone()
    return dict(row) if row else None
