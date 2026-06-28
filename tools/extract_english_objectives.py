# PHASE: build — called only during ingestion prep (syllabus extraction)
"""
Extract CSEC English A syllabus objectives from the amended-2026 PDF.

Structure confirmed against the PDF (CXC 01/G/SYLL 25):
  * 3 modules: Informative Discourse, Literary Discourse, Persuasive Discourse
  * Each module has 3 skill sections with headers in format:
      "Understanding (Module N) – {Discourse Name}"
      "Analysing (Module N) – {Discourse Name}"
      "Evaluating and Creating (Module N) – {Discourse Name}"
  * Within each section, specific objectives are simple numbered items:
      "1. explain meaning conveyed through word choice..."
      "2. identify effective use of parts of speech..."

Section header pages (0-indexed, confirmed from PDF):
  Understanding (Module 1): page 24   Analysing (Module 1): page 29
  Evaluating and Creating (Module 1): page 31
  Understanding (Module 2): page 38   Analysing (Module 2): page 43
  Evaluating and Creating (Module 2): page 49
  Understanding (Module 3): page 56   Analysing (Module 3): page 60
  Evaluating and Creating (Module 3): page 63

Module page ranges (0-indexed):
  Module 1: 24–34   Module 2: 38–55   Module 3: 56–67

Objective ID scheme:
  section_num  = "{module}.{section}"       e.g. "1.2"
  objective_id = "ENG-{module}.{section}.{obj_num}"  e.g. "ENG-1.2.1"
  skill section: 1=Understanding  2=Analysing  3=Evaluating and Creating

exam_weight=Both verified: Assessment Grid II (PDF p14) shows Paper 01 + Paper 02
both test Understanding (UD), Analysis (AN), and Evaluating & Creating (E&C).

Writes master-map CSV consumed by build_syllabus_csv.py.

  python tools/extract_english_objectives.py            # writes CSV + summary
  python tools/extract_english_objectives.py --dry-run  # summary only, no write
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("ERROR: PyMuPDF (fitz) not installed. Run: pip install pymupdf")

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from backend.ingest_v2.subject_prefix import prefix_for  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PREFIX = prefix_for("English")  # "ENG"

DEFAULT_PDF = (
    r"D:\GPT Folder CSEC\Organized_CSEC_2027\English A\Syllabus"
    r"\csec-english-syllabus-amended-2026-for-exams-2027v2.pdf"
)
DEFAULT_OUT = _REPO_ROOT / "backend" / "ingest_v2" / "syllabus_csvs" / "english.csv"

# Left column boundary — specific objectives sit at x0 < 170
OBJ_COL_MAX = 170

# Module metadata
_MODULE_NAMES = {
    1: "INFORMATIVE DISCOURSE",
    2: "LITERARY DISCOURSE",
    3: "PERSUASIVE DISCOURSE",
}

_SECTION_NAMES = {
    1: "UNDERSTANDING",
    2: "ANALYSING",
    3: "EVALUATING AND CREATING",
}

# Module page ranges (0-indexed, inclusive start, exclusive end)
# Wide enough to cover all three sections within each module
_MODULE_PAGE_RANGES = {
    1: range(24, 35),
    2: range(38, 53),  # Module 3 intro (Skills & Abilities) starts at page 53
    3: range(56, 68),
}

# Section header patterns — match "Understanding (Module N", "Analysing (Module N", "Evaluating and Creating (Module N"
_SECTION_HEADER_RE = re.compile(
    r"^(Understanding|Analysing|Evaluating and Creating)\s*\(Module",
    re.IGNORECASE,
)
_SECTION_MAP = {
    "understanding": 1,
    "analysing": 2,
    "evaluating and creating": 3,
}

# Numbered objective line: starts with "N." at the beginning
_OBJ_NUM_RE = re.compile(r"^(\d+)\.\s+(.+)")

# Lines to skip (noise from headers, column labels, footer elements)
_NOISE = re.compile(
    r"(Students should be able|CXC \d|www\.cxc|cont'd|Duration|Credit Weighting|"
    r"General Objectives|SPECIFIC OBJECTIVES|EXPLANATORY NOTES|"
    r"SUGGESTIONS FOR|LEARNING ACTIVITY|SUGGESTIONS FOR ASSESSMENT|"
    r"MODULE \d:|INFORMATIVE DISCOURSE|LITERARY DISCOURSE|PERSUASIVE DISCOURSE)",
    re.IGNORECASE,
)


def _left_column_lines(page) -> list[str]:
    """Return left-column words reassembled into logical text lines."""
    words = page.get_text("words")
    buckets: dict[int, list[tuple[float, str]]] = {}
    for w in words:
        x0, y0, x1, y1, word, *_ = w
        if x0 < OBJ_COL_MAX:
            b = round(float(y0) / 4) * 4
            buckets.setdefault(b, []).append((x0, word))
    lines = []
    for y in sorted(buckets):
        line = " ".join(w for _, w in sorted(buckets[y]))
        lines.append(line.strip())
    return [l for l in lines if l]


def _detect_section(line: str) -> int | None:
    """Return section number (1/2/3) if this line is a skill section header, else None."""
    m = _SECTION_HEADER_RE.match(line)
    if not m:
        return None
    key = m.group(1).lower()
    return _SECTION_MAP.get(key)


def extract(pdf_path: str) -> list[dict]:
    """Return list of objective dicts from the English A syllabus PDF."""
    pdf = fitz.open(pdf_path)
    objectives: list[dict] = []
    seen: set[str] = set()

    for mod, page_range in _MODULE_PAGE_RANGES.items():
        # Collect ALL left-column lines for the module in one sequential pass
        all_lines: list[str] = []
        for pn in page_range:
            if pn >= len(pdf):
                break
            all_lines.extend(_left_column_lines(pdf[pn]))

        current_section: int | None = None
        current_obj_num: int | None = None
        current_obj_lines: list[str] = []

        def _flush():
            nonlocal current_obj_num, current_obj_lines
            if current_obj_num is None or not current_obj_lines or current_section is None:
                current_obj_num = None
                current_obj_lines = []
                return
            stmt = " ".join(current_obj_lines).strip()
            stmt = re.sub(r";\s*and[,.]?\s*$", "", stmt).strip()
            stmt = stmt.rstrip(";").strip()
            if len(stmt) < 10:
                current_obj_num = None
                current_obj_lines = []
                return
            obj_id = f"{PREFIX}-{mod}.{current_section}.{current_obj_num}"
            if obj_id not in seen:
                seen.add(obj_id)
                objectives.append({
                    "subject": "English",
                    "context": (
                        f"SECTION {mod}.{current_section}: "
                        f"{_SECTION_NAMES[current_section]} — "
                        f"{_MODULE_NAMES[mod]}"
                    ),
                    "objective_number": str(current_obj_num),
                    "objective": stmt,
                    "_module": mod,
                    "_section": current_section,
                    "_obj_num": current_obj_num,
                    "_obj_id": obj_id,
                })
            current_obj_num = None
            current_obj_lines = []

        for line in all_lines:
            # Check for section header transition
            sec = _detect_section(line)
            if sec is not None:
                _flush()
                current_section = sec
                continue

            if _NOISE.search(line):
                continue

            # Only process objective lines once we know which section we're in
            if current_section is None:
                continue

            m = _OBJ_NUM_RE.match(line)
            if m:
                _flush()
                current_obj_num = int(m.group(1))
                rest = m.group(2).strip()
                current_obj_lines = [rest] if rest else []
            elif current_obj_num is not None:
                current_obj_lines.append(line)

        _flush()

    return objectives


_CSV_FIELDS = ["subject", "context", "objective_number", "objective"]


def main():
    ap = argparse.ArgumentParser(description="Extract CSEC English A objectives from PDF.")
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"Reading: {args.pdf}")
    objectives = extract(args.pdf)
    print(f"\nObjectives extracted: {len(objectives)}")

    by_mod: dict[int, dict[int, list]] = {}
    for obj in objectives:
        m, s = obj["_module"], obj["_section"]
        by_mod.setdefault(m, {}).setdefault(s, []).append(obj)

    for mod in sorted(by_mod):
        print(f"\n  MODULE {mod} — {_MODULE_NAMES[mod]}:")
        for sec in sorted(by_mod[mod]):
            objs = by_mod[mod][sec]
            nums = sorted(o["_obj_num"] for o in objs)
            print(f"    {_SECTION_NAMES[sec]}: {len(objs)} objectives (nums: {nums})")

    print("\n--- Numbering gaps ---")
    gaps_found = False
    for mod in sorted(by_mod):
        for sec in sorted(by_mod[mod]):
            nums = sorted(o["_obj_num"] for o in by_mod[mod][sec])
            expected = list(range(1, nums[-1] + 1))
            missing = [n for n in expected if n not in nums]
            if missing:
                print(f"  ENG-{mod}.{sec}: missing {missing}")
                gaps_found = True
    if not gaps_found:
        print("  None detected.")

    print("\n--- All objectives ---")
    for o in objectives:
        print(f"  {o['_obj_id']:22s}  {o['objective'][:90]}")

    if args.dry_run:
        print("\n[dry-run] No CSV written.")
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(
            {k: v for k, v in o.items() if not k.startswith("_")}
            for o in objectives
        )
    print(f"\nCSV written → {out_path}  ({len(objectives)} rows)")


if __name__ == "__main__":
    main()
