# PHASE: build
"""
backend/ocr_utils.py
====================
Shared Tesseract OCR primitives for build-time text extraction -- the single
source of truth for locating the Tesseract binary, choosing a render DPI that
stays under Pillow's decompression-bomb guard, and OCRing a PyMuPDF page or a
PIL image.

Consumers: backend/uploads.py (the Upload Material extractor) and the ingest_v2
adapters (moe_slms, and later generic_pdf). Extracted from uploads.py so every
caller shares ONE OCR path -- including the decompression-bomb DPI guard -- rather
than each re-implementing it (moe_slms previously had a guard-less copy).

Tesseract is located via the same drive-letter-agnostic resolver extract.py uses
(TESSERACT_CMD env -> SSD-bundled binary -> PATH).
"""

import io
import logging
import math

logger = logging.getLogger("csec.ocr")

OCR_LANG = "eng"
OCR_DPI = 300          # render a PDF page to image at this DPI before OCR
# Pillow's default Image.MAX_IMAGE_PIXELS decompression-bomb guard. Some CXC PDFs have
# oversized pages / embedded high-res images that, rendered at OCR_DPI, exceed this and
# make Pillow refuse to open the image. We keep the guard and lower our render DPI to fit.
PIL_PIXEL_LIMIT = 178_956_970

# A PDF page whose native (PyMuPDF) text is below this many characters is treated as
# image-only and OCR'd. Shared by the ingest_v2 PDF-page OCR call sites (moe_slms and
# generic_pdf's opt-in path) so they don't each carry their own arbitrary number.
# NOTE: uploads.py deliberately does NOT use this -- its student-upload extractor has a
# higher, purpose-tuned per-page threshold paired with a whole-file-average mode.
OCR_TRIGGER_THRESHOLD = 30

# Set once Tesseract has been pointed at its binary (idempotent -- see ensure_tesseract).
_ocr_configured = False


def ensure_tesseract() -> None:
    """Point pytesseract at its binary, once. Uses extract.py's drive-letter-agnostic
    resolver (TESSERACT_CMD -> SSD-bundled -> PATH). Best-effort: if extract or the
    binary can't be resolved we leave pytesseract on PATH, and the OCR call itself
    raises a clear error that the caller records as a failed extraction."""
    global _ocr_configured
    if _ocr_configured:
        return
    try:
        import pytesseract
        import extract  # same resolver as the notes-OCR flow -- single source of truth
        extract._configure_tesseract(pytesseract)
    except Exception:  # noqa: BLE001 -- fall back to PATH; OCR will surface any failure
        pass
    _ocr_configured = True


def _conf_int(c) -> int:
    """Tesseract per-word confidence -> int. image_to_data returns these as strings
    (sometimes floats); -1 means 'no confidence' and is filtered out by callers."""
    try:
        return int(float(c))
    except (ValueError, TypeError):
        return -1


def ocr_image(img) -> tuple:
    """Run Tesseract on a PIL image. Returns (text, mean_confidence|None)."""
    import pytesseract
    data = pytesseract.image_to_data(
        img, lang=OCR_LANG, output_type=pytesseract.Output.DICT
    )
    words = [w for w in data["text"] if w.strip()]
    confs = [v for v in (_conf_int(c) for c, w in zip(data["conf"], data["text"])
                         if w.strip()) if v != -1]
    text = " ".join(words)
    mean_conf = int(sum(confs) / len(confs)) if confs else None
    return text, mean_conf


def render_dpi(page) -> tuple:
    """Choose the render DPI for a page so the rasterised image stays under Pillow's
    decompression-bomb guard. Returns (dpi, reduced): `reduced` is True when the page is
    so large that OCR_DPI would exceed 90% of PIL_PIXEL_LIMIT (10% headroom for Pillow
    internals), in which case DPI is scaled down proportionally (sqrt of the area ratio),
    floored at 72. The single source of truth for the DPI decision -- both ocr_page and
    uploads._extract_pdf consult it (the tuple contract of ocr_page stays unchanged)."""
    rect = page.rect
    # page.rect is in points (72 per inch); pixels = points * DPI / 72.
    width_px = rect.width * OCR_DPI / 72
    height_px = rect.height * OCR_DPI / 72
    pixel_count = width_px * height_px
    if pixel_count > PIL_PIXEL_LIMIT * 0.9:
        ratio = (PIL_PIXEL_LIMIT * 0.9) / pixel_count
        scale = math.sqrt(ratio)
        return max(72, int(OCR_DPI * scale)), True
    return OCR_DPI, False


def ocr_page(page) -> tuple:
    """Render a PyMuPDF page to a PNG and OCR it. Returns (text, mean_conf). The render
    DPI is reduced for oversized pages so Pillow's guard never trips (logged when it
    happens); the (text, conf) contract is unchanged."""
    ensure_tesseract()
    from PIL import Image
    target_dpi, reduced = render_dpi(page)
    if reduced:
        rect = page.rect
        logger.info(
            "Reducing OCR DPI for oversized page: DPI %d (page rect %.0f×%.0f pt, "
            "%dpx at %d DPI)",
            target_dpi, rect.width, rect.height,
            int(rect.width * OCR_DPI / 72 * rect.height * OCR_DPI / 72), OCR_DPI,
        )
    pix = page.get_pixmap(dpi=target_dpi)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    try:
        return ocr_image(img)
    finally:
        img.close()
