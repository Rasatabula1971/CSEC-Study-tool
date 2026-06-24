# PHASE: build — called only during ingestion prep (intake triage, read-only)
"""
Intake triage for a mixed folder of subject source material.

Walks a source folder recursively and, for every file, classifies it by
extension + content, guesses its document type, and guesses its subject — all
from cheap, deterministic filename / path keywords. NOTHING is moved, renamed,
modified, or ingested — this is a read-only report that the builder reviews
before any manual sorting into the knowledge base.

  python tools/sort_gpt_folder.py --source "D:\\GPT Folder CSEC" --subject Integrated_Science

Categories (by extension + content):
  word_doc   .docx / .doc
  text_pdf   .pdf with extractable text (avg chars/page above threshold)
  image_pdf  .pdf that is scanned / image-only (near-zero extractable text — needs OCR)
  unknown    anything else (its real extension is recorded in file_ext)

Guessed doc type (filename keywords only — no LLM, falls back to 'unclear'):
  syllabus | specimen_paper | past_paper | mark_scheme | notes | unclear

Guessed subject (filename + parent-path keywords only — no LLM, falls back to
'unclear'): the seven CSEC subject ids.

Output: {REPORTS_ROOT}\\{Subject}_intake_triage.csv  (per-subject runs), or
        {REPORTS_ROOT}\\GPT_Folder_CSEC_full_triage.csv when --full is passed.
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("ERROR: PyMuPDF (fitz) is not installed. Run: pip install pymupdf")

load_dotenv()

# A page with at least this many extractable chars counts as a real text page.
# Mirrors backend/uploads.py PAGE_TEXT_THRESHOLD so the two stay consistent.
PAGE_TEXT_THRESHOLD = 50
# Whole-file average chars/page below this -> treat as scanned/image-only PDF.
FILE_AVG_THRESHOLD = 100

WORD_EXTS = {".docx", ".doc"}
PDF_EXTS = {".pdf"}

# Filename keyword -> doc type. Checked in order; first hit wins. Patterns are
# deliberately broad-but-cheap; ambiguity falls back to 'unclear' rather than guessing.
DOC_TYPE_PATTERNS = [
    ("syllabus", [r"\bsyllabus\b", r"\bspecification\b", r"\bsyll\b", r"\bcurriculum\b"]),
    ("mark_scheme", [r"mark\s*scheme", r"\bms\b", r"marking\s*scheme", r"\banswers?\b",
                     r"\bsolutions?\b", r"worked\b"]),
    ("specimen_paper", [r"\bspecimen\b", r"\bsample\s*paper\b", r"\bspec\b"]),
    ("past_paper", [r"past\s*paper", r"\bpaper\s*[0-9]\b", r"\bp[123]\b",
                    r"\bquestion\s*paper\b", r"\bexam\b",
                    r"\b(jan(uary)?|may|june|jun)\s*20[0-9]{2}\b",
                    r"\b20[0-9]{2}\b"]),
    ("notes", [r"\bnotes?\b", r"\blesson\b", r"\bchapter\b", r"\bunit\b",
               r"\bnotes?\b", r"\bsummary\b", r"\brevision\b", r"\bstudy\b",
               r"\bguide\b", r"\bworkbook\b", r"\btextbook\b", r"\bbook\b"]),
]


def guess_doc_type(filename: str) -> str:
    """Cheap, deterministic doc-type guess from the filename only."""
    name = filename.lower()
    for doc_type, patterns in DOC_TYPE_PATTERNS:
        for pat in patterns:
            if re.search(pat, name):
                return doc_type
    return "unclear"


# Subject id -> path keyword regexes. Checked in order; first hit wins. Matched
# against a NORMALISED full path (lowercased, separators/underscores/hyphens/dots
# collapsed to single spaces) so " it " and multi-word phrases match regardless of
# the original separator. Word boundaries guard the short abbreviations.
SUBJECT_PATTERNS = [
    ("Principles_of_Business", [r"\bpob\b", r"\bbusiness\b", r"principles of business"]),
    ("Economics", [r"\beconomics\b", r"\becon\b"]),
    ("Mathematics", [r"\bmaths\b", r"\bmath\b"]),
    ("Principles_of_Accounts", [r"\bpoa\b", r"\baccounts\b", r"principles of accounts"]),
    ("Integrated_Science", [r"integrated science", r"\bscience\b",
                            r"\bbio\b", r"\bchem\b", r"\bphys\b"]),
    ("Information_Technology", [r"information technology", r"\bict\b", r" it "]),
    ("English", [r"\benglish\b", r"\beng\b"]),
]


def _normalise_path(full_path: str) -> str:
    """Lowercase; collapse \\ / _ - . and runs of whitespace to single spaces.
    Padded with leading/trailing spaces so ' it ' matches at the ends too."""
    flat = re.sub(r"[\\/_.\-]+", " ", full_path.lower())
    flat = re.sub(r"\s+", " ", flat).strip()
    return f" {flat} "


def guess_subject(full_path: str) -> str:
    """Cheap, deterministic subject guess from the whole path (filename + folders)."""
    norm = _normalise_path(full_path)
    for subject, patterns in SUBJECT_PATTERNS:
        for pat in patterns:
            if re.search(pat, norm):
                return subject
    return "unclear"


def file_ext_label(path: Path) -> str:
    """The file's real extension (lowercased, e.g. '.html'), or 'no_extension'."""
    return path.suffix.lower() if path.suffix else "no_extension"


