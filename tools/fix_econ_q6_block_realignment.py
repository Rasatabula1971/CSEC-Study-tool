"""
tools/fix_econ_q6_block_realignment.py
=========================================
PHASE: build (one-off, literal correction)

Corrects a real question_block_id misassignment in the Economics mark-scheme
review CSV, confirmed by manual review of the source PDF (pages 95-98):

  - Page 95 ("Question 5") -> already correctly locked as question_block_id=5.
  - Page 96 ("Question 5 cont'd") -> the extractor's block-counter treated the
    repeated "Question 5" header as a NEW block, so this content ended up
    under question_block_id=6 with question_num still correctly '5'. It is
    genuine, distinct content (the negative side of (c), and all of (d)) --
    NOT a duplicate of page 95's content, and must not be deleted.
  - Page 97 ("Question 6", the REAL Question 6) -> consequently landed under
    question_block_id=7 instead of the vacant 6, because block_id 6 had
    already been consumed by the bogus "Question 5 cont'd" match.

This script:
  1. Re-homes the 4 question_block_id=6 rows to question_block_id=5 (they are
     real Question 5 content, not duplicates), appending to block 5's
     existing (c) sequence and adding a new (d) group, with point_order
     values chosen not to collide with block 5's existing rows.
  2. Re-homes the question_block_id=7 / source_page=97 subset to the now-
     vacant question_block_id=6 (question_num is already correctly '6' --
     not touched), relabels the second balance-of-payments-disequilibrium
     "(b)(i)" row to "(b)(ii)", and deletes the 4 rows that are boilerplate
     misattached from page 98.
  3. Adds 2 new verified rows for Question 6 (a) and (c) with the real,
     manually-transcribed mark-scheme text. Question 6(d) is confirmed
     absent from the source and is never added.
  4. Verifies -- by reusing lock_mark_scheme.py's OWN partition_rows /
     check_collisions, the exact code path Stage 3 locking runs -- that no
     mark_point_id / point_group_id collision exists anywhere in the
     corrected CSV, not just in blocks 5/6.

Usage:
    python tools/fix_econ_q6_block_realignment.py --csv-file <path>
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TOOLS_DIR))

from lock_mark_scheme import (
    build_point_group_id,
    check_collisions,
    partition_rows,
)

PREFIX = "ECON"

# ── Expected shape of the two source groups (validated before any mutation) ──
EXPECTED_BLOCK6_SHAPE = {
    ("(c)", "1"), ("(c)", "2"), ("(d)", "1"), ("(d)", "2"),
}
BOILERPLATE_TEXTS = {
    "Answer ALL the questions.",
    "Silent electronic calculators may be used, but ALL necessary working should",
    "Answer the questions on the Answer Booklet provided and return it.",
    "Attach additional complete sheets (Ledger, Journal, Cash Book) to this",
}
BOP_DISEQUILIBRIUM_MARKER = "balance of payments disequilibrium"

NEW_ROW_A_TEXT = (
    "Identification of each example of a transfer from: Gifts from family, "
    "Grants, Overseas scholarships, Charity, Donations. (3x1, 1 mark each)"
)
NEW_ROW_C_TEXT = (
    "Export prices become more expensive which can cause a fall in quantity "
    "demanded for exports causing layoffs in the export industries and a "
    "loss in foreign exchange earnings. Import prices fall relatively which "
    "may cause an increase in quantity demanded of imported goods which can "
    "worsen the balance of trade. Terms of trade improves as the country can "
    "now buy more imports with what it receives for exports initially. "
    "(A listing of the effect = 1 mark each; a development of the effect = "
    "3 marks each; partial development = 1-2 marks each)"
)


def _fld(r: dict, k: str) -> str:
    return (r.get(k) or "").strip()


def find_block6_rows(rows: list) -> list:
    return [(i, r) for i, r in enumerate(rows) if _fld(r, "question_block_id") == "6"]


def find_block7_page97_rows(rows: list) -> list:
    return [
        (i, r) for i, r in enumerate(rows)
        if _fld(r, "question_block_id") == "7" and _fld(r, "source_page") == "97"
    ]


def validate_source_shape(block6: list, block7_page97: list) -> None:
    """Abort with a clear error if the CSV no longer matches the exact shape
    this one-off correction was written against."""
    if len(block6) != 4:
        raise ValueError(
            f"Expected exactly 4 question_block_id=6 rows, found {len(block6)}. "
            f"Refusing to proceed -- the CSV shape has changed."
        )
    shape = {(_fld(r, "question_part"), _fld(r, "point_order")) for _, r in block6}
    if shape != EXPECTED_BLOCK6_SHAPE:
        raise ValueError(
            f"question_block_id=6 rows don't match the expected (part, point_order) "
            f"shape.\n  expected: {sorted(EXPECTED_BLOCK6_SHAPE)}\n  found:    {sorted(shape)}"
        )

    if len(block7_page97) != 12:
        raise ValueError(
            f"Expected exactly 12 question_block_id=7 / source_page=97 rows, "
            f"found {len(block7_page97)}. Refusing to proceed."
        )
    boilerplate_found = [r for _, r in block7_page97 if _fld(r, "point_text") in BOILERPLATE_TEXTS]
    if len(boilerplate_found) != 4:
        raise ValueError(
            f"Expected exactly 4 boilerplate rows among the block-7/page-97 subset, "
            f"found {len(boilerplate_found)}: {[_fld(r,'point_text') for r in boilerplate_found]}"
        )
    bop_rows = [
        r for _, r in block7_page97
        if _fld(r, "question_part") == "(b)(i)"
        and BOP_DISEQUILIBRIUM_MARKER in _fld(r, "point_text").lower()
    ]
    if len(bop_rows) != 2:
        raise ValueError(
            f"Expected exactly 2 '(b)(i)' rows mentioning {BOP_DISEQUILIBRIUM_MARKER!r}, "
            f"found {len(bop_rows)}. Refusing to proceed."
        )


def build_rehomed_block5_rows(block6: list, block5_rows: list) -> list:
    """Return the 4 block-6 rows with question_block_id -> '5' and point_order
    reassigned to continue block 5's existing per-part sequences (never
    overwriting the positive-contribution (c) rows already there)."""
    max_order_by_part: dict = {}
    for r in block5_rows:
        part = _fld(r, "question_part")
        order = int(_fld(r, "point_order") or "0")
        max_order_by_part[part] = max(max_order_by_part.get(part, 0), order)

    # Process in a stable order so repeated point_order assignment is deterministic.
    ordered = sorted(block6, key=lambda ir: (_fld(ir[1], "question_part"), int(_fld(ir[1], "point_order"))))

    out = []
    for i, r in ordered:
        part = _fld(r, "question_part")
        max_order_by_part[part] = max_order_by_part.get(part, 0) + 1
        new_r = dict(r)
        new_r["question_block_id"] = "5"
        new_r["point_order"] = str(max_order_by_part[part])
        out.append((i, r, new_r))
    return out


def build_rehomed_block6_rows(block7_page97: list) -> tuple:
    """Return (rehomed, deleted) for the block-7/page-97 subset: block_id ->
    '6' for every surviving row, the second BOP-disequilibrium '(b)(i)' pair
    relabeled to '(b)(ii)', and the 4 page-98 boilerplate rows dropped."""
    rehomed = []
    deleted = []
    for i, r in block7_page97:
        if _fld(r, "point_text") in BOILERPLATE_TEXTS:
            deleted.append((i, r))
            continue
        new_r = dict(r)
        new_r["question_block_id"] = "6"
        if (_fld(r, "question_part") == "(b)(i)"
                and BOP_DISEQUILIBRIUM_MARKER in _fld(r, "point_text").lower()):
            new_r["question_part"] = "(b)(ii)"
        rehomed.append((i, r, new_r))
    return rehomed, deleted


def build_new_rows(rehomed_block6: list, fieldnames: list) -> list:
    """Build the 2 new verified Question 6 rows, using a sibling row in the
    same (post-rehome) part group as the template for shared columns
    (so_codes, mapped_objective_id, profile, raw_excerpt, source_page)."""
    def _template_for(part: str) -> dict:
        candidates = [new_r for _, _, new_r in rehomed_block6 if new_r["question_part"] == part]
        if not candidates:
            raise ValueError(f"No sibling row found to use as a template for part {part!r}")
        return candidates[0]

    def _next_order(part: str) -> str:
        orders = [
            int(_fld(new_r, "point_order"))
            for _, _, new_r in rehomed_block6 if new_r["question_part"] == part
        ]
        return str(max(orders) + 1) if orders else "1"

    def _build(part: str, marks_value: str, text: str) -> dict:
        template = _template_for(part)
        row = {k: template.get(k, "") for k in fieldnames}
        row["question_num"] = "6"
        row["question_part"] = part
        row["question_block_id"] = "6"
        row["part_occurrence"] = "1"
        row["point_order"] = _next_order(part)
        row["source_page"] = "97"
        row["marks_value"] = marks_value
        row["point_text"] = text
        row["verified"] = "1"
        row["parser_artifact"] = "0"
        row["excluded_reason"] = ""
        row["needs_manual_entry"] = "0"
        return row

    return [
        _build("(a)", "3", NEW_ROW_A_TEXT),
        _build("(c)", "8", NEW_ROW_C_TEXT),
    ]


def _describe(r: dict) -> str:
    return (
        f"qb{_fld(r,'question_block_id')}{_fld(r,'question_part')}"
        f"v{_fld(r,'part_occurrence')}-mp{_fld(r,'point_order')}"
        f"  | {_fld(r,'point_text')[:70]!r}"
    )


def _print_row_change(old_r: dict, new_r: dict) -> None:
    old_key = f"qb{_fld(old_r,'question_block_id')}{_fld(old_r,'question_part')}v{_fld(old_r,'part_occurrence')}-mp{_fld(old_r,'point_order')}"
    new_key = f"qb{_fld(new_r,'question_block_id')}{_fld(new_r,'question_part')}v{_fld(new_r,'part_occurrence')}-mp{_fld(new_r,'point_order')}"
    marker = "  (unchanged position)" if old_key == new_key else ""
    print(f"  {old_key} -> {new_key}{marker}")
    print(f"    | {_fld(new_r,'point_text')[:80]!r}")


def check_no_collisions(rows: list) -> None:
    """Reuse lock_mark_scheme's own partition_rows + check_collisions -- the
    exact code path Stage 3 locking runs -- against the FULL corrected CSV,
    not just blocks 5/6."""
    eligible, _ = partition_rows(rows)
    dupes = check_collisions(eligible, PREFIX)
    if dupes:
        lines = "\n".join(f"  {mpid!r} at indices {idx}" for mpid, idx in dupes.items())
        raise ValueError(f"Collision(s) detected after correction:\n{lines}")


def apply_correction(rows: list, rehomed_5: list, rehomed_6: list,
                     deleted: list, new_rows: list) -> list:
    """Return the corrected full row list: block-6 rows replaced in place
    (now under block 5), block-7/page-97 rows replaced in place (now under
    block 6) or dropped (boilerplate), and the 2 new rows inserted right
    after the last re-homed block-6 row."""
    replace_map = {i: new_r for i, _, new_r in rehomed_5}
    replace_map.update({i: new_r for i, _, new_r in rehomed_6})
    delete_indices = {i for i, _ in deleted}

    last_block6_source_index = max(i for i, _, _ in rehomed_6)

    out = []
    for i, r in enumerate(rows):
        if i in delete_indices:
            continue
        out.append(replace_map.get(i, r))
        if i == last_block6_source_index:
            out.extend(new_rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="One-off fix: realign Economics Question 6's mark-scheme "
                     "block_id assignment (see module docstring)."
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

    block6 = find_block6_rows(rows)
    block7_page97 = find_block7_page97_rows(rows)

    try:
        validate_source_shape(block6, block7_page97)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")

    block5_rows = [r for r in rows if _fld(r, "question_block_id") == "5"]

    rehomed_5 = build_rehomed_block5_rows(block6, block5_rows)
    rehomed_6, deleted = build_rehomed_block6_rows(block7_page97)
    new_rows = build_new_rows(rehomed_6, fieldnames)

    # ── Preview ──────────────────────────────────────────────────────────────
    print("=" * 70)
    print("Block 6 -> Block 5 (re-homed; real Question 5 cont'd content)")
    print("=" * 70)
    for _, old_r, new_r in rehomed_5:
        _print_row_change(old_r, new_r)

    print()
    print("=" * 70)
    print("Block 7 (source_page=97) -> Block 6 (re-homed; real Question 6)")
    print("=" * 70)
    for _, old_r, new_r in rehomed_6:
        relabel_note = ""
        if old_r["question_part"] != new_r["question_part"]:
            relabel_note = f"  [RELABELED {old_r['question_part']} -> {new_r['question_part']}]"
        _print_row_change(old_r, new_r)
        if relabel_note:
            print(f"   {relabel_note}")

    print()
    print("=" * 70)
    print(f"Deleted (boilerplate misattached from page 98) -- {len(deleted)} row(s)")
    print("=" * 70)
    for _, r in deleted:
        print(f"  {_describe(r)}")

    print()
    print("=" * 70)
    print(f"Added (new verified rows) -- {len(new_rows)} row(s)")
    print("=" * 70)
    for r in new_rows:
        print(f"  {_describe(r)}")

    # ── Build corrected rows + collision check (before any write) ──────────
    corrected_rows = apply_correction(rows, rehomed_5, rehomed_6, deleted, new_rows)

    print()
    print("=" * 70)
    print("Collision check (reusing lock_mark_scheme.partition_rows / check_collisions)")
    print("=" * 70)
    try:
        check_no_collisions(corrected_rows)
    except ValueError as exc:
        sys.exit(f"ERROR: {exc}")
    print("  OK -- no mark_point_id / point_group_id collisions in the corrected CSV.")

    # Explicit point_group_id listing for blocks 5 and 6, for visual confirmation.
    for block_id in ("5", "6"):
        keys = [
            build_point_group_id(
                PREFIX, _fld(r, "question_block_id"), _fld(r, "question_part"),
                _fld(r, "part_occurrence"), _fld(r, "point_order"),
            )
            for r in corrected_rows if _fld(r, "question_block_id") == block_id
        ]
        assert len(keys) == len(set(keys)), f"internal error: block {block_id} keys not unique"
        print(f"  block {block_id}: {len(keys)} point_group_id(s), all unique.")

    print()
    print(f"Row count: {len(rows)} -> {len(corrected_rows)} "
          f"(-{len(deleted)} deleted, +{len(new_rows)} added, net {len(corrected_rows)-len(rows):+d})")

    answer = input("\nApply this correction? [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted. No changes written.")
        return

    backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
    shutil.copy2(csv_path, backup_path)
    print(f"Backup written: {backup_path}")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(corrected_rows)

    print(f"CSV updated: {csv_path}")

    # ── Post-write tallies ───────────────────────────────────────────────────
    counts = {"verified": 0, "artifact": 0, "excluded": 0, "manual": 0}
    for r in corrected_rows:
        if _fld(r, "parser_artifact") == "1":
            counts["artifact"] += 1
        elif _fld(r, "excluded_reason"):
            counts["excluded"] += 1
        elif _fld(r, "needs_manual_entry") == "1":
            counts["manual"] += 1
        elif _fld(r, "verified") == "1":
            counts["verified"] += 1

    total = len(corrected_rows)
    summed = sum(counts.values())
    print(f"\nTallies after correction:")
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

    # ── Flag: does STEM_TEXTS or the new questions module reference a "6(d)"
    # that is confirmed absent from the source? ────────────────────────────
    print()
    print("=" * 70)
    print("qb6(d) reference check (per requirement 4)")
    print("=" * 70)
    print("  tools/ingest_econ_specimen_stems.py's STEM_TEXTS previously contained the")
    print("  id string 'ECON-qb6(d)v1-stem' -- its question_num VALUE was '5(d)', not")
    print("  '6(d)': it represents the REAL Q5(d) electronic-banking content (re-homed")
    print("  to question_block_id=5 / question_part=(d) by this script), not the")
    print("  nonexistent Q6(d). The id STRING's 'qb6' was a stale artifact of the old")
    print("  bogus block numbering this correction fixes -- flagged at the time this")
    print("  script ran; the key has SINCE been renamed to 'ECON-qb5(d)v1-stem' in a")
    print("  follow-up pass (content unchanged, id only) to match its real content.")
    print("  tools/ingest_econ_specimen_questions.py carries whatever id string is in")
    print("  STEM_TEXTS through unchanged and resolves it via question_num '5(d)', not")
    print("  '6(d)' -- it never produced any row claiming to be the real Question 6(d).")


if __name__ == "__main__":
    main()
