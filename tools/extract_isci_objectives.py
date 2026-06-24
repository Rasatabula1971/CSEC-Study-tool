# PHASE: build — called only during ingestion prep (syllabus extraction)
"""
Re-extract Integrated Science syllabus objectives directly from the canonical PDF.

Replaces the 2026-06-18 extraction that truncated Obj 6.7, invented two
booklet-instruction "objectives" from the specimen papers (pages 141/176), and
left unexplained numbering gaps. This reads the PDF's actual table layout with
PyMuPDF block coordinates, so it captures full objective statements from the
SPECIFIC OBJECTIVES column and ignores the adjacent EXPLANATORY NOTES / SUGGESTED
PRACTICAL ACTIVITIES columns and the exam-paper appendix entirely.

STRUCTURE (confirmed against the canonical PDF):
  * Module: every objectives page carries a running header "MODULE N: TITLE".
    Module 1 = ORGANISMS AND LIFE PROCESSES, 2 = ENERGY, 3 = OUR PLANET.
  * Topic : "TOPIC N: TITLE" headers; topic numbers RESET within each module, so
    a topic is only unique as (module, topic).
  * Objective: a left-column block (x0 ~72) beginning "N.M <statement>[;.]". The
    "N.M" itself encodes topic.objective, so topic = N, objective_number = M, and
    module comes from the page header. General objectives ("6. understand ...",
    single-segment) are NOT specific objectives and are excluded.

ID SCHEME (per builder decision — module+topic must disambiguate the reset):
  * section_num   = "{module}-{topic}"          e.g. "2-1"  (hyphen; grouping/dedup)
  * context       = "SECTION {module}.{topic}: {TITLE}"     (dotted, so
                    build_syllabus_csv composes objective_id = prefix-{m}.{t}.{obj})
  * objective_id  = "{PREFIX}-{module}.{topic}.{objective_number_as_printed}"
                    where PREFIX = subject_prefix.prefix_for("Integrated_Science")
                    = "INTSCI" (the framework convention; Bridge files' "ISCI-*"
                    rebind to it). e.g. CXC Module 2 / Topic 1 / Obj 2.3 ->
                    "INTSCI-2.1.3". The third segment is the number after the dot
                    exactly as printed, so it stays traceable to the CXC document.

Output is a master-map-shaped CSV (the input build_syllabus_csv.py consumes), left
as a STANDALONE file so the shared multi-subject master map is not touched. NOTE:
build_syllabus_csv.py's SECTION_RE currently matches digits-only section numbers;
feeding the dotted "SECTION 2.1: ..." context needs its regex widened from
``(\\d+)`` to ``([\\d.]+)`` (a separate, deliberate change — not made here).

  python tools/extract_isci_objectives.py            # writes the CSV + summary
  python tools/extract_isci_objectives.py --dry-run   # summary only, no write

Read-only on the PDF; writes one CSV; never touches the database.
"""

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("ERROR: PyMuPDF (fitz) is not installed. Run: pip install pymupdf")

load_dotenv()

# Use the framework's single source of truth for the objective-id prefix
# (subject_prefix.prefix_for) rather than hard-coding it, so the master map's
# helper objective_id matches what build_syllabus_csv.py will mint.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from backend.ingest_v2.subject_prefix import prefix_for  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

DEFAULT_PDF = (
    r"E:\CSEC_AI_STUDY_PARTNER\03_KNOWLEDGE_BASE\Integrated_Science"
    r"\00_SYLLABUS\csec-integrated-science-syllabus_effectiveforexamsfrom2027.pdf"
)
DEFAULT_OUT = (
    Path(__file__).resolve().parents[1]
    / "backend" / "ingest_v2" / "source_data"
    / "integrated_science_objectives_master_map.csv"
)
SUBJECT = "Integrated_Science"          # canonical subject_id (matches prefix_for + --subject)
PREFIX = prefix_for(SUBJECT)             # "INTSCI" — framework convention, not hard-coded

# Running page header: "MODULE 1: ORGANISMS AND LIFE PROCESSES (cont'd)".
MODULE_RE = re.compile(r"MODULE\s+(\d+)\s*:\s*([A-Z][A-Z '&/\-]+)", re.IGNORECASE)
# Topic header (often embedded in the activities-column block):
#   "...TOPIC 6: HOUSEHOLD CHEMICALS (cont'd)".
TOPIC_RE = re.compile(r"TOPIC\s+(\d+)\s*:\s*([A-Z][A-Z0-9 '&/\-]+?)\s*(?:\(cont|$)")
# A specific objective begins with its CXC number, e.g. "6.7 distinguish ...".
# The number often sits on its own line with the statement wrapping onto indented
# continuation lines, so extraction is line-based (see extract()).
OBJ_NUM_RE = re.compile(r"^(\d+)\.(\d+)\b\s*(.*)$")

