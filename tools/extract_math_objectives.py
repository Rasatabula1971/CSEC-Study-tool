# PHASE: build — called only during ingestion prep (syllabus extraction)
"""
Extract CSEC Mathematics syllabus objectives from the canonical 2027-effective PDF.

Structure confirmed against the PDF:
  * Module: "MODULE N: TITLE" running header (3 modules).
  * Section: "N. SECTION_TITLE" heading within each module (resets per module).
    Module 1: 1=Number Theory, 2=Consumer Arithmetic, 3=Sets, 4=Measurement,
              5=Algebra 1, 6=Introduction to Graphs
    Module 2: 1=Statistics 1, 2=Algebra 2, 3=Relations/Functions/Graphs 1,
              4=Geometry/Trig 1, 5=Vectors/Matrices 1
    Module 3: 1=Statistics 2, 2=Algebra/Relations 2, 3=Geometry/Trig 2,
              4=Vectors/Matrices 2
  * Objective: left-column block (x0 ≈ 67-77) beginning "N.M <statement>[;.]"
    where N = section number, M = objective number. Section numbers reset per module
    so a unique ID requires all three: (module, section, objective_num).

ID scheme (mirrors INTSCI convention):
  section_num  = "{module}-{section}"          e.g. "1-2"
  context      = "SECTION {module}.{section}: {TITLE}"
  objective_id = "MATH-{module}.{section}.{obj_num}"  e.g. "MATH-1.2.1"

Writes one CSV (the input build_syllabus_csv.py consumes).

  python tools/extract_math_objectives.py            # writes CSV + summary
  python tools/extract_math_objectives.py --dry-run  # summary only, no write

Read-only on the PDF; never touches the database.
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
from tools.extraction.syllabus_extractor_base import (  # noqa: E402
    _detect_gutter,
    _left_text,
    validate_objectives,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PREFIX = prefix_for("Mathematics")  # "MATH"

DEFAULT_PDF = (
    r"D:\GPT Folder CSEC\Organized_CSEC_2027\Mathematics\Syllabus"
    r"\csec-mathematics-syllabus_effectiveforexamsfrom2027.pdf"
)
DEFAULT_OUT = _REPO_ROOT / "backend" / "ingest_v2" / "syllabus_csvs" / "mathematics.csv"

# Column extraction (_detect_gutter / _left_text) and the QA pass
# (validate_objectives) live in tools/extraction/syllabus_extractor_base.py —
# the canonical, subject-agnostic implementation imported above.

# Objective line pattern: "N.M" at the start of a text fragment.
# Captures section_num, obj_num, and the rest of the line.
_OBJ_MARKER_RE = re.compile(r"(?m)^(\d+)\.(\d+)\s+")

# Module header e.g. "MODULE 1: FUNDAMENTALS..."
_MODULE_RE = re.compile(r"MODULE\s+(\d+)\s*:", re.IGNORECASE)

# Section header e.g. "3. SETS" or "3. SETS (cont'd)" — numbered, ALL-CAPS
# A section header is a digit, period, whitespace, then capital letters.
_SECTION_RE = re.compile(r"^(\d+)\.\s+([A-Z][A-Z0-9,/()\s']+?)(?:\s*\n|\s*$)", re.MULTILINE)

# Lines that signal we've left the objective text and entered notes / boilerplate
_NOISE_STARTS = re.compile(
    r"(Suggested Teaching|Students should be able|SPECIFIC OBJECTIVES|"
    r"CONTENT/EXPLANATORY|www\.cxc\.org|CXC \d+/G/SYLL|cont'd\)|"
    r"General objectives|On completion of this)",
    re.IGNORECASE,
)

# Sub-item lines to fold back into the parent objective (e.g. "(a) discount;")
_SUBITEM_RE = re.compile(r"^\s*\([a-z]\)\s+")

# Trailing connector we want to strip from the last objective in a section
_TRAILING_AND = re.compile(r";\s*and[,.]?\s*$", re.IGNORECASE)


def _clean(text: str) -> str:
    """Normalise whitespace and remove leftover noise fragments."""
    # Replace multiple spaces / tabs with single space
    text = re.sub(r"[ \t]+", " ", text)
    # Collapse runs of newlines to single space
    text = re.sub(r"\n+", " ", text)
    text = text.strip()
    # Strip trailing "; and," connector (the last obj in a section often has it)
    text = _TRAILING_AND.sub("", text).strip()
    # Strip trailing bare semicolons that aren't part of the statement
    text = text.rstrip(";").strip()
    return text


def _extract_obj_statement(raw: str) -> str:
    """
    Given the left-column text immediately after an objective marker (e.g. after
    "1.2 "), return the full objective statement.

    Because _left_text now yields a CLEAN left column (no bled-in note text), the
    statement is simply every line up to the next stop signal: a new objective
    marker, a section header, or note/boilerplate. All wrapped continuation lines
    and sub-items (a)/(b)/(c)… are folded in unconditionally — the previous
    ';'-termination heuristic wrongly dropped wrapped tails like "…vertices of /
    solids; and, / (e) classes of solids", so it is gone.
    """
    kept = []
    for line in raw.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _NOISE_STARTS.search(stripped):
            break
        if _OBJ_MARKER_RE.match(stripped):       # next objective marker
            break
        if re.match(r"^\d+\.\s+[A-Z]", stripped):  # next section header "N. CAPS"
            break
        kept.append(stripped)

    result = " ".join(kept).strip()
    # Remove pure numeric/symbolic trailing fragments like "7 2." or "3."
    result = re.sub(r"\s+\d[\d.\s]*$", "", result).strip()
    return result


def extract(pdf_path: str) -> list[dict]:
    """Return a list of objective dicts from the Math syllabus PDF."""
    pdf = fitz.open(pdf_path)
    objectives = []

    current_module = None
    current_section_num = None
    current_section_title = None

    # Track seen objective IDs to catch duplicates
    seen_ids: set[str] = set()

    # Section title map — filled as we discover them
    section_titles: dict[tuple, str] = {}

    # Objectives are on pages 1-52 only; from page 53 onwards are SBA guidelines,
    # appendices, and specimen papers that contain decimal numbers which would be
    # misidentified as objective markers.
    MAX_OBJECTIVE_PAGE = 52  # 1-indexed; page 53+ = non-objective content

    for page_num in range(min(len(pdf), MAX_OBJECTIVE_PAGE)):
        page = pdf[page_num]
        text = _left_text(page, _detect_gutter(page))

        # --- Detect module ---
        m = _MODULE_RE.search(text)
        if m:
            new_module = int(m.group(1))
            if new_module != current_module:
                current_module = new_module
                # Module change resets section tracking
                current_section_num = None
                current_section_title = None

        if current_module is None:
            continue  # Haven't reached objective pages yet

        # --- Detect section headers ---
        for sm in _SECTION_RE.finditer(text):
            sec_num = int(sm.group(1))
            sec_title = sm.group(2).strip().rstrip("(").strip()
            # Reject if this looks like a sub-item number (too small and
            # we already have a section — e.g., "3. use Venn diagrams…" lowercase)
            if sm.group(2)[0].islower():
                continue
            key = (current_module, sec_num)
            if key not in section_titles:
                section_titles[key] = sec_title
            current_section_num = sec_num
            current_section_title = section_titles[key]

        # --- Extract objectives ---
        # Split the page text at every objective marker
        segments = _OBJ_MARKER_RE.split(text)
        # segments pattern after split with 2 groups:
        # [pre_text, sec, obj, post_text, sec, obj, post_text, ...]
        if len(segments) < 4:
            continue  # No objectives on this page

        i = 1  # start at first capture group
        while i < len(segments) - 2:
            sec_num_str = segments[i]
            obj_num_str = segments[i + 1]
            obj_text_raw = segments[i + 2]

            try:
                sec_num = int(sec_num_str)
                obj_num = int(obj_num_str)
            except ValueError:
                i += 3
                continue

            # Sanity guards — no real section is 0, and no section exceeds 25 objectives
            if sec_num == 0 or obj_num == 0 or obj_num > 25:
                i += 3
                continue

            # Use detected section; fall back to sec_num from the marker
            if current_module and sec_num == current_section_num:
                title = current_section_title or f"Section {sec_num}"
            elif (current_module, sec_num) in section_titles:
                title = section_titles[(current_module, sec_num)]
                current_section_num = sec_num
                current_section_title = title
            else:
                title = f"Section {current_module}.{sec_num}"

            # Build the statement from the raw text following the marker
            stmt = _extract_obj_statement(obj_text_raw)
            if not stmt:
                i += 3
                continue

            # Skip false rows: too short, looks like booklet boilerplate, or
            # suspiciously short numbers that are likely page/formula fragments
            if len(stmt) < 8:
                i += 3
                continue
            if re.match(r"^\d", stmt):
                # Starts with a digit — likely a formula fragment, not an objective
                i += 3
                continue

            stmt = _clean(stmt)
            if not stmt:
                i += 3
                continue

            obj_id = f"{PREFIX}-{current_module}.{sec_num}.{obj_num}"

            if obj_id in seen_ids:
                i += 3
                continue
            seen_ids.add(obj_id)

            objectives.append({
                # master-map format — input to build_syllabus_csv.py
                "subject": "Mathematics",
                "context": f"SECTION {current_module}.{sec_num}: {title}",
                "objective_number": str(obj_num),
                "objective": stmt,
                # extra fields for the summary / gap check (not written to CSV)
                "_module": current_module,
                "_section": sec_num,
                "_obj_num": obj_num,
                "_obj_id": obj_id,
                "_page": page_num + 1,
            })

            i += 3

    return objectives


# Master-map format columns (input to build_syllabus_csv.py)
_CSV_FIELDS = ["subject", "context", "objective_number", "objective"]


def main():
    ap = argparse.ArgumentParser(description="Extract CSEC Mathematics objectives from PDF.")
    ap.add_argument("--pdf", default=DEFAULT_PDF, help="Path to the syllabus PDF")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="Output CSV path")
    ap.add_argument("--dry-run", action="store_true", help="Print summary only; do not write CSV")
    args = ap.parse_args()

    print(f"Reading: {args.pdf}")
    objectives = extract(args.pdf)
    print(f"\nObjectives extracted: {len(objectives)}")

    # --- Summary by module and section ---
    by_module: dict[int, dict[int, list]] = {}
    for obj in objectives:
        mod, sec = obj["_module"], obj["_section"]
        by_module.setdefault(mod, {}).setdefault(sec, []).append(obj)

    for mod in sorted(by_module):
        print(f"\n  MODULE {mod}:")
        for sec in sorted(by_module[mod]):
            objs = by_module[mod][sec]
            nums = [o["objective_number"] for o in objs]
            title = objs[0]["context"].split(": ", 1)[-1]
            print(f"    Section {sec} ({title}): {len(objs)} objectives — {nums}")

    # --- Gap detection ---
    print("\n--- Numbering gaps ---")
    gaps_found = False
    for mod in sorted(by_module):
        for sec in sorted(by_module[mod]):
            nums = sorted(o["_obj_num"] for o in by_module[mod][sec])
            expected = list(range(1, nums[-1] + 1))
            missing = [n for n in expected if n not in nums]
            if missing:
                print(f"  MATH-{mod}.{sec}: missing {missing}")
                gaps_found = True
    if not gaps_found:
        print("  None detected.")

    # --- Quality-assurance pass ---
    print("\n--- QA: suspicious statements (review before loading) ---")
    flags = validate_objectives(objectives)
    if flags:
        for oid, reason, stmt in flags:
            print(f"  [{oid}] {reason}")
            print(f"      {stmt!r}")
        print(f"  {len(flags)} flag(s) across "
              f"{len({f[0] for f in flags})} objective(s) — verify against the PDF.")
    else:
        print("  None — all statements pass the truncation/bleed/garble checks.")

    # --- Sample objectives ---
    print("\n--- First 5 objectives ---")
    for o in objectives[:5]:
        print(f"  {o['_obj_id']:22s}  {o['objective'][:80]}")

    print("\n--- Last 5 objectives ---")
    for o in objectives[-5:]:
        print(f"  {o['_obj_id']:22s}  {o['objective'][:80]}")

    if args.dry_run:
        print("\n[dry-run] No CSV written.")
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows({k: v for k, v in o.items() if not k.startswith("_")}
                         for o in objectives)
    print(f"\nCSV written → {out_path}  ({len(objectives)} rows)")


if __name__ == "__main__":
    main()
