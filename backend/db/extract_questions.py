# PHASE: build
"""
backend/db/extract_questions.py
===============================
Best-effort *draft* extractor for CSEC Paper 01 (multiple-choice) past papers.
Parses each PDF into structured items — question number, stem, and options
(A)-(D) — for a practice/quiz bank. Mirrors extract_syllabus.py: the output is a
DRAFT for human review, NOT a source of truth.

IMPORTANT: P1 past papers are scanned PDFs with an OCR text layer of varying
quality. Some extract cleanly (e.g. 2012); others are garbled (e.g. 2006) and
should be skipped or re-OCR'd. Use --triage first to see which papers are usable.

This tool deliberately does NOT assign objective_id or write to the database.
Mapping each item to one of the syllabus objectives (Rule 1) is a separate,
verified pass done once Ollama is available (semantic match) or by hand.

Usage:
    # Quality survey of every PDF in the subject's 02_PAST_PAPERS folder:
    python backend/db/extract_questions.py --subject Principles_of_Business --triage

    # Extract one paper to a draft CSV in REPORTS_ROOT:
    python backend/db/extract_questions.py --subject Principles_of_Business \
        --pdf-file "E:\\...\\CSEC POB MayJune P1 2012.pdf"

    # Extract every paper that passes the CLEAN threshold into one CSV:
    python backend/db/extract_questions.py --subject Principles_of_Business --extract-clean
"""

import argparse
import csv
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

SUBJECT_PREFIX = {
    "Principles_of_Business": "pob",
    "Economics": "econ",
    "Mathematics": "math",
    "English": "english",
    "Principles_of_Accounts": "poa",
    "Integrated_Science": "int_sci",
    "Information_Technology": "it",
}

# An item: a number with a dot/paren, optionally followed by the start of its
# stem on the same print line (the number sits left of the stem, same y-row).
ITEM_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*(.*)$")
# An option marker, always parenthesised here: "(A)" / "( B)" / "(0)".
# OCR turns the 4th option (D) into "(0)" or "(O)"; accept and normalise below.
OPTION_RE = re.compile(r"^\s*[(\[]\s*([A-D0O])\s*[)\]]\s*(.*)$", re.IGNORECASE)
OPTION_FIX = {"0": "D", "O": "D"}  # common OCR confusions for (D)
# Boilerplate that bleeds into a stem/option from page edges; cut the field here.
TRAILER_RE = re.compile(
    r"\s*(CHECK YOUR WORK|GO ON TO|END OF TEST|TURN OVER"
    r"|The best answer|Items?\s+\d+\s*[-–]?\s*\d*\s+refers?).*$",
    re.IGNORECASE,
)


def trim(text: str) -> str:
    """Drop page boilerplate that the column merge appended to a field."""
    return TRAILER_RE.sub("", text).strip()
# Page furniture to drop.
NOISE_RE = re.compile(
    r"^\s*("
    r"-?\s*\d+\s*-?"                       # bare/decorated page numbers  "- 5 -"
    r"|GO ON TO THE NEXT PAGE"
    r"|.*END OF TEST.*"
    r"|TURN OVER"
    r"|\d{8,}"                             # long test-code barcodes
    r"|0?1240\d+"                          # POB P1 test codes
    r")\s*$",
    re.IGNORECASE,
)
# Characters that betray bad OCR (mojibake / replacement char / odd symbols).
GARBLE_CHARS = set("~`^{}|<>\\ �‚„‰")


def _column_lines(words: list, x_lo: float, x_hi: float) -> list[str]:
    """Cluster the words whose left edge is in [x_lo, x_hi) into print lines.

    Words on the same y-row (±3pt) join one line, sorted left-to-right — so an
    option letter "(A)" and its text "Planning" (same row) merge correctly.
    """
    col = [w for w in words if x_lo <= w[0] < x_hi]
    col.sort(key=lambda w: (w[1], w[0]))
    clusters: list[dict] = []
    for w in col:
        for c in clusters:
            if abs(c["y"] - w[1]) <= 3:
                c["ws"].append(w)
                break
        else:
            clusters.append({"y": w[1], "ws": [w]})
    clusters.sort(key=lambda c: c["y"])
    lines = []
    for c in clusters:
        line = " ".join(w[4] for w in sorted(c["ws"], key=lambda w: w[0])).strip()
        if line and not NOISE_RE.match(line):
            lines.append(line)
    return lines


