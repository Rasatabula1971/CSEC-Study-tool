"""
tools/reclassify_existing_csv.py
===================================
PHASE: build (one-off / repeatable)

Re-applies extract_mark_scheme.classify_artifact_and_exclusion() to an
ALREADY-EXTRACTED review CSV, using only the columns already on disk
(point_text, raw_excerpt) -- no PDF re-parse, no LLM call, no structural
re-extraction. This lets a classification-rule change (e.g. the fragmentation
patterns added for arithmetic echoes, empty parentheticals, and lowercase-led
fragments) be retroactively applied to a CSV that was extracted before the
rule existed, without re-running the whole Stage 1 pipeline.

Scope is deliberately narrow:
  - Only ever writes parser_artifact, and (per the state-exclusivity rule)
    verified when parser_artifact flips 0 -> 1.
  - Never writes excluded_reason. A row that already has excluded_reason set
    is skipped entirely -- excluded_reason wins precedence over any freshly
    computed artifact value, no exceptions (see MARK_SCHEME_BUILD_PLAN.md's
    row classification scheme). A row whose recomputed classification would
    newly *introduce* an excluded_reason is also skipped -- resolving that is
    fix_classification_overlap.py's job, not this script's.
  - Never touches point_text, raw_excerpt, mapped_objective_id, question_num,
    question_part, so_codes, marks_value, point_order, source_page, or
    needs_manual_entry.

Usage:
    python tools/reclassify_existing_csv.py --csv-file <path>
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_mark_scheme import classify_artifact_and_exclusion


def _fld(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def _describe(row: dict) -> str:
    block = _fld(row, "question_block_id")
    part  = _fld(row, "question_part")
    occ   = _fld(row, "part_occurrence")
    order = _fld(row, "point_order")
    return f"qb{block}{part}v{occ}-mp{order}"


def find_reclassifications(rows: list, classify_fn=classify_artifact_and_exclusion) -> list:
    """Return diff records for rows whose parser_artifact should change.

    Each record: {"row": <dict>, "old_artifact": "0", "new_artifact": "1"}.

    Additive only: a row is flagged ONLY when it flips 0 -> 1 (a newly-caught
    artifact). A row currently marked parser_artifact=1 is NEVER reconsidered
    for un-flagging, even if the automated function would compute "0" for it.
    This was confirmed necessary against the real, human-reviewed Economics
    CSV: several rows are correctly parser_artifact=1 from manual Stage 2
    review for patterns this function does not (and cannot fully) model --
    e.g. a bracket placeholder with an internal space ("( )", "[ ]"),
    "(any two, each)"-style marking annotations, and OCR/encoding replacement
    glyphs ("Determinant mentioned �"). Reclassifying those back to "0"
    would silently erase correct human judgement rather than catch a gap --
    exactly the failure mode the build plan's verification protocol warns
    against. This script's job is narrowly to retroactively apply newly-added
    *detection* rules, never to relitigate rows a human already resolved.

    A row is also skipped entirely (never included, never touched) when:
      - it already has a non-empty excluded_reason -- precedence: excluded
        wins, parser_artifact must never be set on such a row even if its
        text independently matches an artifact pattern, OR
      - the freshly computed classification would introduce a NEW
        excluded_reason for a row that doesn't currently have one -- out of
        scope for this script (see fix_classification_overlap.py), so it is
        left alone rather than partially updated.
    """
    diffs = []
    for row in rows:
        if _fld(row, "excluded_reason"):
            continue

        old_artifact = _fld(row, "parser_artifact") or "0"
        if old_artifact == "1":
            continue  # never reconsider an already-flagged row -- additive only

        new_artifact, new_excluded = classify_fn(
            row.get("point_text", ""), row.get("raw_excerpt", "")
        )
        if new_excluded:
            continue

        if new_artifact == "1":
            diffs.append({"row": row, "old_artifact": old_artifact, "new_artifact": new_artifact})
    return diffs


def apply_reclassifications(diffs: list) -> None:
    """Mutate each diff's row in place.

    Sets parser_artifact to the new value. When the flip is 0 -> 1, also
    forces verified to "0" (state-exclusivity rule: a row must never be
    simultaneously parser_artifact=1 and verified=1). No other column is
    touched.
    """
    for d in diffs:
        row = d["row"]
        row["parser_artifact"] = d["new_artifact"]
        if d["old_artifact"] == "0" and d["new_artifact"] == "1":
            row["verified"] = "0"


def tally(rows: list) -> dict:
    """Count rows by classification state (same precedence order as
    lock_mark_scheme.py's partition_rows: artifact > excluded > manual, else
    verified). Returns {"verified", "artifact", "excluded", "manual"} counts.

    Rows matching none of the four states (genuinely unreviewed) are not
    counted in any bucket -- the caller compares the sum against the total
    row count to surface any such orphans, per the build plan's pre-lock
    completeness check.
    """
    counts = {"verified": 0, "artifact": 0, "excluded": 0, "manual": 0}
    for row in rows:
        if _fld(row, "parser_artifact") == "1":
            counts["artifact"] += 1
        elif _fld(row, "excluded_reason"):
            counts["excluded"] += 1
        elif _fld(row, "needs_manual_entry") == "1":
            counts["manual"] += 1
        elif _fld(row, "verified") == "1":
            counts["verified"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Re-apply classify_artifact_and_exclusion() to an "
                     "already-extracted review CSV (no PDF re-parse, no LLM call)."
    )
    ap.add_argument("--csv-file", required=True, help="Path to the review CSV to reclassify")
    args = ap.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    diffs = find_reclassifications(rows)
    if not diffs:
        print(f"No reclassifications needed in {csv_path.name}. Nothing to do.")
        return

    print(f"Found {len(diffs)} row(s) whose parser_artifact classification has changed:")
    for d in diffs:
        row = d["row"]
        old_verified = _fld(row, "verified") or "0"
        new_verified = "0" if (d["old_artifact"] == "0" and d["new_artifact"] == "1") else old_verified
        print(
            f"  {_describe(row)}  "
            f"artifact: {d['old_artifact']} -> {d['new_artifact']}  "
            f"verified: {old_verified} -> {new_verified}  "
            f"| {_fld(row, 'point_text')[:60]!r}"
        )

    answer = input(f"\nApply these {len(diffs)} change(s)? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted. No changes written.")
        return

    apply_reclassifications(diffs)

    backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
    shutil.copy2(csv_path, backup_path)
    print(f"Backup written: {backup_path}")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Reclassified {len(diffs)} row(s). CSV updated: {csv_path}")

    counts = tally(rows)
    total  = len(rows)
    summed = sum(counts.values())
    print(f"\nTallies after reclassification:")
    print(f"  verified           : {counts['verified']}")
    print(f"  parser_artifact    : {counts['artifact']}")
    print(f"  excluded_reason    : {counts['excluded']}")
    print(f"  needs_manual_entry : {counts['manual']}")
    print(f"  ------------------------------")
    print(f"  sum                : {summed}")
    print(f"  total rows         : {total}")
    if summed == total:
        print("  OK -- tallies sum to total rows (0 orphans).")
    else:
        print(f"  WARNING: {total - summed} orphan row(s) do not match any of the "
              f"four classification states -- genuinely unreviewed, blocks Stage 3 locking.")


if __name__ == "__main__":
    main()
