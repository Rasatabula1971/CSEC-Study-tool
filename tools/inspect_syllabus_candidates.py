# PHASE: build — called only during ingestion prep (read-only inspection)
"""
Surface the real CXC/CSEC Integrated Science syllabus from the intake triage.

Reads the existing GPT_Folder_CSEC_full_triage.csv, narrows to Integrated Science
real documents (category != unknown), and prints text previews for:
  * every filename-guessed 'syllabus' row, and
  * every 'unclear' row (the filename guesser's leftovers),

so the builder can eyeball which file is actually the official syllabus before
building objectives. As a safety net it also scans the unclear rows' extracted
text for CXC / CSEC / syllabus / SYLL and flags those at the top — the syllabus
the filename guesser missed.

Read-only: nothing is moved, modified, or written to disk. Output is printed.

  python tools/inspect_syllabus_candidates.py
"""

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("ERROR: PyMuPDF (fitz) is not installed. Run: pip install pymupdf")

load_dotenv()

# PDFs embed private-use / non-cp1252 glyphs; keep the Windows console from
# crashing on them rather than losing the whole report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PREVIEW_CHARS = 300          # how much text to print per file
SCAN_CHARS = 2000            # how much text to extract for keyword scanning
KEYWORDS = ("cxc", "csec", "syllabus", "syll")   # lowercased; scanned case-insensitively

SUBJECT = "Integrated_Science"


def _report_csv() -> Path:
    root = os.getenv("REPORTS_ROOT")
    if not root:
        sys.exit("ERROR: REPORTS_ROOT not set in .env")
    path = Path(root) / "GPT_Folder_CSEC_full_triage.csv"
    if not path.exists():
        sys.exit(f"ERROR: triage CSV not found: {path}\nRun tools/sort_gpt_folder.py --full first.")
    return path


def _extract_text(path: Path, category: str, limit: int) -> str:
    """Best-effort text extraction. Returns '' (image_pdf or any failure)."""
    if category == "image_pdf":
        return ""  # scanned — no OCR at this stage
    try:
        if category == "text_pdf":
            doc = fitz.open(path)
            try:
                parts = []
                for page in doc:
                    parts.append(page.get_text("text"))
                    if sum(len(p) for p in parts) >= limit:
                        break
            finally:
                doc.close()
            return "".join(parts)[:limit]
        if category == "word_doc":
            return _extract_docx(path, limit)
    except Exception as exc:
        return f"[extraction failed: {exc}]"
    return ""


def _extract_docx(path: Path, limit: int) -> str:
    if path.suffix.lower() != ".docx":
        return "[.doc (legacy Word) — no extractor; inspect manually]"
    try:
        import docx  # python-docx
    except ImportError:
        return "[python-docx not installed — cannot preview .docx]"
    document = docx.Document(str(path))
    parts = []
    for para in document.paragraphs:
        if para.text.strip():
            parts.append(para.text)
        if sum(len(p) for p in parts) >= limit:
            break
    return "\n".join(parts)[:limit]


def _clean(text: str) -> str:
    """Collapse whitespace so a 300-char preview is dense and readable."""
    return " ".join(text.split())


def _load_rows(csv_path: Path) -> list[dict]:
    with open(csv_path, "r", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _print_row(idx: int, total: int, row: dict, preview: str) -> None:
    path = Path(row["file_path"])
    print(f"\n[{idx}/{total}] {path.name}")
    print(f"    path:     {row['file_path']}")
    print(f"    category: {row['category']}   pages: {row['page_count'] or '-'}   "
          f"chars: {row['extractable_text_chars'] or '-'}")
    if row["category"] == "image_pdf":
        print("    preview:  (scanned image PDF — page_count only, no OCR yet)")
    else:
        snippet = _clean(preview)[:PREVIEW_CHARS]
        print(f"    preview:  {snippet if snippet else '(no extractable text)'}")


def main() -> None:
    csv_path = _report_csv()
    rows = _load_rows(csv_path)

    isci = [r for r in rows
            if r["guessed_subject"] == SUBJECT and r["category"] != "unknown"]
    syllabus_rows = [r for r in isci if r["guessed_doc_type"] == "syllabus"]
    unclear_rows = [r for r in isci if r["guessed_doc_type"] == "unclear"]

    print("=" * 78)
    print(f"Integrated Science syllabus inspection — source: {csv_path}")
    print(f"Real ISCI documents (category != unknown): {len(isci)}")
    print(f"  guessed_doc_type = syllabus : {len(syllabus_rows)}")
    print(f"  guessed_doc_type = unclear  : {len(unclear_rows)}")
    print("=" * 78)

    # Pre-extract text once per unclear row so we can both scan + preview it.
    unclear_text = {id(r): _extract_text(Path(r["file_path"]), r["category"], SCAN_CHARS)
                    for r in unclear_rows}

    # --- TOP: missed syllabus candidates (keyword hits among unclear rows) ---
    flagged = []
    for r in unclear_rows:
        text = unclear_text[id(r)]
        low = text.lower()
        hits = [kw for kw in KEYWORDS if kw in low]
        if hits:
            flagged.append((r, hits, text))

    print("\n" + "#" * 78)
    print(f"# MISSED SYLLABUS CANDIDATES — 'unclear' rows mentioning {KEYWORDS}")
    print(f"# (filename guesser missed these; {len(flagged)} hit)")
    print("#" * 78)
    if not flagged:
        print("\n(none — no unclear ISCI document mentions CXC/CSEC/syllabus/SYLL "
              "in its first text)")
    for i, (r, hits, text) in enumerate(flagged, 1):
        print(f"\n>>> HIT {i}/{len(flagged)} — keywords: {', '.join(hits)}")
        _print_row(i, len(flagged), r, text)

    # --- SECTION A: guessed_doc_type = syllabus ---
    print("\n\n" + "=" * 78)
    print(f"SECTION A — guessed_doc_type = syllabus  ({len(syllabus_rows)} rows)")
    print("=" * 78)
    for i, r in enumerate(syllabus_rows, 1):
        preview = _extract_text(Path(r["file_path"]), r["category"], SCAN_CHARS)
        _print_row(i, len(syllabus_rows), r, preview)

    # --- SECTION B: guessed_doc_type = unclear ---
    print("\n\n" + "=" * 78)
    print(f"SECTION B — guessed_doc_type = unclear  ({len(unclear_rows)} rows)")
    print("=" * 78)
    for i, r in enumerate(unclear_rows, 1):
        _print_row(i, len(unclear_rows), r, unclear_text[id(r)])

    print("\n" + "=" * 78)
    print(f"Done. {len(flagged)} missed-candidate hit(s), "
          f"{len(syllabus_rows)} syllabus-guessed, {len(unclear_rows)} unclear inspected.")
    print("=" * 78)


if __name__ == "__main__":
    main()