# The SPECIFIC OBJECTIVES column starts at x0 ~72 (number) / ~113 (wrapped text).
# The EXPLANATORY NOTES column's left edge VARIES per page (~235 in Modules 1-2 but
# ~214-221 in Module 3), so the boundary is computed per page from the explanatory
# header / "(a)" markers rather than hard-coded (see _objective_col_boundary).
DEFAULT_OBJ_COL_BOUNDARY = 210.0
# Explanatory-column sub-markers: "(a)" ... or "(i)" "(ii)" roman numerals.
EXPL_MARKER_RE = re.compile(r"^\((?:[a-z]|[ivx]{1,4})\)")
CONT_RE = re.compile(r"\(cont.?d\)", re.IGNORECASE)


def _objective_col_boundary(lines, default: float = DEFAULT_OBJ_COL_BOUNDARY) -> float:
    """Left edge of the EXPLANATORY column on this page; objective text is left of it.

    Uses the leftmost of: the 'EXPLANATORY NOTES' header and any '(a)'/'(i)' marker.
    Objective statements never start with those, and the explanatory column is always
    right of the objective column, so the minimum such x0 (minus a hair) cleanly
    splits the two regardless of the per-module layout shift."""
    edges = [x0 for _y, x0, txt in lines
             if txt.upper().startswith("EXPLANATORY") or EXPL_MARKER_RE.match(txt)]
    return (min(edges) - 1.0) if edges else default

# Left-column lines that are page structure, not objective text.
_HEADER_PREFIXES = (
    "MODULE", "TOPIC", "SECTION", "SPECIFIC OBJECTIVES", "EXPLANATORY",
    "SUGGESTED", "PRACTICAL", "ACTIVITIES", "STUDENTS SHOULD",
    "GENERAL OBJECTIVES", "SKILLS AND ABILITIES", "CXC ",
)


def _is_structural(text: str) -> bool:
    """True for left-column lines that are headers/footers, not objective text."""
    up = text.upper()
    if up.startswith(_HEADER_PREFIXES):
        return True
    if "WWW.CXC.ORG" in up:
        return True
    if re.fullmatch(r"\d+", text):          # bare page number
        return True
    if CONT_RE.fullmatch(text):
        return True
    return False


def _clean(text: str) -> str:
    return " ".join(text.split())


# De-hyphenation of PDF line-wrap artifacts. When a word is split across lines by
# a hyphen, the continuation join leaves "stem- tail" (hyphen + space). Two cases:
#   * a solid word split by the wrap  -> rejoin into one word ("inter- conversion"
#     -> "interconversion"). Only stems in SOLID_WRAP_PREFIXES are joined.
#   * a genuinely hyphenated compound wrapped at its hyphen -> keep the hyphen,
#     drop only the stray space ("non- communicable" -> "non-communicable").
# Conservative by design: the DEFAULT is to keep the hyphen, so legitimate
# compounds (non-/pre-/post-/self-, etc.) are never fused. Compounds that were
# never wrapped have no space and are untouched (the regex requires the space).
SOLID_WRAP_PREFIXES = {
    "inter", "intra", "multi", "photo", "electro", "thermo", "hydro",
    "micro", "macro", "bio", "geo", "trans", "over", "under",
}
_HYPHEN_WRAP_RE = re.compile(r"([A-Za-z]+)-\s+([a-z])")


def dehyphenate(text: str) -> str:
    """Repair line-wrap hyphenation conservatively (see SOLID_WRAP_PREFIXES)."""
    def _repl(m: re.Match) -> str:
        stem, tail = m.group(1), m.group(2)
        if stem.lower() in SOLID_WRAP_PREFIXES:
            return f"{stem}{tail}"          # rejoin a split solid word
        return f"{stem}-{tail}"             # keep hyphen, drop the wrap space
    return _HYPHEN_WRAP_RE.sub(_repl, text)


