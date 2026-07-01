"""
tests/test_extract_mark_scheme.py
===================================
Unit tests for the Stage-1 mark-scheme extraction pipeline.

All PyMuPDF calls are mocked — no real PDF is opened.
All LLM calls are mocked — no real model is called.
"""

import json
import sqlite3
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Path setup ───────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "db"))

# ── Canned mark-scheme page text (mirrors the real Economics 2016 specimen) ──
Q1_TEXT = """\
Question 1

S.O: 1.6, 1.8

KC IA APP
(a) (i)  Production possibility curve                      1 mark

(b) (i)  Decreasing
    Stating correct type of cost                 1 mark

(b) (ii) 0 tons of sugar
    Stating correct amount                       1 mark

(b) (iii)  Emigration, depletion of renewable resources,
    natural disasters, fall in productivity.
    For each factor listed                       1 mark
    2 marks

(c) (i)  The country is operating efficiently since at a production
    level of 2,000 tonnes 15,000 tonnes of bananas can be
    produced, implying that the country is on the production
    possibility curve.
    For identification of position               1 mark
    For explanation  (2x2)                       3 marks

(d) • Costs associated with providing the service.
    • The number of providers of internet cafes.
    • The profit that can be generated.
    • The market demand for the product.
    For each factor listed                       1 mark
    For each factor developed into explanation   2 marks each

5 6 4
"""

Q2_TEXT = """\
Question 2

S.O: 2.3, 2.7

KC IA APP
(a) (i)  The network of organizations designed by countries.
    For a complete definition                    2 marks
    For a partial definition                     1 mark

(b)  The responses will include:
    • Risk-bearing economies.
    • Purchasing economies.
    • Managerial economies.
    For identifying each economy of scale        1 mark

5 6 4
"""

CANNED_PAGES = [
    (90, Q1_TEXT),
    (91, Q2_TEXT),
]


def _make_mock_doc(pages_text: list[tuple[int, str]]):
    """Build a minimal PyMuPDF-like mock that supports iteration and indexing."""
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = pages_text[-1][0] if pages_text else 0

    page_objects = []
    for _, text in pages_text:
        mp = MagicMock()
        mp.get_text.return_value = text
        page_objects.append(mp)

    mock_doc.__getitem__ = lambda self, i: page_objects[i]
    return mock_doc