def classify_pdf(path: Path) -> tuple[str, int, int, str]:
    """
    Open a PDF and decide text_pdf vs image_pdf.

    Returns (category, page_count, extractable_text_chars, notes).
    A PDF that fails to open is reported as 'unknown' with the error in notes.
    """
    try:
        doc = fitz.open(path)
    except Exception as exc:  # corrupt / password-protected / not really a PDF
        return "unknown", 0, 0, f"pdf open failed: {exc}"

    try:
        page_count = doc.page_count
        total_chars = 0
        text_pages = 0
        for page in doc:
            txt = page.get_text("text").strip()
            total_chars += len(txt)
            if len(txt) >= PAGE_TEXT_THRESHOLD:
                text_pages += 1
    finally:
        doc.close()

    if page_count == 0:
        return "unknown", 0, total_chars, "pdf has 0 pages"

    avg_chars = total_chars / page_count
    if avg_chars < FILE_AVG_THRESHOLD:
        notes = (f"avg {avg_chars:.0f} chars/page, {text_pages}/{page_count} text pages "
                 f"-> scanned/image-only, needs OCR")
        return "image_pdf", page_count, total_chars, notes

    notes = f"avg {avg_chars:.0f} chars/page, {text_pages}/{page_count} text pages"
    return "text_pdf", page_count, total_chars, notes


def classify_file(path: Path) -> tuple[str, str, int, int, str]:
    """
    Returns (category, guessed_doc_type, page_count, extractable_text_chars, notes).
    """
    ext = path.suffix.lower()
    guessed = guess_doc_type(path.name)

    if ext in WORD_EXTS:
        return "word_doc", guessed, 0, 0, ""

    if ext in PDF_EXTS:
        category, page_count, chars, notes = classify_pdf(path)
        return category, guessed, page_count, chars, notes

    return "unknown", guessed, 0, 0, f"unhandled extension '{ext or '(none)'}'"


FIELDNAMES = [
    "file_path", "category", "file_ext", "guessed_doc_type", "guessed_subject",
    "page_count", "extractable_text_chars", "notes",
]


def triage(source: Path, subject: str, reports_root: Path, full: bool = False) -> Path:
    rows = []
    for root, _dirs, files in os.walk(source):
        for fname in files:
            fpath = Path(root) / fname
            category, guessed, page_count, chars, notes = classify_file(fpath)
            rows.append({
                "file_path": str(fpath),
                "category": category,
                "file_ext": file_ext_label(fpath),
                "guessed_doc_type": guessed,
                "guessed_subject": guess_subject(str(fpath)),
                "page_count": page_count,
                "extractable_text_chars": chars,
                "notes": notes,
            })

    reports_root.mkdir(parents=True, exist_ok=True)
    out_name = "GPT_Folder_CSEC_full_triage.csv" if full else f"{subject}_intake_triage.csv"
    out_path = reports_root / out_name
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    _print_summary(rows, out_path)
    return out_path


def _print_counter(title: str, counter: Counter, width: int = 16) -> None:
    print(title)
    if not counter:
        print("  (none)")
        return
    for key, n in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        print(f"  {str(key):<{width}} {n}")


def _print_summary(rows: list[dict], out_path: Path) -> None:
    by_category = Counter(r["category"] for r in rows)
    by_doc_type = Counter(r["guessed_doc_type"] for r in rows)
    by_subject = Counter(r["guessed_subject"] for r in rows)
    cross = Counter((r["category"], r["guessed_doc_type"]) for r in rows)
    unknown_exts = Counter(r["file_ext"] for r in rows if r["category"] == "unknown")

    print(f"\nScanned {len(rows)} file(s).")
    print(f"Report written to: {out_path}\n")

    _print_counter("By category:", by_category, width=12)
    print()
    _print_counter("Extensions inside 'unknown':", unknown_exts, width=16)
    print()
    _print_counter("By guessed doc type:", by_doc_type, width=16)
    print()
    _print_counter("By guessed subject:", by_subject, width=24)
    print()
    _print_counter("Category x doc type:",
                   Counter({f"{c:<12} {d}": n for (c, d), n in cross.items()}), width=30)

    # The number we actually care about right now: Integrated_Science only.
    isci = [r for r in rows if r["guessed_subject"] == "Integrated_Science"]
    print(f"\n--- Integrated_Science only ({len(isci)} file(s)) ---")
    _print_counter("  by category:", Counter(r["category"] for r in isci), width=12)
    print()
    _print_counter("  by guessed doc type:",
                   Counter(r["guessed_doc_type"] for r in isci), width=16)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only intake triage of a mixed subject-material folder.")
    parser.add_argument("--source", required=True,
                        help='Folder to walk, e.g. "D:\\GPT Folder CSEC"')
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Integrated_Science (names the CSV)")
    parser.add_argument("--reports-root", default=os.getenv("REPORTS_ROOT"),
                        help="Output folder (defaults to REPORTS_ROOT from .env)")
    parser.add_argument("--full", action="store_true",
                        help="Whole-dump run: write GPT_Folder_CSEC_full_triage.csv "
                             "instead of {Subject}_intake_triage.csv")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        sys.exit(f"ERROR: source folder not found: {source}")
    if not source.is_dir():
        sys.exit(f"ERROR: source is not a directory: {source}")

    if not args.reports_root:
        sys.exit("ERROR: no reports root. Set REPORTS_ROOT in .env or pass --reports-root.")

    triage(source, args.subject, Path(args.reports_root), full=args.full)


if __name__ == "__main__":
    main()
