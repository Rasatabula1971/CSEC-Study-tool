"""
tools/locate_mark_scheme_pages.py
==================================
PHASE: build

Scans a syllabus PDF for the embedded mark-scheme page range.

Usage:
    python tools/locate_mark_scheme_pages.py --pdf "D:/path/to/syllabus.pdf"
"""

import argparse
import re
import sys
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")


# Patterns that mark the START of the embedded mark scheme
_START_PATTERNS = [
    re.compile(r"keys\s+and\s+mark\s+scheme", re.IGNORECASE),
    re.compile(r"keys\s*/\s*mark\s+scheme",   re.IGNORECASE),
    re.compile(r"mark\s+scheme",               re.IGNORECASE),
]

# Patterns that mark the END of the mark-scheme section
_END_PATTERNS = [
    re.compile(r"recommended\s+readings?",     re.IGNORECASE),
    re.compile(r"end\s+of\s+mark\s+scheme",    re.IGNORECASE),
    re.compile(r"bibliography",                re.IGNORECASE),
    re.compile(r"appendix",                    re.IGNORECASE),
]


def _page_matches(patterns: list, text: str) -> bool:
    return any(p.search(text) for p in patterns)


def locate(pdf_path: str) -> tuple[int, int] | tuple[None, None]:
    """Return (start_page, end_page) as 1-based page numbers, or (None, None)."""
    doc = fitz.open(pdf_path)
    total = len(doc)

    start_page = None
    for i in range(total):
        text = doc[i].get_text()
        if _page_matches(_START_PATTERNS, text):
            start_page = i + 1  # 1-based
            break

    if start_page is None:
        return None, None

    end_page = total  # default: runs to end of PDF
    for i in range(start_page, total):  # start_page is 1-based → index = start_page
        text = doc[i].get_text()
        if _page_matches(_END_PATTERNS, text):
            end_page = i  # page before the end marker (1-based: i = the 0-based index of the end page)
            break

    return start_page, end_page


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Locate the mark-scheme page range inside a CXC syllabus PDF."
    )
    ap.add_argument("--pdf", required=True, help="Path to the syllabus PDF.")
    args = ap.parse_args()

    pdf_path = args.pdf
    if not Path(pdf_path).exists():
        sys.exit(f"ERROR: PDF not found: {pdf_path}")

    doc = fitz.open(pdf_path)
    total = len(doc)
    print(f"PDF: {pdf_path}")
    print(f"Total pages: {total}")

    start, end = locate(pdf_path)
    if start is None:
        print("\nNo mark-scheme section detected.")
        print("Searched for: 'Keys and Mark Scheme', 'Keys / Mark Scheme', 'Mark Scheme'")
        return

    print(f"\nDetected mark-scheme range: pages {start}–{end} (1-based)\n")
    print("First 200 chars of each page in range:")
    print("=" * 70)
    for i in range(start - 1, end):  # convert back to 0-based
        text = doc[i].get_text()
        preview = text[:200].replace("\n", " ").strip()
        print(f"  Page {i + 1}: {preview!r}")
    print("=" * 70)
    print(f"\nTo record this range: add to tools/mark_scheme_page_ranges.json:")
    print(f'  "pages": [{start}, {end}]')


if __name__ == "__main__":
    main()
