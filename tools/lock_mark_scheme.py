"""
tools/lock_mark_scheme.py
=========================
PHASE: build

Stage 3 of the mark scheme extraction pipeline.

Reads {REPORTS_ROOT}/{subject}_mark_scheme_review.csv and promotes
verified mark-scheme rows into the mark_points table.

Usage:
    python tools/lock_mark_scheme.py --subject Economics
    python tools/lock_mark_scheme.py --subject Economics --dry-run
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from backend.ingest_v2.subject_prefix import prefix_for
from backend.db.backup import backup_first
from backend.app import open_db, apply_runtime_migrations
from extract_mark_scheme import resolve_paper_pages

load_dotenv(_REPO_ROOT / ".env")

_PAGE_RANGES_JSON = _REPO_ROOT / "tools" / "mark_scheme_page_ranges.json"


# ── helpers ────────────────────────────────────────────────────────────────────

def _fld(r: dict, k: str) -> str:
    return r.get(k, "").strip()


def build_mark_point_id(prefix: str, obj_num: str, block_id: str,
                        part: str, occ: str, order: str) -> str:
    """Stable, collision-free mark_point_id per the Stage 3 formula."""
    return f"{prefix}-{obj_num}-qb{block_id}{part}v{occ}-mp{order}"


def build_point_group_id(prefix: str, block_id: str,
                         part: str, occ: str, order: str) -> str:
    """Positional key shared by all fanned-out rows from the same source row.

    Identical to build_mark_point_id but WITHOUT the per-objective prefix/num —
    so it is stable regardless of objective list order or count, and survives
    re-locks. grade.py uses this to deduplicate back to one gradeable point.
    """
    return f"{prefix}-qb{block_id}{part}v{occ}-mp{order}"


def build_question_id(prefix: str, block_id: str, part: str, occ: str) -> str:
    return f"{prefix}-qb{block_id}{part}v{occ}"


def build_mark_group_id(prefix: str, raw_mark_group_id: str) -> str | None:
    """Prefix the CSV's raw mark_group_id (e.g. '4(c)(i)-g1') for DB storage.

    extract_mark_scheme.py builds mark_group_id as
    f"{q_block_id}{part_label}-g{group_index}" -- q_block_id is the same
    globally-incrementing, document-order counter that build_mark_point_id /
    build_point_group_id / build_question_id already rely on for their own
    run-to-run stability, and group_index is a deterministic within-part
    position. So the raw value is already stable/reproducible across
    re-extraction runs of an unchanged source PDF -- no separate
    build-from-components formula is needed here, only the same subject-prefix
    convention every other CSV-derived id already gets. Returns None (NULL)
    when the CSV has no value, e.g. rows from before the m021 grouping work --
    Economics' 552 already-locked rows are exactly this case.
    """
    raw = raw_mark_group_id.strip()
    return f"{prefix}-{raw}" if raw else None


def _first_obj(mapped: str) -> str:
    """Return the first objective_id from a comma-separated list."""
    return mapped.split(",")[0].strip()


def _all_objs(mapped: str) -> list[str]:
    """Return deduplicated objective_ids from a comma-separated list, order preserved.

    Some CSV rows carry duplicates (e.g. 'ECON-4.4,ECON-4.6,ECON-4.6') due to
    extractor overlap — deduplication here prevents fanout from inserting multiple
    mark_points rows for the same objective and logging weakness twice per point.
    """
    seen: set[str] = set()
    result: list[str] = []
    for o in mapped.split(","):
        oid = o.strip()
        if oid and oid not in seen:
            seen.add(oid)
            result.append(oid)
    return result


def _obj_num_from_id(obj_id: str, prefix: str) -> str:
    """Strip the subject prefix to get the numeric portion: 'ECON-1.6' → '1.6'."""
    leader = f"{prefix}-"
    if obj_id.startswith(leader):
        return obj_id[len(leader):]
    raise ValueError(
        f"objective_id {obj_id!r} does not start with expected prefix {leader!r}"
    )


# ── core logic (importable for tests) ─────────────────────────────────────────

def check_blocking_rows(rows: list) -> list:
    """Return rows that must block locking (genuinely unreviewed content)."""
    return [
        r for r in rows
        if (_fld(r, "verified") == "0"
            and _fld(r, "parser_artifact") != "1"
            and _fld(r, "excluded_reason") == ""
            and _fld(r, "needs_manual_entry") != "1")
    ]


def check_null_source_pages(rows: list, prefix: str) -> list:
    """Return rows destined for mark_points that have no source_page.

    Only checks rows that would survive into the eligible set (not artifacts,
    not excluded, not manual-entry flagged).  Rows with an empty source_page
    violate Rule 2 of the build plan — every row must cite the page it came
    from — and must be refused before any write.
    """
    bad = []
    for r in rows:
        if (_fld(r, "parser_artifact") == "1"
                or _fld(r, "excluded_reason")
                or _fld(r, "needs_manual_entry") == "1"):
            continue
        if not _fld(r, "source_page"):
            bad.append(r)
    return bad


def check_unmapped_eligible_rows(rows: list) -> list:
    """Return rows destined for mark_points that have no mapped_objective_id.

    Only checks rows that would survive into the eligible set (not artifacts,
    not excluded, not manual-entry flagged) -- mirrors check_null_source_pages().
    Rule 1 requires every mark point to resolve to a real objective_id; a row
    with an empty mapped_objective_id and none of the three skip flags would
    otherwise be silently counted "eligible" by partition_rows() while
    _all_objs("") returns [] -- so lock_subject()'s per-objective insert loop
    for that row runs zero times, with no error, no warning, and no adjustment
    to the reported row count. Must be refused before any write.
    """
    bad = []
    for r in rows:
        if (_fld(r, "parser_artifact") == "1"
                or _fld(r, "excluded_reason")
                or _fld(r, "needs_manual_entry") == "1"):
            continue
        if not _fld(r, "mapped_objective_id"):
            bad.append(r)
    return bad


def check_overlapping_classification(rows: list) -> list:
    """Return rows that have BOTH parser_artifact=1 AND a non-empty excluded_reason.

    These two states are mutually exclusive per the row classification scheme
    (MARK_SCHEME_BUILD_PLAN.md): parser_artifact=1 means "structural rubric
    noise, by design"; excluded_reason means "out of scope for a different,
    specific reason" (contamination, duplicate document, etc). A row claiming
    both is a data error in the review CSV, not something this script should
    silently resolve.
    """
    return [
        r for r in rows
        if _fld(r, "parser_artifact") == "1" and _fld(r, "excluded_reason")
    ]


def partition_rows(rows: list) -> tuple:
    """Split rows into eligible and skipped categories.

    Returns (eligible_rows, skip_counts) where skip_counts has keys
    'artifact', 'excluded', 'manual'.

    Raises ValueError if any row has both parser_artifact=1 and a non-empty
    excluded_reason (mutually exclusive states — see
    check_overlapping_classification). This is refused outright rather than
    auto-corrected so the underlying review CSV gets fixed at its source.
    """
    overlapping = check_overlapping_classification(rows)
    if overlapping:
        lines = []
        for r in overlapping:
            block = _fld(r, "question_block_id")
            part  = _fld(r, "question_part")
            occ   = _fld(r, "part_occurrence")
            order = _fld(r, "point_order")
            pseudo_mpid = f"qb{block}{part}v{occ}-mp{order}"
            lines.append(
                f"  {pseudo_mpid}  excluded_reason={_fld(r,'excluded_reason')!r}  "
                f"| {_fld(r,'point_text')[:60]}"
            )
        raise ValueError(
            f"{len(overlapping)} row(s) have BOTH parser_artifact=1 AND excluded_reason "
            f"set (mutually exclusive states):\n" + "\n".join(lines)
        )

    eligible = []
    skip_counts = {"artifact": 0, "excluded": 0, "manual": 0}
    for r in rows:
        if _fld(r, "parser_artifact") == "1":
            skip_counts["artifact"] += 1
        elif _fld(r, "excluded_reason"):
            skip_counts["excluded"] += 1
        elif _fld(r, "needs_manual_entry") == "1":
            skip_counts["manual"] += 1
        else:
            eligible.append(r)
    return eligible, skip_counts


def check_collisions(eligible: list, prefix: str) -> dict:
    """Return a dict mapping duplicate point_group_id → list of eligible-list indices.

    Collisions are detected at the GROUP level (same positional key from two
    different source rows), not per fanned-out mark_point_id. A single source row
    fanning out to N objectives is expected and correct — those N rows all share
    one point_group_id intentionally. A collision is when two *distinct* source rows
    produce the same positional key, which would result in overlapping fanned sets.
    """
    seen: dict = {}
    dupes: dict = {}
    for i, r in enumerate(eligible):
        pgid = build_point_group_id(
            prefix,
            _fld(r, "question_block_id"),
            _fld(r, "question_part"),
            _fld(r, "part_occurrence"),
            _fld(r, "point_order"),
        )
        if pgid in seen:
            dupes.setdefault(pgid, [seen[pgid]]).append(i)
        else:
            seen[pgid] = i
    return dupes


def lock_subject(db: sqlite3.Connection, eligible: list,
                 subject: str, source_pdf: str, page_range: str) -> int:
    """Delete existing mark_points for the subject then re-insert from eligible rows.

    The delete and all inserts run inside a single transaction so a failed insert
    never leaves the subject's mark_points partially empty.  If anything raises,
    the whole operation rolls back.

    Returns the number of mark_points rows written.
    Raises ValueError on unknown objective_ids (FK safety before any write).
    """
    prefix = prefix_for(subject)

    # Validate all objective_ids exist before touching the DB
    missing = []
    for r in eligible:
        for obj_id in _all_objs(_fld(r, "mapped_objective_id")):
            exists = db.execute(
                "SELECT 1 FROM objectives WHERE objective_id = ?", (obj_id,)
            ).fetchone()
            if not exists:
                missing.append((obj_id, _fld(r, "question_block_id"), _fld(r, "question_part")))
    if missing:
        lines = "\n".join(f"  {o}  (block={b}, part={p})" for o, b, p in missing)
        raise ValueError(f"{len(missing)} unknown objective_id(s):\n{lines}")

    db.execute("""
        CREATE TABLE IF NOT EXISTS mark_scheme_locks (
            subject_id  TEXT PRIMARY KEY REFERENCES subjects(subject_id),
            source_pdf  TEXT NOT NULL,
            page_range  TEXT NOT NULL,
            locked_at   TEXT DEFAULT (datetime('now')),
            row_count   INTEGER NOT NULL
        )
    """)

    # Atomic delete-then-reinsert: wipe this subject's PREVIOUSLY LOCKED rows
    # before writing the new batch.  A formula change between lock runs would
    # otherwise leave stale rows with the old mark_point_id format alongside
    # the new ones (INSERT OR REPLACE only replaces on exact PK match).
    #
    # Scoped to doc_id IS NULL: every row this function inserts is written
    # with doc_id=NULL explicitly (see the INSERT below), which is a reliable,
    # code-guaranteed signal for "this row came from the mark-scheme-lock
    # pipeline." Some subjects (confirmed: Principles_of_Business, 2447 rows)
    # also carry a SEPARATE, unrelated mark_points population from
    # ingest_solutions.py's worked-solutions answer bank -- those rows always
    # carry a real doc_id (e.g. 'sol-fa67a890f043') tying them to an ingested
    # PDF. The original unscoped DELETE matched on objective ownership alone,
    # which does not distinguish the two pipelines: a lock run against a
    # subject with both populations silently deleted the entire worked-
    # solutions answer bank on its very first run. The doc_id IS NULL guard
    # makes this DELETE reach only rows this function itself could have
    # written, never another pipeline's data, regardless of which subject.
    db.execute("""
        DELETE FROM mark_points
        WHERE doc_id IS NULL
          AND objective_id IN (
            SELECT objective_id FROM objectives WHERE subject_id = ?
        )
    """, (subject,))

    written = 0
    for r in eligible:
        obj_ids = _all_objs(_fld(r, "mapped_objective_id"))
        block   = _fld(r, "question_block_id")
        part    = _fld(r, "question_part")
        occ     = _fld(r, "part_occurrence")
        order_s = _fld(r, "point_order")

        pgid = build_point_group_id(prefix, block, part, occ, order_s)
        qid  = build_question_id(prefix, block, part, occ)
        mv   = int(mv_s) if (mv_s := _fld(r, "marks_value")).isdigit() else 1
        po   = int(order_s) if order_s.isdigit() else 0
        text = _fld(r, "point_text")
        mgid = build_mark_group_id(prefix, _fld(r, "mark_group_id"))
        gmm_s = _fld(r, "group_max_marks")
        gmm  = int(gmm_s) if gmm_s.isdigit() else None

        for obj_id in obj_ids:
            obj_num = _obj_num_from_id(obj_id, prefix)
            if len(obj_ids) == 1:
                # Single-objective row: mark_point_id follows the original formula
                mpid = build_mark_point_id(prefix, obj_num, block, part, occ, order_s)
            else:
                # Multi-objective fanout: append the objective_num to keep PK unique
                mpid = build_mark_point_id(prefix, obj_num, block, part, occ, order_s) + f"-{obj_num}"

            db.execute("""
                INSERT OR REPLACE INTO mark_points
                    (mark_point_id, objective_id, question_id, doc_id,
                     point_text, marks_value, point_order, point_group_id,
                     mark_group_id, group_max_marks)
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """, (mpid, obj_id, qid, text, mv, po, pgid, mgid, gmm))
            written += 1

    db.execute("""
        INSERT OR REPLACE INTO mark_scheme_locks
            (subject_id, source_pdf, page_range, locked_at, row_count)
        VALUES (?, ?, ?, datetime('now'), ?)
    """, (subject, source_pdf or "", page_range, written))

    db.commit()
    return written


# ── CLI ────────────────────────────────────────────────────────────────────────

@backup_first("pre_lock_mark_scheme")
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Lock verified mark-scheme rows into mark_points."
    )
    ap.add_argument("--subject", required=True, help="Subject ID, e.g. Economics")
    ap.add_argument("--paper", default=None,
                    help="Paper key (e.g. paper_02) — required only for subjects "
                         "with multiple mark-scheme papers configured in "
                         "mark_scheme_page_ranges.json; ignored for single-paper "
                         "subjects like Economics")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate only — do not write to the DB")
    args = ap.parse_args()

    subject = args.subject

    reports_root = os.getenv("REPORTS_ROOT")
    db_path      = os.getenv("DB_PATH")
    if not reports_root:
        sys.exit("ERROR: REPORTS_ROOT not set in .env")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")

    # Load page-range metadata up front — resolve_paper_pages() tells us both
    # the paper_key (needed to build the CSV filename below) and the pages
    # (needed later, before locking, for the mark_scheme_locks page_range).
    page_meta = {}
    if _PAGE_RANGES_JSON.exists():
        page_meta = json.loads(_PAGE_RANGES_JSON.read_text(encoding="utf-8")).get(subject, {})
    pages, paper_key = resolve_paper_pages(page_meta, subject, args.paper)

    csv_name = (f"{subject}_{paper_key}_mark_scheme_review.csv" if paper_key
                else f"{subject}_mark_scheme_review.csv")
    csv_path = Path(reports_root) / csv_name
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}\nRun extract_mark_scheme.py first.")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    print(f"Read {len(rows)} rows from {csv_path.name}")

    # Gate: genuinely unreviewed rows block locking
    blocking = check_blocking_rows(rows)
    if blocking:
        print(f"\nERROR: {len(blocking)} unreviewed row(s) block locking:")
        for r in blocking:
            print(f"  block={_fld(r,'question_block_id')} part={_fld(r,'question_part')} "
                  f"occ={_fld(r,'part_occurrence')} ord={_fld(r,'point_order')} "
                  f"obj={_fld(r,'mapped_objective_id')!r}  "
                  f"| {_fld(r,'point_text')[:60]}")
        sys.exit("Resolve all unreviewed rows (set verified=1 or flag appropriately) before locking.")

    # Gate: Rule 2 — every eligible row must cite its source page
    prefix = prefix_for(subject)
    null_page_rows = check_null_source_pages(rows, prefix)
    if null_page_rows:
        print(f"\nERROR: {len(null_page_rows)} row(s) with empty source_page violate Rule 2 "
              f"(every mark point must cite its source page):")
        for r in null_page_rows:
            block = _fld(r, "question_block_id")
            part  = _fld(r, "question_part")
            occ   = _fld(r, "part_occurrence")
            order = _fld(r, "point_order")
            obj   = _fld(r, "mapped_objective_id")
            mpid  = f"{prefix}-{obj.split(',')[0].strip()}-qb{block}{part}v{occ}-mp{order}" if obj else f"qb{block}{part}v{occ}-mp{order}"
            print(f"  {mpid}  block={block} part={part} occ={occ} ord={order} "
                  f"obj={obj!r}  | {_fld(r,'point_text')[:60]}")
        sys.exit("Populate source_page for all affected rows before locking.")

    # Gate: Rule 1 — every eligible row must resolve to a real objective_id
    unmapped_rows = check_unmapped_eligible_rows(rows)
    if unmapped_rows:
        print(f"\nERROR: {len(unmapped_rows)} row(s) with empty mapped_objective_id "
              f"violate Rule 1 (every mark point must resolve to a real objective_id):")
        for r in unmapped_rows:
            block = _fld(r, "question_block_id")
            part  = _fld(r, "question_part")
            occ   = _fld(r, "part_occurrence")
            order = _fld(r, "point_order")
            pseudo_mpid = f"qb{block}{part}v{occ}-mp{order}"
            print(f"  {pseudo_mpid}  block={block} part={part} occ={occ} ord={order} "
                  f"| {_fld(r,'point_text')[:60]}")
        sys.exit("Populate mapped_objective_id for all affected rows before locking.")

    try:
        eligible, skip_counts = partition_rows(rows)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    print(f"Skipped  — artifact: {skip_counts['artifact']}, "
          f"excluded: {skip_counts['excluded']}, "
          f"needs_manual_entry: {skip_counts['manual']}")
    print(f"Eligible to lock: {len(eligible)}")

    # Collision check — must pass before any DB write
    dupes = check_collisions(eligible, prefix)
    if dupes:
        print(f"\nERROR: {len(dupes)} mark_point_id collision(s) detected:")
        for mpid, indices in dupes.items():
            print(f"  {mpid!r}  at eligible-list indices {indices}")
        sys.exit("Fix collisions before locking.")

    if args.dry_run:
        print(f"\n[dry-run] Validation passed — would lock {len(eligible)} rows. No DB changes.")
        return

    # page_meta / pages were already resolved above (via resolve_paper_pages,
    # paper-aware) — reuse them rather than reloading the JSON a second time.
    source_pdf = page_meta.get("pdf") or ""
    page_range = f"{pages[0]}-{pages[1]}" if pages and pages[0] is not None else ""

    db = open_db(db_path)
    apply_runtime_migrations(db)

    try:
        written = lock_subject(db, eligible, subject, source_pdf, page_range)
        # lock_subject does a full DELETE+reinsert using build_question_id, which
        # never produces the -stem suffix.  Re-applying migrations here normalises
        # the freshly-inserted rows to the convention the quiz picker and grade
        # path depend on.  The pre-lock call above normalised any pre-existing rows
        # but cannot help rows that didn't exist yet.
        apply_runtime_migrations(db)
    finally:
        db.close()

    print(f"\nLocked {written} mark points.")
    print(f"Skipped {sum(skip_counts.values())} rows "
          f"({skip_counts['artifact']} artifact, "
          f"{skip_counts['excluded']} excluded, "
          f"{skip_counts['manual']} needs_manual_entry).")
    print(f"Subject '{subject}' mark scheme locked.")


if __name__ == "__main__":
    main()
