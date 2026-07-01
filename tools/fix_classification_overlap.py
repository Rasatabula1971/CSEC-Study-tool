"""
tools/fix_classification_overlap.py
=====================================
PHASE: build (one-off)

Finds rows in a mark-scheme review CSV that have BOTH parser_artifact=1 AND a
non-empty excluded_reason set (mutually exclusive states per the row
classification scheme in MARK_SCHEME_BUILD_PLAN.md) and clears parser_artifact
on them, leaving excluded_reason as the authoritative classification.

This is a one-off cleanup script for the existing Economics CSV (2 rows found
with both flags set, root-caused to a "Total" summation line absorbed by a
contaminated block). It is NOT a permanent pipeline component — going forward,
tools/extract_mark_scheme.py enforces this precedence at extraction time, and
tools/lock_mark_scheme.py's partition_rows() refuses to lock a CSV that still
has the overlap.

Usage:
    python tools/fix_classification_overlap.py --csv-file <path>
    python tools/fix_classification_overlap.py --csv-file <path> --auto-prefer-excluded
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path


def _fld(r: dict, k: str) -> str:
    return r.get(k, "").strip()


def find_overlapping_rows(rows: list) -> list:
    """Return rows with both parser_artifact=1 and a non-empty excluded_reason."""
    return [
        r for r in rows
        if _fld(r, "parser_artifact") == "1" and _fld(r, "excluded_reason")
    ]


def _describe(r: dict) -> str:
    block = _fld(r, "question_block_id")
    part  = _fld(r, "question_part")
    occ   = _fld(r, "part_occurrence")
    order = _fld(r, "point_order")
    return (
        f"qb{block}{part}v{occ}-mp{order}  "
        f"excluded_reason={_fld(r, 'excluded_reason')!r}  "
        f"| {_fld(r, 'point_text')[:60]}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="One-off fix: clear parser_artifact on rows that also have "
                     "excluded_reason set (mutually exclusive states)."
    )
    ap.add_argument("--csv-file", required=True, help="Path to the review CSV to fix")
    ap.add_argument("--auto-prefer-excluded", action="store_true",
                     help="Clear parser_artifact on every offending row without "
                          "per-row confirmation (batch mode).")
    args = ap.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    offenders = find_overlapping_rows(rows)
    if not offenders:
        print(f"No rows with both parser_artifact=1 and excluded_reason set in {csv_path.name}. Nothing to do.")
        return

    print(f"Found {len(offenders)} row(s) with both flags set in {csv_path.name}:")
    for r in offenders:
        print(f"  {_describe(r)}")

    if args.auto_prefer_excluded:
        fixed = offenders
    else:
        print(
            "\nFor each row above, excluded_reason will be kept and "
            "parser_artifact will be cleared to 0."
        )
        answer = input(f"Apply this fix to all {len(offenders)} row(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted. No changes written.")
            return
        fixed = offenders

    for r in fixed:
        r["parser_artifact"] = "0"

    backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
    shutil.copy2(csv_path, backup_path)
    print(f"Backup written: {backup_path}")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Cleared parser_artifact on {len(fixed)} row(s). CSV updated: {csv_path}")


if __name__ == "__main__":
    main()