def clean_title(raw: str) -> str:
    raw = CONT_RE.sub("", raw)
    # drop trailing column-header words that bleed into the same block
    raw = re.split(r"\b(SPECIFIC|EXPLANATORY|SUGGESTED|STUDENTS)\b", raw)[0]
    return _clean(raw).rstrip(" :").upper()


def extract(pdf_path: Path):
    """Return (objectives, headers_sample, module_titles).

    objectives: list of dicts with module/topic/objective_number/objective/page.
    """
    doc = fitz.open(pdf_path)
    objectives = []
    title_map: dict[tuple[int, int], str] = {}     # (module, topic) -> title
    module_titles: dict[int, str] = {}
    headers_sample: list[str] = []
    seen_topic_headers: set[tuple[int, int, str]] = set()

    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            page_text = page.get_text("text")

            mod_match = MODULE_RE.search(page_text)
            if not mod_match:
                continue                            # not an objectives page (skips appendix)
            module = int(mod_match.group(1))
            module_titles.setdefault(module, clean_title(mod_match.group(2)))

            # capture topic titles present on this page (for section_title lookup)
            for tm in TOPIC_RE.finditer(page_text):
                topic = int(tm.group(1))
                title = clean_title(tm.group(2))
                if title:
                    title_map.setdefault((module, topic), title)
                    key = (module, topic, title)
                    if key not in seen_topic_headers and len(headers_sample) < 40:
                        seen_topic_headers.add(key)
                        headers_sample.append(
                            f"p{pno + 1}  MODULE {module}  TOPIC {topic}: {title}")

            # Line-based objective capture from the SPECIFIC OBJECTIVES column only.
            # A line beginning "N.M" opens an objective; subsequent left-column,
            # non-structural lines are its wrapped continuation (the number and the
            # statement live on separate lines, and the statement wraps). Lines at
            # x0 >= OBJ_COL_MAX_X0 are other columns and are skipped, so explanatory
            # notes never bleed into the objective text.
            dd = page.get_text("dict")
            lines = []
            for blk in dd["blocks"]:
                for ln in blk.get("lines", []):
                    txt = "".join(s["text"] for s in ln["spans"])
                    if txt.strip():
                        lines.append((ln["bbox"][1], ln["bbox"][0], txt))
            lines.sort(key=lambda t: (t[0], t[1]))   # reading order: top->bottom, left->right
            boundary = _objective_col_boundary(lines)

            current = None
            for _y0, x0, raw in lines:
                txt = _clean(raw)
                if x0 >= boundary:
                    continue                         # EXPLANATORY / ACTIVITIES column
                if _is_structural(txt):
                    current = None                   # header/footer ends any open objective
                    continue
                m = OBJ_NUM_RE.match(txt)
                if m:
                    current = {
                        "module": module,
                        "topic": int(m.group(1)),
                        "objective_number": m.group(2),
                        "objective": _clean(m.group(3)),
                        "page": pno + 1,
                    }
                    objectives.append(current)
                elif current is not None:
                    current["objective"] = _clean(current["objective"] + " " + txt)
    finally:
        doc.close()

    return objectives, headers_sample, title_map, module_titles


def build_rows(objectives, title_map):
    """Return (rows, dehyphenation_changes).

    dehyphenation_changes is a list of (objective_id, before, after) for every
    statement the de-hyphenation pass altered, so the change can be spot-checked.
    """
    rows = []
    changes = []
    for o in objectives:
        module, topic = o["module"], o["topic"]
        obj_num = o["objective_number"]
        title = title_map.get((module, topic), "(title not detected)")
        objective_id = f"{PREFIX}-{module}.{topic}.{obj_num}"
        raw = o["objective"]
        fixed = dehyphenate(raw)
        if fixed != raw:
            changes.append((objective_id, raw, fixed))
        rows.append({
            "subject": SUBJECT,
            "context": f"SECTION {module}.{topic}: {title}",
            "section_num": f"{module}-{topic}",
            "section_title": title,
            "module": module,
            "topic": topic,
            "objective_number": obj_num,
            "objective_id": objective_id,
            "page": o["page"],
            "objective": fixed,
        })
    rows.sort(key=lambda r: (r["module"], r["topic"], int(r["objective_number"])))
    return rows, changes


OUTPUT_COLUMNS = [
    "subject", "context", "section_num", "section_title", "module", "topic",
    "objective_number", "objective_id", "page", "objective",
]


