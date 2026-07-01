"""
tools/ingest_econ_specimen_questions.py
==========================================
PHASE: build

Replaces the FABRICATED Economics specimen stems (see
tools/ingest_econ_specimen_stems.py -- its own docstring admits "the question
prompts themselves are not in the PDF... these stems are inferred from the
mark scheme answers") with the REAL question paper text.

The actual Economics Specimen 1 question paper (exam code
01216020/F/SPEC 2016) lives in the same syllabus PDF as the mark scheme, on
pages 73-84 -- directly preceding the mark scheme at page 90. Pages 88-89 are
blank/candidate-receipt boilerplate and are excluded.

This script extracts real, page-tagged question text with PyMuPDF for each
question_id already locked in mark_points (reusing
ingest_econ_specimen_stems.STEM_TEXTS -- currently 24 entries -- as the
canonical id list; that module is imported READ-ONLY here, never modified or
deleted) and writes a REVIEWABLE CSV. It writes NOTHING to the database --
no chunks, no documents. Locking real chunks from this CSV is a separate,
later step.

Usage:
    python tools/ingest_econ_specimen_questions.py
    python tools/ingest_econ_specimen_questions.py --dry-run
"""

import argparse
import csv
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_TOOLS_DIR))
load_dotenv(_REPO_ROOT / ".env")

try:
    import fitz  # PyMuPDF
    _FITZ_AVAILABLE = True
except ImportError:
    _FITZ_AVAILABLE = False

# Reuse the letter-part / optional-inline-roman pattern already proven in the
# mark-scheme extractor rather than re-implementing part-boundary logic. The
# real question paper puts roman sub-parts on their OWN line (unlike the mark
# scheme's inline "(a) (i)"), so _RE_PART's optional inline-roman group simply
# never fires here -- sub-parts are found separately by _RE_ROMAN_LINE below.
from extract_mark_scheme import _RE_PART

# tools/ingest_econ_specimen_stems.py is READ-ONLY imported for its canonical
# question_id -> question_num mapping. Never modified, never deleted.
from ingest_econ_specimen_stems import STEM_TEXTS

# ── Source document ──────────────────────────────────────────────────────────
PDF_PATH = (
    r"E:\CSEC_AI_STUDY_PARTNER\03_KNOWLEDGE_BASE\Economics\00_SYLLABUS"
    r"\csec-economics-syllabus-revised-2017.pdf"
)
START_PAGE = 73
END_PAGE   = 84  # inclusive. 85-87 = extra-space pages, 88-89 = blank/receipt.

CSV_COLUMNS = ["question_id", "question_num", "question_part", "page", "stem_text", "verified"]

_TOP_LEVEL_LETTERS = {"a", "b", "c", "d"}

# ── Boilerplate stripping (requirement: repeated per-page exam-booklet header) ─
_BOILERPLATE_LINE_PATTERNS = [
    re.compile(r"^-\s*\d+\s*-$"),                     # page number, e.g. "- 4 -"
    re.compile(r"^GO ON TO THE NEXT PAGE$", re.IGNORECASE),
    re.compile(r"^01216020/F/SPEC 2016$"),
    re.compile(r"Barcode Area", re.IGNORECASE),
    re.compile(r"^Sequential Bar Code$", re.IGNORECASE),
    re.compile(r"DO NOT WRITE IN THIS AREA", re.IGNORECASE),
]
# Dotted answer-ruling lines are exam-booklet writing space, not question
# content -- not one of the five named boilerplate categories, but the same
# kind of per-page noise; stripped for stem_text quality.
_RE_DOT_LEADER = re.compile(r"^\.{5,}$")


