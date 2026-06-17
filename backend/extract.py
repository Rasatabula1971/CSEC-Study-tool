# PHASE: runtime
"""
backend/extract.py
==================
Plain-text extraction for the Welcome-page "Add Study Notes" upload flow.
One entry point per concern:

  * detect_mime_type(filename) -- map a file extension to a mime type, rejecting
    anything we can't read.
  * extract_text(file_path, mime_type) -- pull plain text out of a saved file.

Supported formats (CLAUDE.md keeps the live system thin -- these are the only
note sources the UI offers):

    .pdf        -> PyMuPDF (fitz), every page, joined by newlines
    .docx       -> python-docx, every paragraph, joined by newlines
    .txt        -> direct read (utf-8, latin-1 fallback)
    .jpg/.jpeg  -> Tesseract OCR via pytesseract + Pillow
    .png        -> Tesseract OCR via pytesseract + Pillow

Image OCR needs the Tesseract *binary*, which is a separate install from the
pytesseract Python package. We locate it without relying on PATH (the SSD ships a
bundled copy): TESSERACT_CMD env var, else SSD_ROOT\\Tesseract\\tesseract.exe,
else whatever is on PATH. When none resolves we raise a clear ValueError so the
UI can tell the student to install it -- never a raw stack trace.
"""

import os
from pathlib import Path

# Extension -> mime type. The single source of truth for what we accept; both
# detect_mime_type and the UI's accept="" list derive from these keys.
_EXT_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

_IMAGE_MIMES = {"image/jpeg", "image/png"}

_TESSERACT_HELP = (
    "Photo upload requires Tesseract OCR. Install it from "
    "https://github.com/tesseract-ocr/tesseract"
)


def detect_mime_type(filename: str) -> str:
    """Return the mime type for a filename's extension.

    Raises ValueError for any extension we don't support (the caller turns this
    into a 400, never a 500).
    """
    ext = Path(filename or "").suffix.lower()
    mime = _EXT_MIME.get(ext)
    if mime is None:
        supported = ", ".join(sorted(_EXT_MIME))
        raise ValueError(
            f"Unsupported file type '{ext or filename}'. Supported: {supported}."
        )
    return mime


def _extract_pdf(file_path: str) -> str:
    import fitz  # PyMuPDF -- several times faster than pdfplumber on large PDFs.

    doc = fitz.open(file_path)
    try:
        return "\n".join(
            doc.load_page(pno).get_text("text") for pno in range(doc.page_count)
        )
    finally:
        doc.close()


def _extract_docx(file_path: str) -> str:
    import docx  # python-docx

    document = docx.Document(file_path)
    return "\n".join(p.text for p in document.paragraphs)


def _extract_txt(file_path: str) -> str:
    data = Path(file_path).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def _extract_image(file_path: str) -> str:
    """OCR an image with Tesseract. Clear ValueError if the toolchain is absent."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise ValueError(_TESSERACT_HELP) from exc

    _configure_tesseract(pytesseract)

    try:
        with Image.open(file_path) as img:
            return pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError as exc:
        # pytesseract is installed but the tesseract binary couldn't be located.
        raise ValueError(_TESSERACT_HELP) from exc


def _resolve_tesseract_cmd() -> str | None:
    """Locate the tesseract binary, drive-letter-agnostic (CLAUDE.md SSD rule).

    Order: explicit TESSERACT_CMD, then the SSD's bundled copy under
    SSD_ROOT\\Tesseract\\tesseract.exe, then None (let pytesseract try PATH).
    """
    explicit = os.getenv("TESSERACT_CMD")
    if explicit and Path(explicit).is_file():
        return explicit
    ssd_root = os.getenv("SSD_ROOT")
    if ssd_root:
        bundled = Path(ssd_root) / "Tesseract" / "tesseract.exe"
        if bundled.is_file():
            return str(bundled)
    return None


def _configure_tesseract(pytesseract) -> None:
    """Point pytesseract at the resolved binary and its tessdata, if we found one.

    No-op when nothing resolves -- pytesseract then falls back to PATH and, if
    that also fails, raises TesseractNotFoundError (mapped to the help message).
    """
    cmd = _resolve_tesseract_cmd()
    if not cmd:
        return
    pytesseract.pytesseract.tesseract_cmd = cmd
    # Tesseract 5 reads language data from TESSDATA_PREFIX, which for this build
    # must be the tessdata directory itself (it appends "<lang>.traineddata"
    # directly). Only set it if the user hasn't overridden it.
    tessdata = Path(cmd).parent / "tessdata"
    if tessdata.is_dir():
        os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))


def extract_text(file_path: str, mime_type: str) -> str:
    """Extract plain text from a saved file, dispatched on mime type.

    Raises ValueError for an unsupported mime type, or (for images) when the
    Tesseract toolchain is unavailable.
    """
    if mime_type == "application/pdf":
        return _extract_pdf(file_path)
    if mime_type == _EXT_MIME[".docx"]:
        return _extract_docx(file_path)
    if mime_type == "text/plain":
        return _extract_txt(file_path)
    if mime_type in _IMAGE_MIMES:
        return _extract_image(file_path)
    raise ValueError(f"Unsupported mime type '{mime_type}'.")