# ── Helper: run parse_mark_scheme with mocked fitz ──────────────────────────
def _parse_canned(pages=CANNED_PAGES):
    """Import extract_mark_scheme with fitz mocked, parse canned pages."""
    # Patch fitz before importing the module
    mock_fitz = types.ModuleType("fitz")
    mock_doc  = _make_mock_doc(pages)
    mock_fitz.open = MagicMock(return_value=mock_doc)

    with patch.dict("sys.modules", {"fitz": mock_fitz}):
        # Force fresh import each test
        if "extract_mark_scheme" in sys.modules:
            del sys.modules["extract_mark_scheme"]
        import extract_mark_scheme as ems
        ems._FITZ_AVAILABLE = True
        # Patch llm fallback so it never calls real model
        ems.llm_extract_mark_points = MagicMock(return_value=[])
        rows = ems.parse_mark_scheme("fake.pdf", start_page=1, end_page=2)
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
class TestSOParsing(unittest.TestCase):
    """parse_so_codes correctly tokenises S.O. header strings."""

    def _import_ems(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems
        return ems

    def test_standard_two_codes(self):
        ems = self._import_ems()
        codes = ems.parse_so_codes("1.6, 1.8")
        self.assertEqual(codes, ["1.6", "1.8"])

    def test_four_codes(self):
        ems = self._import_ems()
        codes = ems.parse_so_codes("6.9, 6.11, 6.12, 6.14")
        self.assertEqual(codes, ["6.9", "6.11", "6.12", "6.14"])

    def test_single_code(self):
        ems = self._import_ems()
        codes = ems.parse_so_codes("3.5")
        self.assertEqual(codes, ["3.5"])

    def test_strips_trailing_dot(self):
        ems = self._import_ems()
        codes = ems.parse_so_codes("1.6,")
        self.assertEqual(codes, ["1.6"])


# ═══════════════════════════════════════════════════════════════════════════════
class TestStructuralParser(unittest.TestCase):
    """parse_mark_scheme correctly splits questions and parts."""

    def test_extracts_two_questions(self):
        rows = _parse_canned()
        question_nums = {r["question_num"] for r in rows}
        self.assertIn("1", question_nums)
        self.assertIn("2", question_nums)

    def test_q1_has_multiple_parts(self):
        rows = _parse_canned()
        q1_parts = {r["question_part"] for r in rows if r["question_num"] == "1"}
        # Should have at least (a), (b), (c), (d) — possibly sub-parts too
        self.assertGreater(len(q1_parts), 1)

    def test_so_codes_attached_to_rows(self):
        rows = _parse_canned()
        q1_rows = [r for r in rows if r["question_num"] == "1"]
        self.assertTrue(all(r["so_codes"] == "1.6,1.8" for r in q1_rows))

    def test_q2_so_codes(self):
        rows = _parse_canned()
        q2_rows = [r for r in rows if r["question_num"] == "2"]
        self.assertTrue(q2_rows)
        self.assertTrue(all(r["so_codes"] == "2.3,2.7" for r in q2_rows))

    def test_bullet_points_extracted(self):
        rows = _parse_canned()
        # Q2 (b) has three explicit bullets
        q2b_rows = [r for r in rows if r["question_num"] == "2" and r["question_part"] == "(b)"]
        point_texts = [r["point_text"] for r in q2b_rows]
        self.assertTrue(
            any("Risk-bearing" in pt for pt in point_texts),
            f"Expected 'Risk-bearing' bullet; got: {point_texts}",
        )

    def test_question_group_is_set(self):
        rows = _parse_canned()
        # Two questions, each appearing once → both should be group 1
        for r in rows:
            self.assertIn("question_group", r)
            self.assertEqual(r["question_group"], 1)

    def test_verified_defaults_to_zero(self):
        rows = _parse_canned()
        self.assertTrue(all(r["verified"] == 0 for r in rows))

    def test_source_page_is_set(self):
        rows = _parse_canned()
        self.assertTrue(all(r["source_page"] in (1, 2, 90, 91) for r in rows))

    def test_repeated_question_num_gets_group_2(self):
        # Two pages both containing "Question 1" → first occurrence group 1, second group 2
        page_a = Q1_TEXT   # question_num "1", first encounter
        page_b = Q1_TEXT   # question_num "1" again (simulates a second specimen paper)
        rows = _parse_canned([(90, page_a), (91, page_b)])
        q1_groups = sorted({r["question_group"] for r in rows if r["question_num"] == "1"})
        self.assertEqual(q1_groups, [1, 2])


# ═══════════════════════════════════════════════════════════════════════════════
class TestReviewNeededFlag(unittest.TestCase):
    """Sections that can't be fully parsed emit [REVIEW NEEDED:] rows."""

    def test_review_needed_when_no_bullets_and_llm_returns_empty(self):
        """A part with a mark allocation but no extractable bullets and a
        failing LLM fallback should produce a [REVIEW NEEDED:] row."""

        sparse_page = """\
Question 3

S.O: 3.1

KC IA APP
(a) Some complex prose explanation that has no bullet formatting.
    For a clear and adequate explanation    6 marks

5 6 4
"""
        mock_fitz = types.ModuleType("fitz")
        mock_doc  = _make_mock_doc([(90, sparse_page)])
        mock_fitz.open = MagicMock(return_value=mock_doc)

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems
            ems._FITZ_AVAILABLE = True
            ems.llm_extract_mark_points = MagicMock(return_value=[])
            rows = ems.parse_mark_scheme("fake.pdf", 1, 1)

        review_rows = [r for r in rows if "[REVIEW NEEDED:" in r["point_text"]]
        self.assertTrue(
            len(review_rows) > 0,
            f"Expected at least one [REVIEW NEEDED:] row; got rows: {rows}",
        )


# ═══════════════════════════════════════════════════════════════════════════════
class TestObjectiveMapping(unittest.TestCase):
    """map_so_to_objective correctly resolves S.O. codes against the DB."""

    def _setup_db(self) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("""
            CREATE TABLE subjects (
                subject_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                syllabus_locked INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE syllabus_sections (
                section_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                title TEXT NOT NULL,
                section_num TEXT
            )
        """)
        db.execute("""
            CREATE TABLE objectives (
                objective_id  TEXT PRIMARY KEY,
                section_id    TEXT NOT NULL,
                subject_id    TEXT NOT NULL,
                objective_num TEXT NOT NULL,
                content_stmt  TEXT NOT NULL
            )
        """)
        db.execute("INSERT INTO subjects VALUES ('Economics','Economics',1)")
        db.execute("INSERT INTO syllabus_sections VALUES ('ECON-S1','Economics','Production',1)")
        db.execute("INSERT INTO objectives VALUES "
                   "('ECON-1.6','ECON-S1','Economics','1.6','Describe PPC')")
        db.execute("INSERT INTO objectives VALUES "
                   "('ECON-1.8','ECON-S1','Economics','1.8','Define opportunity cost')")
        db.commit()
        return db

    def test_single_code_resolves(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems

        db = self._setup_db()
        result = ems.map_so_to_objective(db, "Economics", ["1.6"])
        self.assertEqual(result, "ECON-1.6")

    def test_two_codes_resolve(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems

        db = self._setup_db()
        result = ems.map_so_to_objective(db, "Economics", ["1.6", "1.8"])
        self.assertIn("ECON-1.6", result)
        self.assertIn("ECON-1.8", result)

    def test_unknown_code_returns_empty(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems

        db = self._setup_db()
        result = ems.map_so_to_objective(db, "Economics", ["9.99"])
        self.assertEqual(result, "")

    def test_fill_objective_ids_mutates_rows(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems

        db = self._setup_db()
        rows = [{"so_codes": "1.6,1.8", "mapped_objective_id": ""}]
        ems.fill_objective_ids(rows, db, "Economics")
        self.assertIn("ECON-1.6", rows[0]["mapped_objective_id"])
        self.assertIn("ECON-1.8", rows[0]["mapped_objective_id"])


# ═══════════════════════════════════════════════════════════════════════════════
class TestClassificationPrecedence(unittest.TestCase):
    """parser_artifact and excluded_reason must be mutually exclusive.

    When a row's text matches a parser-artifact pattern (e.g. a bare "Total"
    summation line) AND its surrounding segment matches known exam-booklet
    boilerplate (contamination from a document-structure overrun),
    excluded_reason must win and parser_artifact must be forced to "0".
    This is the real Economics scenario: a "Total" line absorbed into a
    contaminated block previously produced a row with BOTH flags set.
    """

    def _import_ems(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems
        return ems

    def test_bare_total_with_no_contamination_is_plain_artifact(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "Total", raw_excerpt="(d) Some ordinary part text. Total"
        )
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_bare_total_inside_contaminated_block_prefers_excluded(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "Total",
            raw_excerpt="Answer ALL the questions in this section. Total",
        )
        self.assertEqual(parser_artifact, "0")
        self.assertEqual(excluded_reason, "contaminated_exam_instructions")

    def test_non_artifact_text_unaffected(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "Production possibility curve", raw_excerpt="(a) Production possibility curve"
        )
        self.assertEqual(parser_artifact, "0")
        self.assertEqual(excluded_reason, "")

    def test_parse_mark_scheme_total_summation_line_classified_correctly(self):
        """End-to-end: an LLM-fallback row with point_text 'Total', extracted
        from a segment whose text contains exam-cover boilerplate, must come
        out of parse_mark_scheme with excluded_reason set and parser_artifact
        forced to '0' — never both flags set."""
        contaminated_page = """\
Question 7

S.O: 6.9

KC IA APP
(a) Answer ALL the questions in this section. Silent electronic calculators may be used.
    Some complex prose with no bullets at all describing the total.
    For a clear explanation    6 marks

5 6 4
"""
        mock_fitz = types.ModuleType("fitz")
        mock_doc  = _make_mock_doc([(97, contaminated_page)])
        mock_fitz.open = MagicMock(return_value=mock_doc)

        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems
            ems._FITZ_AVAILABLE = True
            ems.llm_extract_mark_points = MagicMock(
                return_value=[{"point_text": "Total", "marks_value": 1}]
            )
            rows = ems.parse_mark_scheme("fake.pdf", 1, 1, dry_run=False)

        total_rows = [r for r in rows if r["point_text"].strip() == "Total"]
        self.assertTrue(total_rows, f"Expected a 'Total' row; got: {rows}")
        for r in total_rows:
            self.assertEqual(r["excluded_reason"], "contaminated_exam_instructions")
            self.assertEqual(r["parser_artifact"], "0")
            self.assertFalse(
                r["parser_artifact"] == "1" and r["excluded_reason"],
                "Row must never have both parser_artifact=1 and excluded_reason set",
            )


# ═══════════════════════════════════════════════════════════════════════════════
class TestFragmentationArtifactDetection(unittest.TestCase):
    """classify_artifact_and_exclusion catches PDF line-break fragmentation:
    bare arithmetic echoes, dangling empty parentheticals, and lowercase-led
    sentence fragments -- all real rows found in a manual audit of the
    Economics review CSV."""

    def _import_ems(self):
        mock_fitz = types.ModuleType("fitz")
        mock_fitz.open = MagicMock()
        with patch.dict("sys.modules", {"fitz": mock_fitz}):
            if "extract_mark_scheme" in sys.modules:
                del sys.modules["extract_mark_scheme"]
            import extract_mark_scheme as ems
        return ems

    def test_bare_arithmetic_echo_equals_sign_leading(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("= 2 x 2 =")
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_bare_arithmetic_echo_no_leading_equals(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("2 x 3 =")
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_empty_parenthetical_after_sentence(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("be poor. ()")
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_empty_parenthetical_long_sentence(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "It ignores income distribution ()"
        )
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_lowercase_start_and_empty_parenthetical_both_match_not_double_counted(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "a small food stall holder to a restaurant.()"
        )
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_lowercase_start_fragment(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("of living each")
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_legitimate_short_capitalized_row_unaffected_reduce_inflation(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("Reduce inflation")
        self.assertEqual(parser_artifact, "0")
        self.assertEqual(excluded_reason, "")

    def test_legitimate_short_capitalized_row_unaffected_land_earns_rent(self):
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("Land earns rent")
        self.assertEqual(parser_artifact, "0")
        self.assertEqual(excluded_reason, "")

    def test_exact_match_and_lowercase_rule_overlap_not_double_counted(self):
        """'each' is both an exact-match rubric label AND lowercase-leading --
        must still resolve to a single parser_artifact=1, not fail or duplicate."""
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion("each")
        self.assertEqual(parser_artifact, "1")
        self.assertEqual(excluded_reason, "")

    def test_is_list_continuation_flag_suppresses_lowercase_rule_only(self):
        """A caller that already knows a lowercase-leading row is a deliberate
        list continuation can suppress just that check -- other artifact
        checks (e.g. empty parenthetical) still apply."""
        ems = self._import_ems()
        # Lowercase-leading but otherwise ordinary text: continuation flag saves it.
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "of the three main factors", is_list_continuation=True
        )
        self.assertEqual(parser_artifact, "0")
        self.assertEqual(excluded_reason, "")

        # Lowercase-leading AND an empty parenthetical: the parenthetical rule
        # still fires even with the continuation flag set.
        parser_artifact2, excluded_reason2 = ems.classify_artifact_and_exclusion(
            "a small food stall holder to a restaurant.()", is_list_continuation=True
        )
        self.assertEqual(parser_artifact2, "1")
        self.assertEqual(excluded_reason2, "")

    def test_excluded_reason_still_takes_precedence_over_fragmentation_rules(self):
        """Contamination detection must still win even when the fragment text
        itself independently matches a new fragmentation pattern."""
        ems = self._import_ems()
        parser_artifact, excluded_reason = ems.classify_artifact_and_exclusion(
            "= 2 x 2 =",
            raw_excerpt="Answer ALL the questions in this section. = 2 x 2 =",
        )
        self.assertEqual(parser_artifact, "0")
        self.assertEqual(excluded_reason, "contaminated_exam_instructions")


# ═══════════════════════════════════════════════════════════════════════════════
class TestPageRangeConfig(unittest.TestCase):
    """mark_scheme_page_ranges.json is present and has the Economics entry."""

    def test_config_file_exists(self):
        cfg_path = ROOT / "tools" / "mark_scheme_page_ranges.json"
        self.assertTrue(cfg_path.exists(), f"Not found: {cfg_path}")

    def test_economics_pages_set(self):
        cfg_path = ROOT / "tools" / "mark_scheme_page_ranges.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        self.assertIn("Economics", cfg)
        pages = cfg["Economics"]["pages"]
        self.assertEqual(pages, [90, 128])

    def test_other_subjects_have_null_pages(self):
        cfg_path = ROOT / "tools" / "mark_scheme_page_ranges.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        for subj, entry in cfg.items():
            if subj == "Economics":
                continue
            self.assertEqual(
                entry["pages"], [None, None],
                f"{subj} should have [null, null] until located",
            )


if __name__ == "__main__":
    unittest.main()
