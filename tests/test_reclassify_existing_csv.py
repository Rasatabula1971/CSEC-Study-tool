"""
tests/test_reclassify_existing_csv.py
========================================
Unit tests for tools/reclassify_existing_csv.py -- the one-off script that
re-applies classify_artifact_and_exclusion() to an already-extracted review
CSV without touching the PDF, the LLM, or any column besides parser_artifact
(and verified, per the state-exclusivity rule).
"""

import csv
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import reclassify_existing_csv as rec


ALL_COLUMNS = [
    "question_num", "question_group", "question_block_id",
    "question_part", "part_occurrence", "so_codes", "point_text",
    "marks_value", "point_order", "profile", "source_page",
    "raw_excerpt", "mapped_objective_id", "verified",
    "parser_artifact", "excluded_reason", "needs_manual_entry",
]


def _row(point_text, raw_excerpt="", verified="1", parser_artifact="0",
        excluded_reason="", question_block_id="1", question_part="(a)",
        part_occurrence="1", point_order="1", needs_manual_entry="0") -> dict:
    return {
        "question_num": "1",
        "question_group": "1",
        "question_block_id": question_block_id,
        "question_part": question_part,
        "part_occurrence": part_occurrence,
        "so_codes": "1.6",
        "point_text": point_text,
        "marks_value": "1",
        "point_order": point_order,
        "profile": "",
        "source_page": "90",
        "raw_excerpt": raw_excerpt,
        "mapped_objective_id": "ECON-1.6",
        "verified": verified,
        "parser_artifact": parser_artifact,
        "excluded_reason": excluded_reason,
        "needs_manual_entry": needs_manual_entry,
    }


def _write_csv(path: Path, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ═══════════════════════════════════════════════════════════════════════════════
class TestFindReclassifications(unittest.TestCase):

    def test_fragmentation_pattern_flagged_as_diff(self):
        """'= 2 x 2 =' currently verified=1/artifact=0 must be flagged to flip."""
        rows = [_row("= 2 x 2 =", verified="1", parser_artifact="0")]
        diffs = rec.find_reclassifications(rows)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["old_artifact"], "0")
        self.assertEqual(diffs[0]["new_artifact"], "1")

    def test_already_correctly_classified_row_not_in_diff(self):
        rows = [_row("Reduce inflation", verified="1", parser_artifact="0")]
        diffs = rec.find_reclassifications(rows)
        self.assertEqual(diffs, [])

    def test_already_artifact_row_not_in_diff(self):
        rows = [_row("Total", verified="0", parser_artifact="1")]
        diffs = rec.find_reclassifications(rows)
        self.assertEqual(diffs, [])

    def test_manually_flagged_artifact_never_unflagged_even_if_function_disagrees(self):
        """Additive-only regression guard, found via real-data audit: several
        rows in the live Economics CSV are correctly parser_artifact=1 from
        manual Stage 2 review for patterns classify_artifact_and_exclusion()
        does not model (e.g. a space-separated empty parenthetical "( )",
        vs. the function's "()" no-space pattern). Reclassification must
        NEVER flip an already-flagged row back to 0, even when the automated
        function would independently compute "0" for its current text."""
        rows = [
            _row("( )", verified="0", parser_artifact="1", point_order="1"),
            _row("[ ]", verified="0", parser_artifact="1", point_order="2"),
            _row("(any two,  each)", verified="0", parser_artifact="1", point_order="3"),
        ]
        diffs = rec.find_reclassifications(rows)
        self.assertEqual(diffs, [])
        # Confirm the premise: the automated function really would disagree,
        # which is exactly why the additive-only guard is load-bearing here.
        import extract_mark_scheme as ems
        for r in rows:
            new_artifact, _ = ems.classify_artifact_and_exclusion(r["point_text"])
            self.assertEqual(new_artifact, "0", f"premise check failed for {r['point_text']!r}")

    def test_excluded_reason_already_set_never_flagged(self):
        """Even though '= 2 x 2 =' independently matches an artifact pattern,
        a row that already has excluded_reason set must never be touched --
        precedence preserved."""
        rows = [_row("= 2 x 2 =", verified="0", parser_artifact="0",
                     excluded_reason="duplicate_of_block_8-15")]
        diffs = rec.find_reclassifications(rows)
        self.assertEqual(diffs, [])

    def test_multiple_rows_only_matching_ones_diffed(self):
        rows = [
            _row("= 2 x 2 =", verified="1", parser_artifact="0", point_order="1"),
            _row("Reduce inflation", verified="1", parser_artifact="0", point_order="2"),
            _row("be poor.                      ()", verified="1", parser_artifact="0",
                 point_order="3"),
            _row("Land earns rent", verified="1", parser_artifact="0", point_order="4"),
        ]
        diffs = rec.find_reclassifications(rows)
        flagged_orders = {d["row"]["point_order"] for d in diffs}
        self.assertEqual(flagged_orders, {"1", "3"})


# ═══════════════════════════════════════════════════════════════════════════════
class TestApplyReclassifications(unittest.TestCase):

    def test_flip_0_to_1_forces_verified_to_0(self):
        rows = [_row("= 2 x 2 =", verified="1", parser_artifact="0")]
        diffs = rec.find_reclassifications(rows)
        rec.apply_reclassifications(diffs)
        self.assertEqual(rows[0]["parser_artifact"], "1")
        self.assertEqual(rows[0]["verified"], "0")

    def test_only_parser_artifact_and_verified_columns_change(self):
        original = _row(
            "of living each", verified="1", parser_artifact="0",
            raw_excerpt="some surrounding context", question_block_id="4",
            question_part="(c)", part_occurrence="2", point_order="3",
        )
        row = dict(original)
        diffs = rec.find_reclassifications([row])
        self.assertEqual(len(diffs), 1)
        rec.apply_reclassifications(diffs)

        for key in ALL_COLUMNS:
            if key in ("parser_artifact", "verified"):
                continue
            self.assertEqual(
                row[key], original[key],
                f"Column {key!r} must never be modified by reclassification",
            )
        self.assertEqual(row["parser_artifact"], "1")
        self.assertEqual(row["verified"], "0")


