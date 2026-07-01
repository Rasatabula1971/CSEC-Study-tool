"""
tools/fix_q4c_fragmentation.py
=================================
PHASE: build (one-off, literal correction)

Economics question_block_id=4, question_part="(c)", part_occurrence=1 was
extracted as 7 fragmented rows (mark_point_ids qb4(c)v1-mp1 .. qb4(c)v1-mp7)
instead of the 2 real mark points the source PDF page 94 actually contains.
Manual verification against the page 94 image confirmed the correct text and
mark allocation for both points -- this script performs exactly that one
literal correction: delete the 7 fragments, insert the 2 verified rows in
their place.

This is NOT a reusable pipeline component and does not call
classify_artifact_and_exclusion() or any other classification logic -- the
replacement rows' verified/parser_artifact/excluded_reason values are the
literal, manually-confirmed final state, not a computed guess.

Usage:
    python tools/fix_q4c_fragmentation.py --csv-file <path>
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

# ── Target selector for the fragmented group ─────────────────────────────────
TARGET_BLOCK_ID  = "4"
TARGET_PART      = "(c)"
TARGET_OCCURRENCE = "1"
EXPECTED_ROW_COUNT = 7
EXPECTED_POINT_ORDERS = {"1", "2", "3", "4", "5", "6", "7"}

# Columns that must be identical across all 7 fragments before we trust that
# they really are one fragmented group and not an accidental cross-question
# collision. (raw_excerpt is deliberately NOT checked here -- it is carried
# over from the first row for traceability, but minor differences in it
# wouldn't indicate a real grouping problem the way a differing so_codes or
# source_page would.)
CONSISTENCY_COLUMNS = [
    "question_num", "question_part", "so_codes", "source_page",
    "mapped_objective_id", "question_group", "question_block_id",
    "part_occurrence", "profile",
]

# ── The two verified replacement rows ────────────────────────────────────────
REPLACEMENT_POINTS = [
    {
        "point_text": (
            "It ignores income distribution. A country can have a high GDP "
            "but the distribution of this wealth could be very uneven with "
            "only a few rich people having a large percentage; majority may "
            "be poor."
        ),
        "marks_value": "3",
    },
    {
        "point_text": (
            "GDP is measured in numbers so it does not take account of the "
            "quality of life of the people. GDP can be high while the "
            "majority of the people may not have access to good sanitation, "
            "water, electricity and medical facilities."
        ),
        "marks_value": "3",
    },
]


def _fld(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def is_target_row(row: dict) -> bool:
    return (
        _fld(row, "question_block_id") == TARGET_BLOCK_ID
        and _fld(row, "question_part") == TARGET_PART
        and _fld(row, "part_occurrence") == TARGET_OCCURRENCE
    )


def find_target_rows(rows: list) -> list:
    """Return the (index, row) pairs matching the Q4(c) fragment group."""
    return [(i, r) for i, r in enumerate(rows) if is_target_row(r)]


def validate_target_group(matches: list) -> None:
    """Abort with a clear, specific error on any shape mismatch.

    Raises ValueError -- never silently proceeds with the wrong number of
    rows or a group whose rows don't actually share the fields that would
    prove they're one fragmented answer.
    """
    if len(matches) != EXPECTED_ROW_COUNT:
        lines = "\n".join(
            f"  point_order={_fld(r, 'point_order')}  | {_fld(r, 'point_text')[:60]!r}"
            for _, r in matches
        )
        raise ValueError(
            f"Expected exactly {EXPECTED_ROW_COUNT} rows for "
            f"question_block_id={TARGET_BLOCK_ID} question_part={TARGET_PART!r} "
            f"part_occurrence={TARGET_OCCURRENCE}, found {len(matches)}:\n"
            f"{lines if lines else '  (none)'}\n"
            f"Refusing to proceed -- the CSV shape has changed since this "
            f"script was written; do not run it against different data."
        )

    orders = {_fld(r, "point_order") for _, r in matches}
    if orders != EXPECTED_POINT_ORDERS:
        raise ValueError(
            f"Expected point_order values {sorted(EXPECTED_POINT_ORDERS)}, "
            f"found {sorted(orders)}. Refusing to proceed."
        )

    for col in CONSISTENCY_COLUMNS:
        values = {_fld(r, col) for _, r in matches}
        if len(values) > 1:
            raise ValueError(
                f"Column {col!r} differs across the matched rows: {sorted(values)}. "
                f"These rows are not one fragmented group -- refusing to proceed."
            )


def build_replacement_rows(matches: list, fieldnames: list) -> list:
    """Build the 2 replacement row dicts, copying all shared/unset columns
    from the first original row and overriding only the fields the manual
    correction actually changes."""
    template = matches[0][1]
    replacements = []
    for i, point in enumerate(REPLACEMENT_POINTS, start=1):
        row = {k: template.get(k, "") for k in fieldnames}
        row["point_text"]        = point["point_text"]
        row["marks_value"]       = point["marks_value"]
        row["point_order"]       = str(i)
        row["verified"]          = "1"
        row["parser_artifact"]   = "0"
        row["excluded_reason"]   = ""
        row["needs_manual_entry"] = "0"
        replacements.append(row)
    return replacements


def apply_fix(rows: list, matches: list, fieldnames: list) -> list:
    """Return a new row list with the matched group replaced by the 2
    verified rows, inserted at the position of the first matched row."""
    target_indices = {i for i, _ in matches}
    replacements = build_replacement_rows(matches, fieldnames)

    updated = []
    inserted = False
    for i, row in enumerate(rows):
        if i in target_indices:
            if not inserted:
                updated.extend(replacements)
                inserted = True
            continue
        updated.append(row)
    return updated


def tally(rows: list) -> dict:
    """Four-state classification tally (artifact > excluded > manual, else
    verified), matching lock_mark_scheme.py's partition_rows precedence."""
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


