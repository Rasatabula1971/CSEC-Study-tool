"""
backend/db/extract_syllabus.py
==============================
Best-effort *draft* extractor: reads a CXC syllabus PDF and produces a
`{prefix}_syllabus_raw.csv` in the subject's 00_SYLLABUS folder.

IMPORTANT: the output is a DRAFT, not the source of truth. CXC syllabus PDFs use
two-column layouts that text-extraction interleaves unpredictably. You MUST open
the CSV and verify every row against the PDF before running syllabus_parser.py
and lock_subject.py. This script only saves you the bulk typing.

Usage:
    python backend/db/extract_syllabus.py --subject Principles_of_Business \
        --pdf-file "D:\\...\\POB_Syllabus_CXC.pdf"

Columns produced (matches syllabus_parser.py / README_syllabus_csv.md):
    section_id, section_num, section_title, objective_id, objective_num,
    content_stmt, skill_type, command_words, exam_weight
"""

import argparse
import csv
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

# Short filename prefix + objective_id prefix per subject.
SUBJECT_PREFIX = {
    "Principles_of_Business": "pob",
    "Economics": "econ",
    "Mathematics": "math",
    "English": "english",
    "Principles_of_Accounts": "poa",
    "Integrated_Science": "int_sci",
    "Information_Technology": "it",
}

# Command verb -> skill_type (CXC cognitive levels).
SKILL_BY_VERB = {
    # Knowledge
    "identify": "Knowledge", "list": "Knowledge", "state": "Knowledge",
    "define": "Knowledge", "name": "Knowledge", "classify": "Knowledge",
    # Understanding
    "describe": "Understanding", "explain": "Understanding", "outline": "Understanding",
    "distinguish": "Understanding", "differentiate": "Understanding",
    "discuss": "Understanding", "interpret": "Understanding",
    # Application
    "apply": "Application", "construct": "Application", "prepare": "Application",
    "evaluate": "Application", "assess": "Application",
    "analyse": "Application", "analyze": "Application", "compare": "Application",
}

# exam_weight rules driven by the command verb:
#   P2  -> extended-response verbs that demand worked/constructed answers
#   P1  -> pure-recall verbs (the Knowledge set) suited to multiple choice
#   Both -> everything else
P2_VERBS = {"construct", "prepare", "apply"}
P1_VERBS = {"identify", "list", "state", "define", "name", "classify"}

# Section header, e.g. "SECTION 1: THE NATURE OF BUSINESS" / "SECTION 2 - ..."
SECTION_RE = re.compile(r"^\s*SECTION\s+(\d+)\s*[:.\-–—]?\s*(.*)$", re.IGNORECASE)
# Numbered objective, e.g. "1. explain the concept of a business" / "2) outline ..."
OBJECTIVE_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+([A-Za-z].*)$")
# Marks the start of an objectives block.
TRIGGER_RE = re.compile(r"(students should be able to|specific objective)", re.IGNORECASE)
# Headings that end an objectives block (the right-hand 'CONTENT' column etc.).
STOP_HEADINGS = (
    "CONTENT", "RESOURCE", "SUGGESTED", "GUIDELINES", "GENERAL OBJECTIVE",
    "FORMAT", "SKILLS", "ASSESSMENT", "REGULATIONS", "RECOMMENDED",
)


def infer_skill_type(content: str) -> tuple[str, str]:
    """Return (skill_type, command_word) inferred from the leading verb."""
    m = re.search(r"[A-Za-z]+", content)
    if not m:
        return "", ""
    verb = m.group(0).lower()
    return SKILL_BY_VERB.get(verb, ""), verb.capitalize()


def infer_exam_weight(command_word: str) -> str:
    """P2 for construct/prepare/apply, P1 for pure-recall verbs, else Both."""
    verb = command_word.lower()
    if verb in P2_VERBS:
        return "P2"
    if verb in P1_VERBS:
        return "P1"
    return "Both"


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(";").strip()
    if text:
        text = text[0].upper() + text[1:]
    return text


def is_stop_heading(line: str) -> bool:
    up = line.upper().lstrip("0123456789.)( ").strip()
    return any(up.startswith(h) for h in STOP_HEADINGS)


def extract_text(pdf_path: Path) -> str:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")
    doc = fitz.open(pdf_path)
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(pages)