# ═══════════════════════════════════════════════════════════════════════════════
class TestTally(unittest.TestCase):

    def test_tally_counts_each_state_once_and_sums_to_total(self):
        rows = [
            _row("Reduce inflation", verified="1", parser_artifact="0"),
            _row("Total", verified="0", parser_artifact="1"),
            _row("Boilerplate", verified="0", parser_artifact="0",
                 excluded_reason="out_of_scope_paper_03_2"),
            _row("Bundled content", verified="0", parser_artifact="0",
                 needs_manual_entry="1"),
        ]
        counts = rec.tally(rows)
        self.assertEqual(counts["verified"], 1)
        self.assertEqual(counts["artifact"], 1)
        self.assertEqual(counts["excluded"], 1)
        self.assertEqual(counts["manual"], 1)
        self.assertEqual(sum(counts.values()), len(rows))

    def test_orphan_row_not_double_counted_and_breaks_the_sum(self):
        """A genuinely unreviewed row (verified=0, no flags) matches none of
        the four states and should not inflate any bucket."""
        rows = [
            _row("Reduce inflation", verified="1", parser_artifact="0"),
            _row("Unreviewed dangling row", verified="0", parser_artifact="0"),
        ]
        counts = rec.tally(rows)
        self.assertEqual(sum(counts.values()), 1)  # only the verified row counted
        self.assertNotEqual(sum(counts.values()), len(rows))


# ═══════════════════════════════════════════════════════════════════════════════
class TestEndToEndCsvWrite(unittest.TestCase):
    """Full file round-trip: write a fixture CSV, run the reclassification
    write path directly (bypassing the interactive prompt), and confirm the
    file on disk reflects only the expected column changes."""

    def test_full_write_flow(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Economics_mark_scheme_review.csv"
            rows_in = [
                _row("= 2 x 2 =", verified="1", parser_artifact="0", point_order="1"),
                _row("Reduce inflation", verified="1", parser_artifact="0", point_order="2"),
                _row("legacy excluded row", verified="0", parser_artifact="0",
                     excluded_reason="duplicate_of_block_8-15", point_order="3"),
            ]
            _write_csv(csv_path, rows_in)

            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                fieldnames = reader.fieldnames

            diffs = rec.find_reclassifications(rows)
            self.assertEqual(len(diffs), 1)  # only the arithmetic-echo row
            rec.apply_reclassifications(diffs)

            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            result = _read_csv(csv_path)
            by_order = {r["point_order"]: r for r in result}

            self.assertEqual(by_order["1"]["parser_artifact"], "1")
            self.assertEqual(by_order["1"]["verified"], "0")

            self.assertEqual(by_order["2"]["parser_artifact"], "0")
            self.assertEqual(by_order["2"]["verified"], "1")

            # The already-excluded row must remain completely untouched.
            self.assertEqual(by_order["3"]["parser_artifact"], "0")
            self.assertEqual(by_order["3"]["excluded_reason"], "duplicate_of_block_8-15")
            self.assertEqual(by_order["3"]["verified"], "0")


# ═══════════════════════════════════════════════════════════════════════════════
class TestMainCli(unittest.TestCase):
    """Smoke-test the CLI entry point end to end, including the confirm
    prompt and the .bak backup file, via main()."""

    def test_main_writes_backup_and_applies_confirmed_changes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Economics_mark_scheme_review.csv"
            _write_csv(csv_path, [
                _row("= 2 x 2 =", verified="1", parser_artifact="0"),
            ])

            with patch("builtins.input", return_value="y"), \
                 patch.object(sys, "argv", ["reclassify_existing_csv.py", "--csv-file", str(csv_path)]):
                rec.main()

            backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
            self.assertTrue(backup_path.exists())

            result = _read_csv(csv_path)
            self.assertEqual(result[0]["parser_artifact"], "1")
            self.assertEqual(result[0]["verified"], "0")

    def test_main_aborts_when_not_confirmed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Economics_mark_scheme_review.csv"
            _write_csv(csv_path, [
                _row("= 2 x 2 =", verified="1", parser_artifact="0"),
            ])

            with patch("builtins.input", return_value="n"), \
                 patch.object(sys, "argv", ["reclassify_existing_csv.py", "--csv-file", str(csv_path)]):
                rec.main()

            backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
            self.assertFalse(backup_path.exists())

            result = _read_csv(csv_path)
            self.assertEqual(result[0]["parser_artifact"], "0")  # unchanged
            self.assertEqual(result[0]["verified"], "1")

    def test_main_no_diffs_prints_nothing_to_do_and_writes_no_backup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "Economics_mark_scheme_review.csv"
            _write_csv(csv_path, [
                _row("Reduce inflation", verified="1", parser_artifact="0"),
            ])

            with patch("builtins.input", side_effect=AssertionError("should not prompt")), \
                 patch.object(sys, "argv", ["reclassify_existing_csv.py", "--csv-file", str(csv_path)]):
                rec.main()  # must not raise -- no diffs means no prompt at all

            backup_path = csv_path.with_suffix(csv_path.suffix + ".bak")
            self.assertFalse(backup_path.exists())


if __name__ == "__main__":
    unittest.main()
