"""
tests/test_ingest_econ_specimen_questions.py
================================================
Unit tests for tools/ingest_econ_specimen_questions.py -- the real,
page-tagged replacement for the fabricated Economics specimen stems.

All PyMuPDF calls are mocked. The mocked page text for pages 73 and 79 is the
EXACT text PyMuPDF extracts from the real syllabus PDF (captured via
doc[72].get_text() / doc[78].get_text() against
E:\\CSEC_AI_STUDY_PARTNER\\03_KNOWLEDGE_BASE\\Economics\\00_SYLLABUS\\
csec-economics-syllabus-revised-2017.pdf) -- not paraphrased or invented.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

# ── Real extracted page text (verbatim PyMuPDF output) ──────────────────────
# Page 73: Question 1 (Production Possibility Curve, Table 1, parts (a) and
# (b)(i)/(ii)/(iii)) -- includes the full repeated boilerplate header and the
# "SECTION I" section marker that must never leak into Q1's stem text.
PAGE_73_TEXT = '- 4 -\nGO ON TO THE NEXT PAGE\n01216020/F/SPEC 2016\n‘‘*’’Barcode Area”*”\nSequential Bar Code\nDO NOT WRITE IN THIS AREA            DO NOT WRITE IN THIS AREA            DO NOT WRITE IN THIS AREA   \nSECTION I\nAnswer ALL FOUR questions in this section.\n1.\t\nTable 1 shows the combinations of sugar and bananas that Country X is capable of producing \nusing ALL of its resources.\nTABLE 1: PRODUCTION CAPABILITIES OF COUNTRY X\n\t\nCombination\nSugar (tons)\nBanana (tons)\nA\n0\n40 000\nB\n1 000\n25 000\nC\n2 000\n15 000\nD\n3 000\n9 000\nE\n4 000\n0\n\t\n(a)\t\nState the name of the curve that is normally used to represent the information in Table 1.\n\t\n\t\n..............................................................................................................................................\n\t\n\t\n\t\n\t\n(1 mark)\n\t\n(b)\t\n(i)\t\nMoving from combination A through E in Table 1, state if opportunity cost is \ndecreasing, increasing or constant.\n\t\n\t\n\t\n.................................................................................................................................\n\t\n(1 mark)\n\t\n\t\n\t\n(ii)\t\nState the maximum amount of sugar that can be produced if 40 000 tons of bananas \nare produced.\n\t\n\t\n\t\n................................................................................................................................\n(1 mark)\n \n \n(iii) \nState TWO factors that would cause the curve identified in (a) above to shift \ninwards.\n\t\n\t\n\t\n................................................................................................................................\n\t\n\t\n\t\n.................................................................................................................................\n\t\n\t\n\t\n.................................................................................................................................\n(2 marks)\n'

# Page 79: Question 4 (Economic Goals and GDP, parts (a)/(b)/(c), no
# sub-parts). Note: "economic goals" is wrapped in the PDF's own curly-quote
# glyphs, which PyMuPDF/the PDF's font encoding renders as U+FFFD -- a
# genuine source-PDF artifact, not something this script should invent a fix
# for (same class of issue as documented elsewhere in this project, e.g. the
# Mathematics garbled-formula case).
PAGE_79_TEXT = '- 10 -\nGO ON TO THE NEXT PAGE\n01216020/F/SPEC 2016\n‘‘*’’Barcode Area”*”\nSequential Bar Code\nDO NOT WRITE IN THIS AREA            DO NOT WRITE IN THIS AREA            DO NOT WRITE IN THIS AREA   \n4. \n(a) \nDefine the term ‘economic goals’.  \n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n(2 marks)\n\t\n(b)\t\nList THREE economic goals of a government. \t\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n..............................................................................................................................................\n\t\n\t\n..............................................................................................................................................\n(3 marks)\n\t\n(c)\t\nExplain TWO disadvantages of using Gross Domestic Product (GDP) as a measure of \nstandard of living.\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n..............................................................................................................................................\n\t\n\t\n..............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n\t\n\t\n.............................................................................................................................................\n(6 marks)\n'


def _make_mock_doc(page_texts: dict, total_pages: int = 84):
    """page_texts maps 1-based real page number -> raw PyMuPDF text.
    Pages not present return empty text (simulating untested intervening
    pages -- e.g. pages 74-78 between the two fixtures below)."""
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = total_pages

    def _getitem(self, i):
        real_page = i + 1
        mp = MagicMock()
        mp.get_text.return_value = page_texts.get(real_page, "")
        return mp

    mock_doc.__getitem__ = _getitem
    return mock_doc


def _import_iesq():
    """Import ingest_econ_specimen_questions with fitz mocked (no real PDF
    opened at import time -- the module only calls fitz.open() inside
    functions, not at module load)."""
    mock_fitz = types.ModuleType("fitz")
    mock_fitz.open = MagicMock()
    with patch.dict("sys.modules", {"fitz": mock_fitz}):
        if "ingest_econ_specimen_questions" in sys.modules:
            del sys.modules["ingest_econ_specimen_questions"]
        import ingest_econ_specimen_questions as iesq
    return iesq


def _parse_pages_73_and_79():
    """Parse a mocked 73-79 page range where only 73 and 79 have real
    content; returns the module and the parsed units dict.

    The module import and the parse call must happen inside the SAME
    patch.dict context: `import fitz` inside the module binds its `fitz`
    name to whatever sys.modules['fitz'] is at import time, so a mock
    configured only after import (or in a separate `with` block) never
    takes effect.
    """
    mock_doc = _make_mock_doc({73: PAGE_73_TEXT, 79: PAGE_79_TEXT}, total_pages=84)
    mock_fitz = types.ModuleType("fitz")
    mock_fitz.open = MagicMock(return_value=mock_doc)
    with patch.dict("sys.modules", {"fitz": mock_fitz}):
        if "ingest_econ_specimen_questions" in sys.modules:
            del sys.modules["ingest_econ_specimen_questions"]
        import ingest_econ_specimen_questions as iesq
        iesq._FITZ_AVAILABLE = True
        units = iesq.parse_specimen_questions("fake.pdf", 73, 79)
    return iesq, units


# ═══════════════════════════════════════════════════════════════════════════════
class TestBoilerplateStripping(unittest.TestCase):

    def test_boilerplate_header_removed(self):
        iesq = _import_iesq()
        cleaned = iesq.strip_boilerplate(PAGE_73_TEXT)
        self.assertNotIn("GO ON TO THE NEXT PAGE", cleaned)
        self.assertNotIn("01216020/F/SPEC 2016", cleaned)
        self.assertNotIn("Barcode Area", cleaned)
        self.assertNotIn("Sequential Bar Code", cleaned)
        self.assertNotIn("DO NOT WRITE IN THIS AREA", cleaned)
        self.assertNotIn("- 4 -", cleaned)

    def test_dot_leader_lines_removed(self):
        iesq = _import_iesq()
        cleaned = iesq.strip_boilerplate(PAGE_73_TEXT)
        self.assertNotIn("....", cleaned)

    def test_real_content_survives(self):
        iesq = _import_iesq()
        cleaned = iesq.strip_boilerplate(PAGE_73_TEXT)
        self.assertIn("TABLE 1: PRODUCTION CAPABILITIES OF COUNTRY X", cleaned)
        self.assertIn("State the name of the curve", cleaned)
        self.assertIn("(1 mark)", cleaned)


# ═══════════════════════════════════════════════════════════════════════════════
class TestSectionHeaderStripping(unittest.TestCase):

    def test_section_i_removed(self):
        iesq = _import_iesq()
        cleaned = iesq.strip_section_headers(iesq.strip_boilerplate(PAGE_73_TEXT))
        self.assertNotIn("SECTION I", cleaned)
        self.assertNotIn("Answer ALL FOUR questions in this section.", cleaned)

    def test_real_content_survives_section_strip(self):
        iesq = _import_iesq()
        cleaned = iesq.strip_section_headers(iesq.strip_boilerplate(PAGE_73_TEXT))
        self.assertIn("State the name of the curve", cleaned)


# ═══════════════════════════════════════════════════════════════════════════════
class TestParseSpecimenQuestions(unittest.TestCase):
    """End-to-end parsing against the real page 73 / page 79 text."""

    def test_q1_units_found_with_correct_parts(self):
        _, units = _parse_pages_73_and_79()
        for key in ("1(a)", "1(b)(i)", "1(b)(ii)", "1(b)(iii)"):
            self.assertIn(key, units, f"Expected unit {key!r} not found; got {sorted(units)}")

    def test_q4_units_found_with_correct_parts(self):
        _, units = _parse_pages_73_and_79()
        for key in ("4(a)", "4(b)", "4(c)"):
            self.assertIn(key, units, f"Expected unit {key!r} not found; got {sorted(units)}")

    def test_page_is_always_a_real_integer_never_none(self):
        _, units = _parse_pages_73_and_79()
        self.assertTrue(units, "Expected at least one parsed unit")
        for key, unit in units.items():
            self.assertIsInstance(unit["page"], int, f"{key}: page is not an int: {unit['page']!r}")
            self.assertIsNotNone(unit["page"], f"{key}: page is None")

    def test_q1_units_report_page_73(self):
        _, units = _parse_pages_73_and_79()
        for key in ("1(a)", "1(b)(i)", "1(b)(ii)", "1(b)(iii)"):
            self.assertEqual(units[key]["page"], 73, f"{key} should be on page 73")

    def test_q4_units_report_page_79(self):
        _, units = _parse_pages_73_and_79()
        for key in ("4(a)", "4(b)", "4(c)"):
            self.assertEqual(units[key]["page"], 79, f"{key} should be on page 79")

    def test_boilerplate_absent_from_all_stem_text(self):
        _, units = _parse_pages_73_and_79()
        for key, unit in units.items():
            text = unit["text"]
            self.assertNotIn("GO ON TO THE NEXT PAGE", text, key)
            self.assertNotIn("01216020", text, key)
            self.assertNotIn("DO NOT WRITE IN THIS AREA", text, key)
            self.assertNotIn("Barcode", text, key)
            self.assertNotIn("Sequential Bar Code", text, key)

    def test_section_header_absent_from_q1_stem(self):
        _, units = _parse_pages_73_and_79()
        self.assertNotIn("SECTION I", units["1(a)"]["text"])
        self.assertNotIn("Answer ALL FOUR questions in this section.", units["1(a)"]["text"])

    def test_q1_table_data_preserved_in_stem_text(self):
        """Table 1 data must appear in the stem text, not be dropped."""
        _, units = _parse_pages_73_and_79()
        text = units["1(a)"]["text"]
        self.assertIn("TABLE 1: PRODUCTION CAPABILITIES OF COUNTRY X", text)
        self.assertIn("40 000", text)

    def test_q1_shared_table_intro_prepended_to_each_part(self):
        """Every Q1 part depends on Table 1's context -- it must be prepended
        to (b)(i)/(ii)/(iii) too, not just (a)."""
        _, units = _parse_pages_73_and_79()
        for key in ("1(b)(i)", "1(b)(ii)", "1(b)(iii)"):
            self.assertIn("TABLE 1: PRODUCTION CAPABILITIES OF COUNTRY X", units[key]["text"])

    def test_q1_b_part_specific_content(self):
        _, units = _parse_pages_73_and_79()
        self.assertIn("decreasing, increasing or constant", units["1(b)(i)"]["text"])
        self.assertIn("maximum amount of sugar", units["1(b)(ii)"]["text"])
        self.assertIn("shift", units["1(b)(iii)"]["text"])
        self.assertIn("inwards", units["1(b)(iii)"]["text"])

    def test_q1_b_iii_reference_to_part_a_not_mistaken_for_new_part(self):
        """(iii)'s text references 'the curve identified in (a) above' -- this
        embedded, mid-sentence '(a)' must not be mistaken for a new top-level
        part boundary."""
        _, units = _parse_pages_73_and_79()
        self.assertIn("identified in (a) above", units["1(b)(iii)"]["text"])
        # And it must not have spuriously created/duplicated a "1(a)" unit
        # keyed off this embedded reference.
        _, units2 = _parse_pages_73_and_79()
        self.assertEqual(
            len([k for k in units2 if k == "1(a)"]), 1,
            "Embedded '(a)' reference inside (iii) must not create extra 1(a) units",
        )

    def test_q4_definition_text_is_real_not_fabricated(self):
        """Confirms the real Q4(a) definition prompt is captured, distinct
        from the OLD fabricated stem's wording."""
        _, units = _parse_pages_73_and_79()
        text = units["4(a)"]["text"]
        self.assertIn("Define the term", text)
        self.assertIn("economic goals", text)
        self.assertIn("(2 marks)", text)

    def test_q4_b_and_c_content(self):
        _, units = _parse_pages_73_and_79()
        self.assertIn("List THREE economic goals of a government", units["4(b)"]["text"])
        self.assertIn("Gross Domestic Product (GDP)", units["4(c)"]["text"])
        self.assertIn("standard of living", units["4(c)"]["text"])

    def test_no_dot_leaders_in_stem_text(self):
        _, units = _parse_pages_73_and_79()
        for key, unit in units.items():
            self.assertNotIn("....", unit["text"], key)


