"""
tools/extract_mark_scheme.py
=============================
PHASE: build

Parse the embedded mark-scheme pages from a CXC syllabus PDF and produce a
reviewable CSV at {REPORTS_ROOT}/{subject}_mark_scheme_review.csv.

Writes NOTHING to mark_points or any locked DB table — output is CSV only.

Usage:
    python tools/extract_mark_scheme.py --subject Economics
    python tools/extract_mark_scheme.py --subject Economics --dry-run
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# ── LLM fallback ────────────────────────────────────────────────────────────
# Reuse the existing prose-extractor helper rather than re-implementing routing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "db"))

try:
    from extract_prose_markpoints import parse_points  # defensive JSON parser
    from ollama_client import ollama_chat
    _OLLAMA_AVAILABLE = True
except ImportError:
    _OLLAMA_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    _FITZ_AVAILABLE = True
except ImportError:
    _FITZ_AVAILABLE = False

# ── Constants ────────────────────────────────────────────────────────────────
PAGE_RANGES_FILE = Path(__file__).parent / "mark_scheme_page_ranges.json"
CSV_COLUMNS = [
    "question_num", "question_group", "question_block_id",
    "question_part", "part_occurrence", "so_codes", "point_text",
    "marks_value", "point_order", "profile", "source_page",
    "raw_excerpt", "mapped_objective_id", "verified",
    "parser_artifact", "excluded_reason", "needs_manual_entry",
]

# Stage 3 mark_point_id formula (documented here as the canonical reference):
#   {subject_prefix}-{objective_num}-qb{question_block_id}{question_part}v{part_occurrence}-mp{point_order}
#   e.g.  ECON-1.6-qb1(b)(i)v1-mp1
#          ECON-6.9-qb6(a)v2-mp1   ← second (a) alternative within the same Q6 block

# Regex patterns
_RE_QUESTION   = re.compile(r"^\s*Question\s+(\d+)", re.IGNORECASE | re.MULTILINE)
_RE_SO         = re.compile(r"S\.O[:\s]+([0-9.,\s]+)", re.IGNORECASE)
_RE_PART       = re.compile(r"^\s*\(([a-z])\)\s*(?:\(([ivxlIVXL]+)\))?", re.MULTILINE)
_RE_MARK_ALLOC = re.compile(r"(\d+)\s+marks?", re.IGNORECASE)
_RE_BULLET     = re.compile(r"^\s*[•\-\*?]\s+(.+)", re.MULTILINE)
_RE_NUMBERED   = re.compile(r"^\s*\d+\.\s+(.+)", re.MULTILINE)
_RE_FOR_EACH   = re.compile(
    r"[Ff]or\s+(?:a\s+)?(?:correct|each|complete|partial|excellent|clear|stating|listing|identifying).*?(\d+)\s+marks?",
    re.IGNORECASE | re.DOTALL,
)
_RE_PROFILE_COL = re.compile(r"\b(KC|IA|APP)\b")

# ── Row classification (parser_artifact / excluded_reason) ──────────────────
# See MARK_SCHEME_BUILD_PLAN.md "Row classification scheme" — the four states
# are mutually exclusive.  parser_artifact=1 means "structural rubric noise the
# parser captured as if it were content" (e.g. the bare word "Total", a bracket
# placeholder).  excluded_reason means "the row's *source* is wrong" — usually
# exam-cover boilerplate absorbed from a document-structure overrun.  A row can
# match both heuristics (a bare "Total" line that happens to sit inside a
# contaminated block) -- excluded_reason wins, because it is the stronger
# signal (the source is wrong, which subsumes "the text itself is noise").
_ARTIFACT_EXACT_PATTERNS = {"total", "totals", "each"}
_ARTIFACT_BRACKET_RE = re.compile(r"^\[\d+\]$")

# PDF line-break fragmentation patterns: text that reads like a formula echo
# or a sentence fragment torn out of its surrounding context by a column/line
# break, rather than a complete, checkable mark point. CXC mark points are
# written as complete clauses starting with a capital letter or a command
# word -- a bare arithmetic residue, a dangling empty parenthetical, or a
# fragment starting mid-clause (lowercase) are all symptoms of the same
# underlying failure, not three unrelated ones.
_ARTIFACT_ARITHMETIC_ECHO_RE      = re.compile(r"^[\d\s=x+\-*/]+$", re.IGNORECASE)
_ARTIFACT_EMPTY_PARENTHETICAL_RE = re.compile(r"\(\)\s*$")

_EXCLUDED_BOILERPLATE_PATTERNS = [
    "answer all the questions",
    "silent electronic calculators may be used",
    "number each answer in your booklet correctly",
]


def classify_artifact_and_exclusion(point_text: str, raw_excerpt: str = "",
                                    *, is_list_continuation: bool = False) -> tuple[str, str]:
    """Classify a freshly-extracted row as parser_artifact and/or excluded_reason.

    `point_text` is checked against known structural-noise patterns:
      - exact rubric-label matches ("total", "each", ...) or a bare bracket
        placeholder ("[1]")
      - a bare arithmetic echo left behind by a line break inside a worked
        calculation (e.g. "= 2 x 2 =", "2 x 3 =")
      - a dangling empty parenthetical at the end of the text (e.g.
        "be poor. ()") -- the PDF's citation/example marker survived, its
        content did not
      - text starting with a lowercase letter -- CXC mark-scheme prose is
        written as complete clauses starting with a capital letter or a
        command word, so a lowercase start is a strong signal the row is a
        torn-off continuation of a line/column-wrapped sentence (e.g.
        "of living each", "a small food stall holder to a restaurant.()").
        Pass `is_list_continuation=True` when the caller already knows a
        lowercase-leading fragment is a deliberate, legitimate continuation
        (e.g. a bullet whose source line always starts mid-clause) so this
        specific check is skipped for that row; the other checks still apply.

    `raw_excerpt` (the surrounding ~200 chars of the segment) is checked against
    known exam-booklet boilerplate -- contamination is a property of the
    *source section*, not the isolated point text, so it can fire even when
    point_text itself looks like inert noise (e.g. "Total").

    Precedence: if excluded_reason fires, parser_artifact is forced to "0"
    regardless of whether any artifact pattern also matched.  The two fields
    are mutually exclusive in the CSV -- lock_mark_scheme.py refuses to lock
    any row that violates this invariant. Among the artifact checks
    themselves, they are combined with OR into a single "0"/"1" flag, so a
    row matching more than one pattern (e.g. the bare word "each", which is
    both an exact-match rubric label and lowercase-leading) is still just
    parser_artifact=1 once -- never double-counted.

    Returns (parser_artifact, excluded_reason) as ("0"|"1", str).
    """
    haystack = raw_excerpt.lower()
    excluded_reason = ""
    for phrase in _EXCLUDED_BOILERPLATE_PATTERNS:
        if phrase in haystack:
            excluded_reason = "contaminated_exam_instructions"
            break

    if excluded_reason:
        return "0", excluded_reason

    stripped = point_text.strip()
    normalized = stripped.lower().rstrip(".:")

    is_artifact = (
        normalized in _ARTIFACT_EXACT_PATTERNS
        or bool(_ARTIFACT_BRACKET_RE.match(normalized))
        or bool(_ARTIFACT_ARITHMETIC_ECHO_RE.match(stripped))
        or bool(_ARTIFACT_EMPTY_PARENTHETICAL_RE.search(stripped))
        or (not is_list_continuation and bool(stripped) and stripped[0].islower())
    )
    return ("1" if is_artifact else "0"), ""


LLM_SYSTEM = (
    "You are a CSEC examiner assistant. You will be given a section of an official "
    "CXC mark scheme for an Economics Paper 2 question part. Extract each individual "
    "mark point a student must state to earn marks. Each mark point should be one "
    "clear, concise statement. Return ONLY a JSON array of objects, each with keys "
    '"point_text" (string) and "marks_value" (integer, usually 1 or 2). '
    'Example: [{"point_text": "GDP = C + I + G + (X - M)", "marks_value": 1}]'
)


# ── DB helpers ───────────────────────────────────────────────────────────────
def open_db() -> sqlite3.Connection:
    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path!r}. Check .env DB_PATH.")
    try:
        import sqlite_vec
        db = sqlite3.connect(db_path)
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except ImportError:
        db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def map_so_to_objective(db: sqlite3.Connection, subject_id: str, so_codes: list[str]) -> str:
    """Resolve S.O. codes like ['1.6', '1.8'] -> 'ECON-1.6,ECON-1.8'.

    The DB stores objective_num as the bare number within the section ('6'),
    not the dotted 'section.num' form ('1.6').  The objective_id IS the dotted
    form prefixed by the subject code (e.g. 'ECON-1.6'), so we construct the
    candidate id directly and validate it exists — no objective_num join needed.
    """
    # Build the subject prefix once (e.g. 'Economics' -> 'ECON')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "ingest_v2"))
    try:
        from subject_prefix import prefix_for
        prefix = prefix_for(subject_id)
    except Exception:
        # Fallback: take uppercase first four chars (never reached on a locked subject)
        prefix = subject_id[:4].upper()

    mapped = []
    for code in so_codes:
        code = code.strip()
        candidate_id = f"{prefix}-{code}"
        row = db.execute(
            "SELECT objective_id FROM objectives WHERE objective_id = ? LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if row:
            mapped.append(row["objective_id"])
    return ",".join(mapped)


# ── S.O. parsing ─────────────────────────────────────────────────────────────
def parse_so_codes(so_text: str) -> list[str]:
    """Parse 'S.O: 1.6, 1.8, 2.3' into ['1.6', '1.8', '2.3']."""
    codes = []
    for token in re.split(r"[,\s]+", so_text.strip()):
        token = token.strip().rstrip(".")
        if re.match(r"^\d+\.\d+$", token):
            codes.append(token)
    return codes


# ── Mark-point extraction from one part's text ───────────────────────────────
def _extract_mark_alloc(text: str) -> int:
    """Sum up all explicit mark allocations in a text block."""
    total = 0
    # Look for explicit "N marks" allocations, picking the last/largest stated
    allocs = [int(m.group(1)) for m in _RE_MARK_ALLOC.finditer(text)]
    if allocs:
        total = max(allocs)
    return total


def _extract_bullets(text: str) -> list[str]:
    """Extract bullet/dash/numbered points from text."""
    points = []
    for m in _RE_BULLET.finditer(text):
        pt = m.group(1).strip()
        if pt:
            points.append(pt)
    for m in _RE_NUMBERED.finditer(text):
        pt = m.group(1).strip()
        if pt:
            points.append(pt)
    # Also pick up inline numeric-answer lines like "0 tons of sugar  1 mark"
    for line in text.splitlines():
        line = line.strip()
        if _RE_MARK_ALLOC.search(line) and not any(
            kw in line.lower() for kw in ["for ", "mark scheme", "question", "s.o"]
        ):
            # Line contains a standalone answer + mark value
            answer = _RE_MARK_ALLOC.sub("", line).strip().rstrip(" /-")
            if answer and len(answer) > 2:
                points.append(answer)
    return list(dict.fromkeys(points))  # deduplicate, preserve order


def _detect_profile(text: str) -> str | None:
    """Detect KC/IA/APP profile tag on the page (very rough — column headers)."""
    m = _RE_PROFILE_COL.search(text)
    return m.group(1) if m else None


def llm_extract_mark_points(section_text: str, expected_marks: int) -> list[dict]:
    """
    LLM fallback for parts where structural parse found fewer points than expected.
    Returns list of {point_text, marks_value} dicts.
    Raises on malformed JSON (caller handles).
    """
    if not _OLLAMA_AVAILABLE:
        raise RuntimeError("ollama_client not available for LLM fallback")

    # Check CLOUD_MODE for routing (mirrors grade.py / llm_router pattern)
    cloud_mode = os.getenv("CLOUD_MODE", "0").strip() == "1"
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    prompt = (
        f"The following is a section of a CXC Economics mark scheme for a question "
        f"part worth {expected_marks} mark(s). Extract every individual mark point.\n\n"
        f"{section_text}"
    )

    _GEMINI_TIMEOUT = 60  # seconds — prevents a hung Gemini call blocking the whole run

    if cloud_mode and gemini_key:
        try:
            from gemini_client import gemini_chat
            import threading

            result_holder: list = []
            exc_holder:    list = []

            def _call():
                try:
                    result_holder.append(
                        gemini_chat([{"role": "user", "content": prompt}], system=LLM_SYSTEM)
                    )
                except Exception as e:
                    exc_holder.append(e)

            t = threading.Thread(target=_call, daemon=True)
            t.start()
            t.join(timeout=_GEMINI_TIMEOUT)
            if t.is_alive():
                raise RuntimeError(f"Gemini call timed out after {_GEMINI_TIMEOUT}s")
            if exc_holder:
                raise exc_holder[0]
            raw = result_holder[0]
        except Exception:
            raw = ollama_chat([{"role": "user", "content": prompt}], system=LLM_SYSTEM)
    else:
        raw = ollama_chat([{"role": "user", "content": prompt}], system=LLM_SYSTEM)

    # parse_points returns list[str]; we need list[dict]
    # Try full JSON parse first, then fall back to the robust parse_points helper
    import json as _json
    try:
        data = _json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
        if isinstance(data, list):
            result = []
            for item in data:
                if isinstance(item, dict) and "point_text" in item:
                    result.append({
                        "point_text": str(item["point_text"]).strip(),
                        "marks_value": int(item.get("marks_value", 1)),
                    })
                elif isinstance(item, str):
                    result.append({"point_text": item.strip(), "marks_value": 1})
            if result:
                return result
    except Exception:
        pass

    # Fallback: treat each string from parse_points as a 1-mark point
    strings = parse_points(raw)
    return [{"point_text": s, "marks_value": 1} for s in strings]


# ── Main structural parser ───────────────────────────────────────────────────
def parse_mark_scheme(
    pdf_path: str,
    start_page: int,
    end_page: int,
    *,
    dry_run: bool = False,
) -> list[dict]:
    """
    Parse the mark-scheme pages from a CXC syllabus PDF.

    Returns a list of row dicts matching CSV_COLUMNS (except mapped_objective_id
    and verified, which are filled in later).
    """
    if not _FITZ_AVAILABLE:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")

    doc = fitz.open(pdf_path)
    rows: list[dict] = []

    # ── Collect text per page in the range ──────────────────────────────────
    pages_text: list[tuple[int, str]] = []  # (1-based page num, text)
    for i in range(start_page - 1, min(end_page, len(doc))):
        pages_text.append((i + 1, doc[i].get_text()))

    full_text = "\n".join(text for _, text in pages_text)

    # Build a page-lookup so we can record source_page per extracted point
    page_starts: list[tuple[int, int]] = []  # (char_offset, page_num)
    offset = 0
    for page_num, text in pages_text:
        page_starts.append((offset, page_num))
        offset += len(text) + 1  # +1 for the join "\n"

    def _char_to_page(char_pos: int) -> int:
        page_num = page_starts[0][1]
        for off, pn in page_starts:
            if off > char_pos:
                break
            page_num = pn
        return page_num

    # ── Split into per-question blocks ──────────────────────────────────────
    q_splits = list(_RE_QUESTION.finditer(full_text))
    if not q_splits:
        return rows

    question_blocks: list[tuple[str, int, int, int]] = []  # (text, q_num, start, end)
    for idx, m in enumerate(q_splits):
        q_num = int(m.group(1))
        block_start = m.start()
        block_end = q_splits[idx + 1].start() if idx + 1 < len(q_splits) else len(full_text)
        question_blocks.append((full_text[block_start:block_end], q_num, block_start, block_end))

    # ── Assign question_group: counts how many times each question_num has
    #    appeared so far in document order. The first Q1 = group 1, the second
    #    Q1 (a fresh "Question 1" header encountered later in the same PDF
    #    range, e.g. a second specimen paper) = group 2.
    # ── question_block_id: globally-incrementing counter across ALL question
    #    headers in document order, regardless of question_num.  Unique per
    #    "Question N" opener — used as the stable key in Stage 3 mark_point_id.
    question_group_counter: dict[int, int] = {}
    block_id_counter = 0

    # ── Process each question ────────────────────────────────────────────────
    for q_text, q_num, q_start_char, _ in question_blocks:
        # Track occurrence count for this question_num (1-based)
        question_group_counter[q_num] = question_group_counter.get(q_num, 0) + 1
        q_group = question_group_counter[q_num]

        block_id_counter += 1
        q_block_id = block_id_counter

        # Extract S.O. codes
        so_match = _RE_SO.search(q_text)
        so_codes_str = ""
        so_codes: list[str] = []
        if so_match:
            so_codes = parse_so_codes(so_match.group(1))
            so_codes_str = ",".join(so_codes)

        # Detect profile column header (KC/IA/APP) — best-effort
        profile = _detect_profile(q_text)

        # Split into parts via (a), (b), (c) etc.
        part_splits = list(_RE_PART.finditer(q_text))

        # Also capture everything before first labelled part as part "(intro)"
        intro_end = part_splits[0].start() if part_splits else len(q_text)
        intro_text = q_text[:intro_end].strip()

        # If intro text has bullets and mark allocations, treat as unlabelled part
        segments: list[tuple[str, str, int]] = []  # (part_label, text, char_offset_in_q)
        if part_splits:
            for idx, pm in enumerate(part_splits):
                letter = pm.group(1)
                roman  = pm.group(2) or ""
                label  = f"({letter})" if not roman else f"({letter})({roman.lower()})"
                seg_start = pm.start()
                seg_end   = part_splits[idx + 1].start() if idx + 1 < len(part_splits) else len(q_text)
                segments.append((label, q_text[seg_start:seg_end], q_start_char + seg_start))
        else:
            segments.append(("(a)", intro_text, q_start_char))

        # part_occurrence: 1-based counter per (question_block_id, question_part).
        # Increments each time the same part label is seen again within this block.
        # Distinguishes Section B essay alternatives that share one "Question 6"
        # header but repeat (a)/(b)/(c) for each alternative.
        part_occurrence_counter: dict[str, int] = {}

        # ── Process each part ────────────────────────────────────────────────
        for part_label, seg_text, seg_char_abs in segments:
            part_occurrence_counter[part_label] = part_occurrence_counter.get(part_label, 0) + 1
            part_occ = part_occurrence_counter[part_label]

            source_page = _char_to_page(seg_char_abs)
            raw_excerpt = seg_text[:200].replace("\n", " ").strip()

            alloc = _extract_mark_alloc(seg_text)
            bullets = _extract_bullets(seg_text)

            extracted_marks = sum(1 for _ in bullets)  # each bullet = 1 mark unless we know better

            point_order = 1

            def _row(**extra) -> dict:
                parser_artifact, excluded_reason = classify_artifact_and_exclusion(
                    extra.get("point_text", ""), raw_excerpt
                )
                return {
                    "question_num":        str(q_num),
                    "question_group":      q_group,
                    "question_block_id":   q_block_id,
                    "question_part":       part_label,
                    "part_occurrence":     part_occ,
                    "so_codes":            so_codes_str,
                    "profile":             profile or "",
                    "source_page":         source_page,
                    "raw_excerpt":         raw_excerpt,
                    "mapped_objective_id": "",
                    "verified":            0,
                    "parser_artifact":     parser_artifact,
                    "excluded_reason":     excluded_reason,
                    "needs_manual_entry":  "0",
                    **extra,
                }

            if bullets:
                for pt in bullets:
                    rows.append(_row(point_text=pt, marks_value=1, point_order=point_order))
                    point_order += 1
            elif alloc > 0:
                # Structural parse found a mark allocation but no extractable bullets.
                # Progress line always printed so long-running real runs look alive.
                print(f"  Processing Q{q_num}g{q_group}{part_label} ({alloc} marks)...")

                if dry_run:
                    print(f"  [dry-run: skipping LLM fallback for Q{q_num}g{q_group}{part_label}]")
                    rows.append(_row(
                        point_text=f"[REVIEW NEEDED: parsed only 0 of {alloc} marks]",
                        marks_value=alloc, point_order=1,
                    ))
                    continue

                # Live run: one attempt + max 1 retry on malformed JSON, then REVIEW NEEDED.
                llm_points: list[dict] = []
                for _attempt in range(2):  # attempt 0 = first try, attempt 1 = one retry
                    try:
                        llm_points = llm_extract_mark_points(seg_text, alloc)
                        break  # succeeded — stop retrying
                    except Exception as exc:
                        if _attempt == 0:
                            print(f"  [LLM fallback attempt 1 failed for Q{q_num}g{q_group}{part_label}: {exc}; retrying once]")
                        else:
                            print(f"  [LLM fallback attempt 2 failed for Q{q_num}g{q_group}{part_label}: {exc}; falling back to REVIEW NEEDED]")

                if llm_points:
                    for lp in llm_points:
                        rows.append(_row(
                            point_text=lp["point_text"],
                            marks_value=lp.get("marks_value", 1),
                            point_order=point_order,
                        ))
                        point_order += 1
                    extracted_marks = sum(lp.get("marks_value", 1) for lp in llm_points)
                else:
                    # Neither structural nor LLM succeeded — flag for review.
                    rows.append(_row(
                        point_text=f"[REVIEW NEEDED: parsed only 0 of {alloc} marks]",
                        marks_value=alloc, point_order=1,
                    ))
                    continue

            # If bullets were found but don't sum to the stated allocation, flag the gap.
            # No retry loop here — structural parse is deterministic; one pass is enough.
            if alloc > 0 and extracted_marks < alloc:
                rows.append(_row(
                    point_text=f"[REVIEW NEEDED: parsed only {extracted_marks} of {alloc} marks]",
                    marks_value=alloc - extracted_marks,
                    point_order=point_order,
                ))

    return rows


# ── Resolve objective_ids ────────────────────────────────────────────────────
def fill_objective_ids(rows: list[dict], db: sqlite3.Connection, subject_id: str) -> None:
    """Mutate rows in-place to populate mapped_objective_id."""
    cache: dict[str, str] = {}
    for row in rows:
        codes_str = row.get("so_codes", "")
        if not codes_str:
            continue
        if codes_str in cache:
            row["mapped_objective_id"] = cache[codes_str]
            continue
        codes = [c.strip() for c in codes_str.split(",") if c.strip()]
        mapped = map_so_to_objective(db, subject_id, codes)
        cache[codes_str] = mapped
        row["mapped_objective_id"] = mapped


# ── CSV writer ───────────────────────────────────────────────────────────────
def write_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


# ── Page-gap report ──────────────────────────────────────────────────────────
def _print_page_gap_report(rows: list[dict]) -> None:
    """
    Identify (question_num, question_part) groups whose rows span non-contiguous
    pages (gap > 1).  Non-contiguity almost always means the same label is reused
    for genuinely different questions (e.g. two specimen papers embedded back-to-back,
    or multiple Section B alternatives all numbered Q6).

    Also checks whether non-contiguous groups create collisions in the Stage 3
    mark_point_id formula: {prefix}-{objective_num}-q{num}{part}-mp{order}.
    The new question_group field resolves this — this report confirms it does so.
    """
    from collections import defaultdict, Counter

    # Group by (question_num, question_part) — ignoring question_group intentionally
    # (we want to see what the old formula would have collided on)
    raw_groups: dict = defaultdict(list)
    for r in rows:
        raw_groups[(r["question_num"], r["question_part"])].append(r["source_page"])

    non_contig = {}
    for key, pg_list in sorted(raw_groups.items()):
        sp = sorted(set(pg_list))
        gaps = [(sp[i], sp[i + 1]) for i in range(len(sp) - 1) if sp[i + 1] - sp[i] > 1]
        if gaps:
            non_contig[key] = (sp, gaps)

    print("\n" + "=" * 60)
    print("Page-gap analysis (non-contiguous (question_num, question_part) groups)")
    print("Each entry below is a candidate for two distinct questions sharing the")
    print("same label — confirmed by gap size relative to 1-page content chunks.")
    print("=" * 60)
    if non_contig:
        for (qn, qp), (sp, gaps) in sorted(non_contig.items()):
            print(f"  Q{qn}{qp}: pages {sp}")
            for a, b in gaps:
                print(f"    gap {a}->{b}  ({b - a} pages apart)")
    else:
        print("  No non-contiguous groups detected.")

    print(f"\n  {len(non_contig)} of {len(raw_groups)} groups are non-contiguous.")

    # Collision check: original formula (no disambiguation columns)
    orig_key: Counter = Counter()
    for r in rows:
        first_so = r["so_codes"].split(",")[0].strip() if r["so_codes"] else ""
        orig_key[(r["question_num"], r["question_part"], first_so, r["point_order"])] += 1
    orig_collisions = {k: v for k, v in orig_key.items() if v > 1}

    # Collision check: full new formula
    #   key = (question_block_id, question_part, part_occurrence, first_so_code, point_order)
    #   -> mark_point_id = {prefix}-{obj}-qb{block_id}{part}v{part_occ}-mp{order}
    new_key: Counter = Counter()
    for r in rows:
        first_so = r["so_codes"].split(",")[0].strip() if r["so_codes"] else ""
        new_key[(
            r["question_block_id"],
            r["question_part"],
            r["part_occurrence"],
            first_so,
            r["point_order"],
        )] += 1
    new_collisions = {k: v for k, v in new_key.items() if v > 1}

    print("\n" + "=" * 60)
    print("Stage 3 mark_point_id collision analysis")
    print("Formula: {prefix}-{obj}-qb{block_id}{part}v{part_occ}-mp{order}")
    print("=" * 60)
    print(f"  Original formula (no disambiguation) : {len(orig_collisions)} collision key(s)")
    if orig_collisions:
        for (qn, qp, so, po), cnt in sorted(orig_collisions.items())[:6]:
            so_str = so or "(empty SO)"
            print(f"    {so_str}-q{qn}{qp}-mp{po}  x{cnt}")
        if len(orig_collisions) > 6:
            print(f"    ... and {len(orig_collisions) - 6} more")
    print(f"  New formula (block_id + part_occ)    : {len(new_collisions)} collision key(s)")
    if new_collisions:
        for k, cnt in sorted(new_collisions.items()):
            print(f"    {k}  x{cnt}")
    else:
        print("    -> Zero collisions. All mark_point_ids are unique.")

    # Flag known part-label artifact candidates for Stage 2 manual review
    artifact_candidates = []
    seen_artifacts: set = set()
    for r in rows:
        qn, qp = r["question_num"], r["question_part"]
        blk = r["question_block_id"]
        # Bare "(b)" on Q2 and Q8: suspect missing roman-numeral suffix (i)/(ii)
        if qp == "(b)" and qn in ("2", "8") and (qn, blk) not in seen_artifacts:
            seen_artifacts.add((qn, blk))
            artifact_candidates.append((qn, r["question_group"], blk, r["source_page"]))
    if artifact_candidates:
        print()
        print("  Part-label artifact candidates (Stage 2 manual check):")
        print("  These bare '(b)' labels may be missing a roman-numeral suffix --")
        print("  verify against the source PDF page.")
        for qn, g, blk, pg in sorted(artifact_candidates):
            print(f"    Q{qn} group {g} block_id {blk}  first seen page {pg}")


# ── Summary printer ──────────────────────────────────────────────────────────
def print_summary(rows: list[dict], *, page_gap_report: bool = False) -> None:
    questions     = sorted({r["question_num"] for r in rows})
    review_needed = [r for r in rows if r["point_text"].startswith("[REVIEW NEEDED:")]
    unmapped      = [r for r in rows if not r.get("mapped_objective_id")]
    print("\n" + "=" * 60)
    print("Mark scheme extraction summary")
    print("=" * 60)
    print(f"  Questions parsed      : {len(questions)}  ({', '.join(questions)})")
    print(f"  Total rows produced   : {len(rows)}")
    print(f"  Points for review     : {len(review_needed)}")
    print(f"  Unmapped objective_id : {len(unmapped)}")
    if page_gap_report:
        _print_page_gap_report(rows)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Parse CXC syllabus-embedded mark scheme → reviewable CSV."
    )
    ap.add_argument("--subject", required=True, help="Subject ID (e.g. Economics)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and print summary but do not write CSV")
    ap.add_argument("--remap-only", action="store_true",
                    help="Re-run S.O.-to-objective mapping on the existing CSV "
                         "(no PDF parsing, no LLM calls). Overwrites "
                         "mapped_objective_id in place; all other columns unchanged.")
    args = ap.parse_args()

    # Load page range config
    if not PAGE_RANGES_FILE.exists():
        sys.exit(f"ERROR: {PAGE_RANGES_FILE} not found. Run locate_mark_scheme_pages.py first.")

    with open(PAGE_RANGES_FILE, encoding="utf-8") as f:
        config = json.load(f)

    if args.subject not in config:
        sys.exit(f"ERROR: subject '{args.subject}' not in {PAGE_RANGES_FILE}. "
                 f"Available: {list(config)}")

    entry = config[args.subject]
    pages = entry.get("pages", [None, None])
    pdf   = entry.get("pdf")

    if pages[0] is None or pages[1] is None:
        sys.exit(
            f"ERROR: page range for '{args.subject}' is [null, null]. "
            f"Run tools/locate_mark_scheme_pages.py --pdf <path> to find the range, "
            f"then update tools/mark_scheme_page_ranges.json."
        )
    if not pdf or not Path(pdf).exists():
        sys.exit(f"ERROR: PDF not found: {pdf!r}")

    # ── --remap-only: reload CSV, fix mapping, overwrite in place ────────────
    if args.remap_only:
        reports_root = os.getenv("REPORTS_ROOT")
        if not reports_root:
            sys.exit("ERROR: REPORTS_ROOT not set in .env")
        csv_path = Path(reports_root) / f"{args.subject}_mark_scheme_review.csv"
        if not csv_path.exists():
            sys.exit(f"ERROR: CSV not found: {csv_path}\nRun without --remap-only first.")

        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames or []

        before = sum(1 for r in rows if r.get("mapped_objective_id", "").strip())
        print(f"Remapping: {args.subject}")
        print(f"CSV      : {csv_path}")
        print(f"Rows     : {len(rows)}")
        print(f"Mapped before fix: {before} / {len(rows)}")

        db = open_db()
        # Clear old (wrong) mappings so fill_objective_ids re-evaluates all rows
        for r in rows:
            r["mapped_objective_id"] = ""
        fill_objective_ids(rows, db, args.subject)
        db.close()

        after = sum(1 for r in rows if r.get("mapped_objective_id", "").strip())
        print(f"Mapped after fix : {after} / {len(rows)}")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"CSV updated: {csv_path}")
        return

    start_page, end_page = int(pages[0]), int(pages[1])
    print(f"Parsing: {args.subject}")
    print(f"PDF    : {pdf}")
    print(f"Pages  : {start_page}–{end_page}")

    rows = parse_mark_scheme(pdf, start_page, end_page, dry_run=args.dry_run)

    # Objective-id mapping
    try:
        db = open_db()
        fill_objective_ids(rows, db, args.subject)
        db.close()
    except SystemExit:
        print("WARNING: could not open DB — mapped_objective_id will be empty.")

    print_summary(rows, page_gap_report=args.dry_run)

    if args.dry_run:
        print("\n[dry-run] CSV not written.")
        return

    reports_root = os.getenv("REPORTS_ROOT")
    if not reports_root:
        sys.exit("ERROR: REPORTS_ROOT not set in .env")

    output_path = Path(reports_root) / f"{args.subject}_mark_scheme_review.csv"
    write_csv(rows, output_path)
    print(f"\nCSV written: {output_path}")


if __name__ == "__main__":
    main()
