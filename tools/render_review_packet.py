"""
tools/render_review_packet.py
===============================
PHASE: build

Stage 2 review aid for the mark-scheme extraction pipeline (see
MARK_SCHEME_BUILD_PLAN.md). Pairs extracted CSV rows with the actual syllabus
PDF page they came from, so manual verification ("compare point_text against
the actual mark scheme on that page") doesn't require flipping between a CSV
and a PDF viewer by hand.

Renders the requested PDF pages to PNG and builds one static HTML file with,
for each page, the page image next to a table of every CSV row whose
source_page matches that page.

Writes NOTHING to the CSV or any DB table -- read-only review aid.

Usage:
    python tools/render_review_packet.py \\
        --csv-file  E:\\...\\Economics_mark_scheme_review.csv \\
        --pdf-file  E:\\...\\csec-economics-syllabus-revised-2017.pdf \\
        --pages     "90-97,117-126" \\
        --output-dir E:\\...\\review_packets\\economics
"""

import argparse
import csv
import html
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
    _FITZ_AVAILABLE = True
except ImportError:
    _FITZ_AVAILABLE = False

DEFAULT_DPI = 150

# Columns shown in the review table, in display order.
_TABLE_COLUMNS = [
    "question_num", "question_part", "point_text", "marks_value",
    "verified", "excluded_reason", "parser_artifact",
]

_COLUMN_LABELS = {
    "question_num":     "Q#",
    "question_part":    "Part",
    "point_text":       "Point text",
    "marks_value":       "Marks",
    "verified":         "Verified",
    "excluded_reason":  "Excluded reason",
    "parser_artifact":  "Artifact",
}


# ── Page-list parsing ────────────────────────────────────────────────────────
def parse_pages_arg(pages_str: str) -> list[int]:
    """Parse '107-111,126,128' into a sorted, de-duplicated list of page numbers."""
    pages: set[int] = set()
    for token in pages_str.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
            if end < start:
                start, end = end, start
            pages.update(range(start, end + 1))
        else:
            pages.add(int(token))
    return sorted(pages)


# ── CSV loading / filtering ──────────────────────────────────────────────────
def read_csv_rows(csv_path: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def filter_rows_for_page(rows: list[dict], page: int) -> list[dict]:
    """Return rows whose source_page equals `page` (int comparison).

    Rows with an empty or non-numeric source_page are silently excluded from
    every page's table -- they have nothing to be paired with here. (Stage 3's
    lock script separately refuses to lock such rows at all -- see Rule 2,
    tools/lock_mark_scheme.py's check_null_source_pages.)
    """
    matched = []
    for r in rows:
        sp = (r.get("source_page") or "").strip()
        if not sp:
            continue
        try:
            sp_int = int(sp)
        except ValueError:
            continue
        if sp_int == page:
            matched.append(r)
    return matched


# ── PDF rendering ────────────────────────────────────────────────────────────
def render_page_images(pdf_path: str, pages: list[int], output_dir: Path,
                       dpi: int = DEFAULT_DPI) -> dict[int, Path]:
    """Render each page in `pages` to a PNG under {output_dir}/pages/.

    Returns {page_num: png_path}. A page number outside the PDF's range is
    skipped with a printed warning, not raised -- a typo in --pages should
    not blow up an otherwise-good review packet.
    """
    if not _FITZ_AVAILABLE:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")

    doc = fitz.open(pdf_path)
    doc_len = len(doc)

    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    image_paths: dict[int, Path] = {}
    for page_num in pages:
        if page_num < 1 or page_num > doc_len:
            print(f"  WARNING: page {page_num} is out of range for {pdf_path} "
                  f"({doc_len} pages) -- skipped.")
            continue
        page = doc[page_num - 1]
        pix = page.get_pixmap(dpi=dpi)
        out_path = pages_dir / f"page_{page_num}.png"
        pix.save(str(out_path))
        image_paths[page_num] = out_path

    return image_paths


# ── HTML generation ───────────────────────────────────────────────────────────
def _row_to_cells(row: dict) -> list[str]:
    return [html.escape(str(row.get(c, "")).strip()) for c in _TABLE_COLUMNS]


def _build_page_section(page: int, rows: list[dict], image_paths: dict[int, Path]) -> str:
    page_rows = filter_rows_for_page(rows, page)
    img_path = image_paths.get(page)

    if img_path is not None:
        img_html = f'<img src="pages/{html.escape(img_path.name)}" alt="Page {page}">'
    else:
        img_html = f'<p class="missing-image">Page {page} image not available.</p>'

    header_cells = "".join(
        f"<th>{html.escape(_COLUMN_LABELS[c])}</th>" for c in _TABLE_COLUMNS
    )

    if page_rows:
        body_rows = "\n".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in _row_to_cells(r)) + "</tr>"
            for r in page_rows
        )
    else:
        body_rows = ""

    return f"""
  <section class="page-section" id="page-{page}">
    <h2>Page {page}</h2>
    <div class="page-row">
      <div class="page-image">{img_html}</div>
      <div class="page-table">
        <table>
          <thead><tr>{header_cells}</tr></thead>
          <tbody>{body_rows}</tbody>
        </table>
        <p class="row-count">{len(page_rows)} row(s) on this page.</p>
      </div>
    </div>
  </section>"""