# ═══════════════════════════════════════════════════════════════════════════════
class TestQuestionIdMapping(unittest.TestCase):
    """build_review_rows / lookup_real_unit map onto the existing,
    never-invented STEM_TEXTS question_ids."""

    def test_fallback_used_for_1a_i_when_no_real_subpart(self):
        iesq, units = _parse_pages_73_and_79()
        unit, used_fallback = iesq.lookup_real_unit("1(a)(i)", units)
        self.assertIsNotNone(unit)
        self.assertTrue(used_fallback)
        self.assertEqual(unit, units["1(a)"])

    def test_exact_match_used_when_real_subpart_exists(self):
        iesq, units = _parse_pages_73_and_79()
        unit, used_fallback = iesq.lookup_real_unit("1(b)(i)", units)
        self.assertIsNotNone(unit)
        self.assertFalse(used_fallback)
        self.assertEqual(unit, units["1(b)(i)"])

    def test_split_question_num_key(self):
        iesq = _import_iesq()
        self.assertEqual(iesq._split_question_num_key("1(b)(i)"), ("1", "(b)(i)"))
        self.assertEqual(iesq._split_question_num_key("4(a)"), ("4", "(a)"))

    def test_unmapped_key_returns_none(self):
        iesq, units = _parse_pages_73_and_79()
        unit, used_fallback = iesq.lookup_real_unit("9(z)", units)
        self.assertIsNone(unit)
        self.assertFalse(used_fallback)


if __name__ == "__main__":
    unittest.main()