def strip_boilerplate(page_text: str) -> str:
    """Remove the repeated exam-booklet header from one page's raw PyMuPDF
    text: the page-number line, "GO ON TO THE NEXT PAGE", the exam code
    "01216020/F/SPEC 2016", the barcode-area lines, "Sequential Bar Code",
    and the triple "DO NOT WRITE IN THIS AREA" line. Blank lines and dotted
    answer-ruling lines are dropped too.
    """
    kept = []
    for line in page_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _RE_DOT_LEADER.match(stripped):
            continue
        if any(p.search(stripped) for p in _BOILERPLATE_LINE_PATTERNS):
            continue
        kept.append(stripped)
    return "\n".join(kept)


# ── Section-header stripping (requirement: SECTION I / SECTION II) ───────────
_RE_SECTION_LINE = re.compile(r"^SECTION\s+[IVX]+$", re.IGNORECASE)
_RE_SECTION_INSTRUCTION = re.compile(
    r"^Answer (ALL|ANY)\b.*question.*in this section\.?$", re.IGNORECASE
)
_RE_EACH_QUESTION_WORTH = re.compile(r"^EACH question is worth \d+ marks\.?$", re.IGNORECASE)


def strip_section_headers(text: str) -> str:
    """Remove "SECTION I" / "SECTION II" headers and their instructional
    lines. These are section-level exam formatting, not question content --
    Q1 (preceded by SECTION I) and Q5 (preceded by SECTION II) must not
    inherit them as part of their stem text.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (_RE_SECTION_LINE.match(stripped)
                or _RE_SECTION_INSTRUCTION.match(stripped)
                or _RE_EACH_QUESTION_WORTH.match(stripped)):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _clean_page_text(raw: str) -> str:
    return strip_section_headers(strip_boilerplate(raw))


# ── Question / part boundary patterns ────────────────────────────────────────
# "N." alone on its own line starts a new question (1-6) -- distinct from the
# mark scheme's "Question N" convention, hence a new pattern (no equivalent
# exists in extract_mark_scheme.py for this document's numbering style).
_RE_QUESTION_NUM_LINE = re.compile(r"^\s*([1-6])\.\s*$", re.MULTILINE)

# A roman numeral alone on its own line marks a sub-part. Also new: the mark
# scheme's _RE_PART expects the roman group inline immediately after the
# letter ("(a) (i)"); this document puts sub-parts on a separate line.
_RE_ROMAN_LINE = re.compile(r"^\s*\(([ivxlIVXL]+)\)\s*$", re.MULTILINE)

_RE_TOTAL_MARKS = re.compile(r"Total\s+\d+\s+marks", re.IGNORECASE)


# ── PDF extraction ────────────────────────────────────────────────────────────
def extract_raw_pages(pdf_path: str, start_page: int, end_page: int) -> list[tuple[int, str]]:
    """Return [(page_num, cleaned_text), ...] for start_page..end_page inclusive.

    page_num is always the real, 1-based PDF page number -- never None, never
    a placeholder.
    """
    if not _FITZ_AVAILABLE:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")
    doc = fitz.open(pdf_path)
    pages = []
    for i in range(start_page - 1, min(end_page, len(doc))):
        raw = doc[i].get_text()
        pages.append((i + 1, _clean_page_text(raw)))
    return pages


def _build_char_to_page(pages: list[tuple[int, str]]):
    """Mirror extract_mark_scheme.py's page-lookup pattern: build the
    concatenated text plus a function mapping an absolute character offset
    back to the real page number it came from."""
    full_text = "\n".join(text for _, text in pages)

    page_starts: list[tuple[int, int]] = []
    offset = 0
    for page_num, text in pages:
        page_starts.append((offset, page_num))
        offset += len(text) + 1

    def _char_to_page(char_pos: int) -> int:
        page_num = page_starts[0][1]
        for off, pn in page_starts:
            if off > char_pos:
                break
            page_num = pn
        return page_num

    return full_text, _char_to_page


def _join_nonempty(parts: list[str]) -> str:
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


# ── Structural parser ─────────────────────────────────────────────────────────
def _parse_question_block(q_num: str, block_text: str, block_abs_start: int,
                          char_to_page, units: dict) -> None:
    """Parse one question's (already Total-marks-truncated) block text into
    per-part / per-sub-part units, keyed to match STEM_TEXTS's question_num
    convention ("N(letter)" or "N(letter)(roman)")."""
    # Only accept a/b/c/d as top-level parts -- _RE_PART's [a-z] group would
    # otherwise also match a standalone "(i)" sub-part line as if it were a
    # new top-level part "i", which it is not in this document's convention.
    letter_matches = [
        m for m in _RE_PART.finditer(block_text) if m.group(1) in _TOP_LEVEL_LETTERS
    ]
    if not letter_matches:
        return

    question_intro = block_text[: letter_matches[0].start()].strip()

    for i, lm in enumerate(letter_matches):
        letter = lm.group(1)
        seg_start = lm.start()
        seg_end = letter_matches[i + 1].start() if i + 1 < len(letter_matches) else len(block_text)
        seg_text = block_text[seg_start:seg_end]
        seg_abs_start = block_abs_start + seg_start
        label_body_start = lm.end() - seg_start  # offset within seg_text, past the "(letter)" label

        roman_matches = list(_RE_ROMAN_LINE.finditer(seg_text))

        if not roman_matches:
            body = seg_text[label_body_start:].strip()
            key = f"{q_num}({letter})"
            stem_text = _join_nonempty([
                f"Question {q_num}.", question_intro, f"({letter}) {body}",
            ])
            units[key] = {"page": char_to_page(seg_abs_start), "text": stem_text}
            continue

        part_intro = seg_text[label_body_start: roman_matches[0].start()].strip()

        for j, rm in enumerate(roman_matches):
            roman = rm.group(1)
            sub_start = rm.start()
            sub_end = roman_matches[j + 1].start() if j + 1 < len(roman_matches) else len(seg_text)
            sub_body = seg_text[rm.end():sub_end].strip()
            sub_abs_start = seg_abs_start + sub_start

            key = f"{q_num}({letter})({roman})"
            stem_text = _join_nonempty([
                f"Question {q_num}.", question_intro, part_intro, f"({roman}) {sub_body}",
            ])
            units[key] = {"page": char_to_page(sub_abs_start), "text": stem_text}


def parse_specimen_questions(pdf_path: str, start_page: int, end_page: int) -> dict:
    """Parse pages start_page..end_page into {question_num_key: {"page", "text"}}.

    question_num_key format matches STEM_TEXTS's "question_num" convention:
    "N(letter)" for a part with no sub-parts, "N(letter)(roman)" for a
    sub-part. Every unit's "page" is a real integer -- never None.
    """
    pages = extract_raw_pages(pdf_path, start_page, end_page)
    full_text, char_to_page = _build_char_to_page(pages)

    q_matches = list(_RE_QUESTION_NUM_LINE.finditer(full_text))
    units: dict = {}

    for idx, m in enumerate(q_matches):
        q_num = m.group(1)
        block_start = m.end()
        block_end = q_matches[idx + 1].start() if idx + 1 < len(q_matches) else len(full_text)
        block_text = full_text[block_start:block_end]

        # Truncate at "Total N marks" -- excludes trailing SECTION headers
        # (e.g. "SECTION II" bleeding into Q4) and end-of-test boilerplate
        # (e.g. "END OF TEST" bleeding into Q6) that would otherwise fall
        # inside the naive [this question -> next question) slice.
        total_m = _RE_TOTAL_MARKS.search(block_text)
        if total_m:
            block_text = block_text[: total_m.start()]

        _parse_question_block(q_num, block_text, block_start, char_to_page, units)

    return units


# ── Map real units onto the existing (never-invented) question_ids ──────────
_RE_SPLIT_QNUM_KEY = re.compile(r"^(\d+)(\(.+\))$")


def _split_question_num_key(key: str) -> tuple[str, str]:
    """'1(b)(i)' -> ('1', '(b)(i)'). Mirrors extract_mark_scheme.py's CSV
    convention of separate question_num / question_part columns."""
    m = _RE_SPLIT_QNUM_KEY.match(key)
    if not m:
        raise ValueError(f"Cannot split question_num key: {key!r}")
    return m.group(1), m.group(2)


def lookup_real_unit(question_num_key: str, units: dict) -> tuple[dict | None, bool]:
    """Resolve a STEM_TEXTS question_num value to a real parsed unit.

    Returns (unit_or_None, used_fallback). Handles two known mismatches
    between the fabricated STEM_TEXTS convention and the real paper:

    - "5(c)-2" is qb6(c)'s fabricated label for what the mark-scheme PDF
      treated as a second, separate mark point under "Question 5 cont'd".
      The real question paper has ONE combined part "(c)" (8 marks, asking
      for both a positive AND a negative contribution together) -- there is
      no separate second part to extract. Both qb5(c) and qb6(c) resolve to
      the SAME real "5(c)" unit.
    - A question_num like "1(a)(i)" assumes a roman sub-part that the real
      paper's corresponding letter part doesn't actually have (e.g. the real
      Q1(a) is a single, unsplit part). Falls back to the parent "N(letter)"
      unit rather than inventing sub-part text that isn't in the PDF.
    """
    key = question_num_key[:-2] if question_num_key.endswith("-2") else question_num_key

    if key in units:
        return units[key], False

    m = re.match(r"^(\d+\([a-d]\))\([ivxlIVXL]+\)$", key)
    if m and m.group(1) in units:
        return units[m.group(1)], True

    return None, False


def build_review_rows(units: dict) -> list[dict]:
    """Build one CSV row per existing STEM_TEXTS question_id -- never more,
    never fewer, and never a new question_id."""
    rows = []
    for question_id, meta in sorted(STEM_TEXTS.items()):
        q_num_key = meta["question_num"]
        unit, used_fallback = lookup_real_unit(q_num_key, units)

        if unit is None:
            print(f"  WARNING: {question_id!r} ({q_num_key!r}) has no matching "
                  f"real unit -- left out of the review CSV.")
            continue

        if used_fallback:
            print(f"  NOTE: {question_id!r} ({q_num_key!r}) has no exact real "
                  f"sub-part in the PDF; used the parent part's real text instead.")

        base_key = q_num_key[:-2] if q_num_key.endswith("-2") else q_num_key
        num, part = _split_question_num_key(base_key)
        rows.append({
            "question_id":   question_id,
            "question_num":  num,
            "question_part": part,
            "page":          unit["page"],
            "stem_text":     unit["text"],
            "verified":      0,
        })
    return rows


# ── CSV writer ───────────────────────────────────────────────────────────────
def write_review_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


# ── CLI ────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract the REAL Economics specimen question paper "
                     "(pages 73-84) into a reviewable stem CSV. Writes nothing "
                     "to the database."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and print a summary but do not write the CSV")
    ap.add_argument("--pdf-path", default=PDF_PATH, help="Override the syllabus PDF path")
    args = ap.parse_args()

    if not Path(args.pdf_path).exists():
        sys.exit(f"ERROR: PDF not found: {args.pdf_path!r}")

    print(f"Parsing: {args.pdf_path}")
    print(f"Pages  : {START_PAGE}-{END_PAGE}")

    units = parse_specimen_questions(args.pdf_path, START_PAGE, END_PAGE)
    print(f"\nParsed {len(units)} real question units.")

    rows = build_review_rows(units)
    print(f"\nBuilt {len(rows)} of {len(STEM_TEXTS)} review rows "
          f"(one per existing question_id).")

    if args.dry_run:
        print("\n[dry-run] CSV not written.")
        return

    reports_root = os.getenv("REPORTS_ROOT")
    if not reports_root:
        sys.exit("ERROR: REPORTS_ROOT not set in .env")

    output_path = Path(reports_root) / "Economics_specimen_stems_review.csv"
    write_review_csv(rows, output_path)
    print(f"\nCSV written: {output_path}")


if __name__ == "__main__":
    main()