def build_html(pages: list[int], rows: list[dict], image_paths: dict[int, Path],
               subject_label: str = "") -> str:
    """Build the full static review.html document.

    Emits exactly one <section class="page-section"> per entry in `pages`, in
    the order given -- including pages with zero matching CSV rows (the image
    still renders, the table is just empty).
    """
    sections = "\n".join(_build_page_section(p, rows, image_paths) for p in pages)

    title = "Mark Scheme Review Packet"
    if subject_label:
        title = f"{title} — {subject_label}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 24px;
          background: #f7f7f9; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin: 0 0 12px; }}
  .page-section {{ background: #fff; border: 1px solid #ddd; border-radius: 6px;
                    padding: 16px; margin-bottom: 28px; }}
  .page-row {{ display: flex; gap: 20px; align-items: flex-start; flex-wrap: wrap; }}
  .page-image {{ flex: 0 0 420px; max-width: 420px; }}
  .page-image img {{ width: 100%; border: 1px solid #999; display: block; }}
  .missing-image {{ color: #a00; font-style: italic; }}
  .page-table {{ flex: 1 1 480px; min-width: 320px; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #2E75B6; color: #fff; position: sticky; top: 0; }}
  tr:nth-child(even) td {{ background: #fafafa; }}
  .row-count {{ font-size: 0.8rem; color: #555; margin-top: 6px; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
{sections}
</body>
</html>
"""


# ── Orchestration ─────────────────────────────────────────────────────────────
def write_review_packet(csv_file: str, pdf_file: str, pages_str: str, output_dir: str,
                        *, dpi: int = DEFAULT_DPI) -> Path:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pages = parse_pages_arg(pages_str)
    if not pages:
        sys.exit("ERROR: --pages produced an empty page list.")

    rows = read_csv_rows(csv_file)
    image_paths = render_page_images(pdf_file, pages, out_dir, dpi=dpi)

    subject_label = Path(csv_file).stem.replace("_mark_scheme_review", "")
    html_doc = build_html(pages, rows, image_paths, subject_label=subject_label)

    review_path = out_dir / "review.html"
    review_path.write_text(html_doc, encoding="utf-8")
    return review_path


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render a side-by-side review packet: syllabus PDF page "
                     "images paired with their extracted mark-scheme CSV rows."
    )
    ap.add_argument("--csv-file", required=True,
                    help="Path to the {subject}_mark_scheme_review.csv")
    ap.add_argument("--pdf-file", required=True,
                    help="Path to the source syllabus PDF")
    ap.add_argument("--pages", required=True,
                    help="Pages to render, e.g. '107-111,126,128'")
    ap.add_argument("--output-dir", required=True,
                    help="Directory to write pages/ and review.html into")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                    help=f"Render DPI (default {DEFAULT_DPI})")
    args = ap.parse_args()

    if not Path(args.csv_file).exists():
        sys.exit(f"ERROR: CSV not found: {args.csv_file}")
    if not Path(args.pdf_file).exists():
        sys.exit(f"ERROR: PDF not found: {args.pdf_file}")

    review_path = write_review_packet(
        args.csv_file, args.pdf_file, args.pages, args.output_dir, dpi=args.dpi
    )
    print(f"Review packet written: {review_path}")


if __name__ == "__main__":
    main()