def _print_row_summary(label: str, row: dict) -> None:
    print(
        f"    order={_fld(row,'point_order')}  marks={_fld(row,'marks_value')}  "
        f"verified={_fld(row,'verified')}  artifact={_fld(row,'parser_artifact')}  "
        f"| {row.get('point_text','')[:70]!r}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="One-off fix: merge the 7 fragmented Q4(c) rows into the "
                     "2 verified mark points (Economics, source PDF page 94)."
    )
    ap.add_argument("--csv-file", required=True, help="Path to the review CSV to fix")
    args = ap.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    matches = find_target_rows(rows)
    try:
        validate_target_group(matches)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    replacements = build_replacement_rows(matches, fieldnames)

    print(f"Q4(c) fragmentation fix -- {csv_path.name}")
    print(f"Matched {len(matches)} original row(s):")
    for _, row in matches:
        _print_row_summary("orig", row)

    print(f"\nWill be replaced with {len(replacements)} row(s):")
    for row in replacements:
        _print_row_summary("new", row)

    orig_marks = sum(int(_fld(r, "marks_value") or "0") for _, r in matches)
    new_marks  = sum(int(_fld(r, "marks_value") or "0") for r in replacements)
    print(f"\nTotal marks_value: {orig_marks} (7 rows) -> {new_marks} (2 rows)")

    answer = input(f"\nApply this fix? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted. No changes written.")
        return

    updated_rows = apply_fix(rows, matches, fieldnames)

    backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
    shutil.copy2(csv_path, backup_path)
    print(f"Backup written: {backup_path}")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    print(f"CSV updated: {csv_path}")

    counts = tally(updated_rows)
    total  = len(updated_rows)
    summed = sum(counts.values())
    print(f"\nNew total row count: {total} (was {len(rows)})")
    print(f"Tallies:")
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
              f"four classification states.")


if __name__ == "__main__":
    main()