def parse(text: str, prefix_up: str) -> list[dict]:
    """State-machine parse of section headers + numbered objectives."""
    rows: list[dict] = []
    sec_num = None
    sec_title = ""
    awaiting_title = False
    in_objectives = False
    cur = None  # current objective being accumulated

    def commit():
        nonlocal cur
        if cur:
            cur["content_stmt"] = clean(cur["content_stmt"])
            if cur["content_stmt"]:
                skill, cmd = infer_skill_type(cur["content_stmt"])
                cur["skill_type"] = skill
                cur["command_words"] = cmd
                cur["exam_weight"] = infer_exam_weight(cmd)
                rows.append(cur)
        cur = None

    for raw in text.splitlines():
        line = raw.strip()

        if not line:
            commit()
            continue

        m = SECTION_RE.match(line)
        if m:
            commit()
            sec_num = m.group(1)
            sec_title = clean(m.group(2))
            awaiting_title = not sec_title
            in_objectives = False
            continue

        # Section header had no inline title -> next non-empty line is the title.
        if awaiting_title:
            sec_title = clean(line)
            awaiting_title = False
            continue

        if TRIGGER_RE.search(line):
            commit()
            in_objectives = True
            continue

        if in_objectives and is_stop_heading(line):
            commit()
            in_objectives = False
            continue

        om = OBJECTIVE_RE.match(line)
        if om and in_objectives and sec_num is not None:
            commit()
            obj_num = om.group(1)
            cur = {
                "section_id": f"{prefix_up}-SEC-{sec_num}",
                "section_num": sec_num,
                "section_title": sec_title,
                "objective_id": f"{prefix_up}-{sec_num}.{obj_num}",
                "objective_num": f"{sec_num}.{obj_num}",
                "content_stmt": om.group(2),
                "skill_type": "",
                "command_words": "",
                "exam_weight": "Both",
            }
            continue

        # Continuation of the current objective's text.
        if cur is not None and in_objectives:
            cur["content_stmt"] += " " + line

    commit()
    return rows


COLUMNS = [
    "section_id", "section_num", "section_title", "objective_id",
    "objective_num", "content_stmt", "skill_type", "command_words", "exam_weight",
]


def main() -> None:
    ap = argparse.ArgumentParser(description="Draft-extract a CXC syllabus PDF to CSV.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--pdf-file", required=True, help="Path to the syllabus PDF")
    args = ap.parse_args()

    kb_root = os.getenv("KB_ROOT")
    if not kb_root:
        sys.exit("ERROR: KB_ROOT not set in .env")

    subject_dir = Path(kb_root) / args.subject / "00_SYLLABUS"
    if not subject_dir.exists():
        sys.exit(
            f"ERROR: {subject_dir} does not exist.\n"
            "Run python backend/db/init_db.py first (and check the --subject name)."
        )

    pdf_path = Path(args.pdf_file)
    if not pdf_path.exists():
        sys.exit(f"ERROR: PDF not found: {pdf_path}")

    prefix = SUBJECT_PREFIX.get(args.subject, args.subject.lower())
    prefix_up = prefix.upper()

    print(f"Reading {pdf_path.name} ...")
    text = extract_text(pdf_path)
    rows = parse(text, prefix_up)

    sections = sorted({r["section_num"] for r in rows}, key=lambda n: int(n))
    print(f"Sections found    : {len(sections)}  ({', '.join(sections) or '—'})")
    print(f"Objectives found  : {len(rows)}")

    if not rows:
        sys.exit(
            "\nERROR: no objectives parsed — the PDF layout did not match the expected\n"
            "'SECTION N' + 'Students should be able to:' + numbered-list pattern.\n"
            "Nothing was written (any existing CSV is preserved). Fill the CSV by hand,\n"
            "or send me a sample of the PDF text and I'll adjust the parser."
        )

    out_path = subject_dir / f"{prefix}_syllabus_raw.csv"
    if out_path.exists():
        bak = out_path.with_suffix(".csv.bak")
        out_path.replace(bak)
        print(f"NOTE: existing {out_path.name} backed up to {bak.name}")

    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote DRAFT CSV: {out_path}")
    print("Preview (first 5):")
    for r in rows[:5]:
        st = r["skill_type"] or "?"
        print(f"  {r['objective_id']:<12} [{st:<13}] {r['exam_weight']:<4} {r['content_stmt'][:60]}")
    print(
        "\n*** DRAFT — verify every row against the PDF before running "
        "syllabus_parser.py. ***"
    )


if __name__ == "__main__":
    main()