def extract_lines(pdf_path: Path) -> list[tuple[int, str]]:
    """Return [(page_index, line_text)] in reading order for a 2-column P1 paper.

    CSEC P1 pages are two columns of items. Plain get_text() emits option letters
    and option texts as separate blocks, scrambling the order, so we split words at
    the mid-page gutter, rebuild print lines per column by y-row, and read the left
    column fully before the right — the order the candidate actually reads.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")

    out: list[tuple[int, str]] = []
    doc = fitz.open(pdf_path)
    for i, page in enumerate(doc):
        words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,wordno)
        gutter = page.rect.width * 0.5
        for line in _column_lines(words, 0, gutter):       # left column first
            out.append((i, line))
        for line in _column_lines(words, gutter, page.rect.width):  # then right
            out.append((i, line))
    doc.close()
    return out


def parse_items(lines: list[tuple[int, str]]) -> list[dict]:
    """State-machine parse of numbered MCQ items with (A)-(D) options.

    An item is only emitted once it has a non-empty stem and all four options.
    Item numbers are accepted when roughly sequential (next == last+1, or a small
    forward jump) so stray numbers inside option text don't start phantom items.
    """
    items: list[dict] = []
    cur: dict | None = None
    cur_opt: str | None = None
    last_num = 0

    def commit():
        nonlocal cur, cur_opt
        if cur:
            opts = cur["options"]
            stem = trim(re.sub(r"\s+", " ", cur["stem"]))
            if stem and all(opts.get(k) for k in "ABCD"):
                items.append({
                    "page": cur["page"] + 1,
                    "question_num": cur["num"],
                    "stem": stem,
                    "option_a": trim(opts["A"]),
                    "option_b": trim(opts["B"]),
                    "option_c": trim(opts["C"]),
                    "option_d": trim(opts["D"]),
                })
        cur = None
        cur_opt = None

    for page, line in lines:
        m_item = ITEM_RE.match(line)
        if m_item:
            n = int(m_item.group(1))
            # Accept as a new item if it moves the sequence forward (allow small
            # gaps for items we failed to parse) within the legal 1..60 P1 range.
            if 1 <= n <= 60 and last_num < n <= last_num + 6:
                commit()
                cur = {"num": n, "page": page, "stem": m_item.group(2).strip(),
                       "options": {}}
                cur_opt = None
                last_num = n
                continue

        if cur is None:
            continue

        m_opt = OPTION_RE.match(line)
        # Only treat as an option marker once we've seen a stem (avoids matching a
        # stem line that happens to start with a capital A-D word).
        if m_opt and (cur["stem"] or cur["options"]):
            letter = OPTION_FIX.get(m_opt.group(1).upper(), m_opt.group(1).upper())
            cur_opt = letter
            cur["options"][letter] = m_opt.group(2).strip()
            continue

        if cur_opt:
            cur["options"][cur_opt] += " " + line
        else:
            cur["stem"] += " " + line

    commit()
    return items


def quality(items: list[dict], lines: list[tuple[int, str]]) -> dict:
    """Compute triage signals: items parsed and an OCR-garble ratio over stems."""
    text = " ".join(s for _, s in lines)
    garble = sum(1 for ch in text if ch in GARBLE_CHARS)
    ratio = garble / max(len(text), 1)
    n = len(items)
    if n >= 45 and ratio < 0.004:
        verdict = "CLEAN"
    elif n >= 25:
        verdict = "PARTIAL"
    else:
        verdict = "POOR"
    return {"items": n, "garble_ratio": ratio, "verdict": verdict}


COLUMNS = [
    "source_file", "page", "question_num",
    "stem", "option_a", "option_b", "option_c", "option_d",
]
CLEAN_MIN_ITEMS = 45
CLEAN_MAX_GARBLE = 0.004


def papers_dir(subject: str) -> Path:
    kb_root = os.getenv("KB_ROOT")
    if not kb_root:
        sys.exit("ERROR: KB_ROOT not set in .env")
    d = Path(kb_root) / subject / "02_PAST_PAPERS"
    if not d.exists():
        sys.exit(f"ERROR: {d} does not exist. Run init_db.py and stage the papers first.")
    return d


def is_p1(name: str) -> bool:
    """Heuristic: a Paper 01 file (multiple choice), not P2 / answer keys."""
    low = name.lower()
    return low.endswith(".pdf") and "p2" not in low and "paper 02" not in low


def write_csv(rows: list[dict], out_path: Path) -> None:
    if out_path.exists():
        out_path.replace(out_path.with_suffix(".csv.bak"))
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft-extract P1 MCQ past papers to CSV.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--pdf-file", help="Extract a single paper to a draft CSV")
    ap.add_argument("--triage", action="store_true", help="Quality survey of all P1 papers")
    ap.add_argument("--extract-clean", action="store_true",
                    help="Extract every CLEAN paper into one combined CSV")
    args = ap.parse_args()

    reports_root = os.getenv("REPORTS_ROOT")
    if not reports_root and (args.pdf_file or args.extract_clean):
        sys.exit("ERROR: REPORTS_ROOT not set in .env")
    prefix = SUBJECT_PREFIX.get(args.subject, args.subject.lower())

    # --- single paper -----------------------------------------------------
    if args.pdf_file:
        pdf = Path(args.pdf_file)
        if not pdf.exists():
            sys.exit(f"ERROR: PDF not found: {pdf}")
        lines = extract_lines(pdf)
        items = parse_items(lines)
        for it in items:
            it["source_file"] = pdf.name
        q = quality(items, lines)
        out = Path(reports_root) / f"{prefix}_p1_questions_{pdf.stem}.csv"
        Path(reports_root).mkdir(parents=True, exist_ok=True)
        write_csv(items, out)
        print(f"{pdf.name}: items={q['items']} garble={q['garble_ratio']:.4f} "
              f"verdict={q['verdict']}")
        print(f"Wrote DRAFT CSV: {out}")
        print("*** DRAFT — verify items against the PDF; objective_id NOT yet assigned. ***")
        return

    # --- triage / extract-clean over the folder ---------------------------
    pdir = papers_dir(args.subject)
    papers = sorted(p for p in pdir.iterdir() if is_p1(p.name))
    if not papers:
        sys.exit(f"No P1 PDFs found in {pdir}")

    print(f"{'paper':<40} {'items':>5} {'garble':>7}  verdict")
    print("-" * 70)
    clean_rows: list[dict] = []
    clean_papers: list[str] = []
    for pdf in papers:
        lines = extract_lines(pdf)
        items = parse_items(lines)
        q = quality(items, lines)
        print(f"{pdf.name[:40]:<40} {q['items']:>5} {q['garble_ratio']:>7.4f}  {q['verdict']}")
        if q["verdict"] == "CLEAN":
            for it in items:
                it["source_file"] = pdf.name
            clean_rows.extend(items)
            clean_papers.append(pdf.name)

    if args.extract_clean:
        out = Path(reports_root) / f"{prefix}_p1_questions_clean.csv"
        Path(reports_root).mkdir(parents=True, exist_ok=True)
        write_csv(clean_rows, out)
        print(f"\nCLEAN papers ({len(clean_papers)}): {', '.join(clean_papers)}")
        print(f"Total items extracted: {len(clean_rows)}")
        print(f"Wrote DRAFT CSV: {out}")
        print("*** DRAFT — verify items; objective_id assigned later (Stage 3). ***")
    else:
        print("\n(triage only — re-run with --extract-clean to write the CLEAN papers' CSV)")


if __name__ == "__main__":
    main()