def write_csv(rows, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def report(rows, headers_sample, module_titles, out_path, wrote: bool, changes=None):
    print("=" * 78)
    print("INTEGRATED SCIENCE — objective re-extraction")
    print("=" * 78)

    changes = changes or []
    print(f"\nDe-hyphenation changes ({len(changes)}):")
    if not changes:
        print("  (none)")
    for oid, before, after in changes:
        print(f"  {oid}")
        print(f"      old: {before}")
        print(f"      new: {after}")

    print("\nDetected module headers:")
    for mod in sorted(module_titles):
        print(f"  MODULE {mod}: {module_titles[mod]}")

    print(f"\nDetected (module, topic) section headers ({len(headers_sample)} shown) "
          f"— confirm structure detection:")
    for h in headers_sample:
        print(f"  - {h}")

    # per module/topic breakdown
    by_topic = defaultdict(list)
    for r in rows:
        by_topic[(r["module"], r["topic"])].append(r)
    print(f"\nObjectives per (module.topic):")
    for key in sorted(by_topic):
        mod, top = key
        objs = by_topic[key]
        nums = ", ".join(o["objective_number"] for o in objs)
        title = objs[0]["section_title"]
        print(f"  M{mod}.T{top:<2} {title[:42]:<42} n={len(objs):<2}  obj#: {nums}")

    # duplicate verification (the builder asked for this explicitly)
    ids = [r["objective_id"] for r in rows]
    pair = [(r["section_num"], r["objective_id"]) for r in rows]
    dup_ids = sorted({i for i in ids if ids.count(i) > 1})
    dup_pairs = sorted({p for p in pair if pair.count(p) > 1})

    # completeness flags: an objective whose text does not end with a terminator
    # may have been clipped — surface for manual PDF check (none expected).
    unterminated = [r for r in rows if not r["objective"].rstrip().endswith((";", ".", ","))]

    print("\n" + "-" * 78)
    print(f"TOTAL objectives extracted : {len(rows)}")
    print(f"Modules                    : {len(module_titles)}")
    print(f"Distinct (module.topic)    : {len(by_topic)}")
    print(f"Distinct objective_id      : {len(set(ids))}")
    print(f"Duplicate objective_id     : {len(dup_ids)}  {dup_ids if dup_ids else '(none)'}")
    print(f"Duplicate (section_num,id) : {len(dup_pairs)}  "
          f"{dup_pairs if dup_pairs else '(none)'}")
    print(f"Zero-duplicate check       : "
          f"{'PASS' if not dup_ids and not dup_pairs else '*** FAIL ***'}")

    if unterminated:
        print(f"\n! {len(unterminated)} objective(s) not ending in ';'/'.'/',' "
              f"(verify against PDF):")
        for r in unterminated:
            print(f"    {r['objective_id']} (p{r['page']}): {r['objective'][:70]!r}")
    else:
        print("Statement-termination check: OK (every objective ends ';'/'.'/',')")

    print("\nSample objectives (first 3 of each module):")
    shown = defaultdict(int)
    for r in rows:
        if shown[r["module"]] < 3:
            shown[r["module"]] += 1
            print(f"  {r['objective_id']:<14} (p{r['page']}) {r['objective'][:62]}")

    print("\n" + "=" * 78)
    if wrote:
        print(f"WROTE master-map CSV: {out_path}")
    else:
        print(f"DRY-RUN — no file written (would write {len(rows)} rows to {out_path})")
    print("Next: build_syllabus_csv.py --subject Integrated_Science on this file "
          "(widen its SECTION_RE to accept dotted section numbers first).")
    print("=" * 78)

    return not dup_ids and not dup_pairs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", default=DEFAULT_PDF, help="canonical syllabus PDF")
    ap.add_argument("--output", default=str(DEFAULT_OUT), help="master-map CSV output")
    ap.add_argument("--dry-run", action="store_true", help="summary only, do not write")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        sys.exit(f"ERROR: syllabus PDF not found: {pdf_path}")

    objectives, headers_sample, title_map, module_titles = extract(pdf_path)
    if not objectives:
        sys.exit("ERROR: no objectives extracted — structure detection failed; "
                 "re-check MODULE/TOPIC/objective patterns against the PDF.")

    rows, changes = build_rows(objectives, title_map)

    out_path = Path(args.output)
    wrote = False
    if not args.dry_run:
        write_csv(rows, out_path)
        wrote = True

    ok = report(rows, headers_sample, module_titles, out_path, wrote, changes)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
