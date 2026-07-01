"""
tests/test_render_review_packet.py
=====================================
Unit tests for tools/render_review_packet.py — the Stage 2 review packet
builder that pairs extracted CSV rows with their source PDF page image.

All PyMuPDF calls are mocked — no real PDF is opened or rendered.
"""

import csv
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


def _make_mock_doc(num_pages: int):
    """Build a minimal PyMuPDF-like mock document with `num_pages` pages.

    Each page's get_pixmap(...) returns a pixmap mock whose .save(path) is a
    no-op (recorded via MagicMock for call assertions) -- no real PNG bytes
    are written to disk.
    """
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = num_pages

    page_objects = []
    for _ in range(num_pages):
        mock_pixmap = MagicMock()
        mock_pixmap.save = MagicMock()
        mock_page = MagicMock()
        mock_page.get_pixmap = MagicMock(return_value=mock_pixmap)
        page_objects.append(mock_page)

    mock_doc.__getitem__ = lambda self, i: page_objects[i]
    return mock_doc


def _import_rrp(num_pages: int = 200):
    """Import render_review_packet with fitz mocked. Returns (module, mock_doc)."""
    mock_fitz = types.ModuleType("fitz")
    mock_doc = _make_mock_doc(num_pages)
    mock_fitz.open = MagicMock(return_value=mock_doc)

    with patch.dict("sys.modules", {"fitz": mock_fitz}):
        if "render_review_packet" in sys.modules:
            del sys.modules["render_review_packet"]
        import render_review_packet as rrp
        rrp._FITZ_AVAILABLE = True
    return rrp, mock_doc


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "question_num", "question_group", "question_block_id",
        "question_part", "part_occurrence", "so_codes", "point_text",
        "marks_value", "point_order", "profile", "source_page",
        "raw_excerpt", "mapped_objective_id", "verified",
        "parser_artifact", "excluded_reason", "needs_manual_entry",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _row(source_page, question_num="1", question_part="(a)", point_text="Some point",
        marks_value="1", verified="1", excluded_reason="", parser_artifact="0") -> dict:
    return {
        "question_num": question_num,
        "question_group": "1",
        "question_block_id": "1",
        "question_part": question_part,
        "part_occurrence": "1",
        "so_codes": "1.6",
        "point_text": point_text,
        "marks_value": marks_value,
        "point_order": "1",
        "profile": "",
        "source_page": str(source_page),
        "raw_excerpt": "",
        "mapped_objective_id": "ECON-1.6",
        "verified": verified,
        "parser_artifact": parser_artifact,
        "excluded_reason": excluded_reason,
        "needs_manual_entry": "0",
    }


# ═══════════════════════════════════════════════════════════════════════════════
class TestParsePagesArg(unittest.TestCase):

    def test_simple_range(self):
        rrp, _ = _import_rrp()
        self.assertEqual(rrp.parse_pages_arg("107-111"), [107, 108, 109, 110, 111])

    def test_singles_and_ranges_mixed(self):
        rrp, _ = _import_rrp()
        self.assertEqual(
            rrp.parse_pages_arg("107-111,126,128"),
            [107, 108, 109, 110, 111, 126, 128],
        )

    def test_dedupes_and_sorts(self):
        rrp, _ = _import_rrp()
        self.assertEqual(rrp.parse_pages_arg("90,89,90,88"), [88, 89, 90])

    def test_reversed_range_normalised(self):
        rrp, _ = _import_rrp()
        self.assertEqual(rrp.parse_pages_arg("111-109"), [109, 110, 111])


# ═══════════════════════════════════════════════════════════════════════════════
class TestFilterRowsForPage(unittest.TestCase):

    def test_filters_correctly_per_page(self):
        rrp, _ = _import_rrp()
        rows = [
            _row(source_page=90, point_text="A"),
            _row(source_page=90, point_text="B"),
            _row(source_page=91, point_text="C"),
        ]
        page90 = rrp.filter_rows_for_page(rows, 90)
        page91 = rrp.filter_rows_for_page(rows, 91)
        page92 = rrp.filter_rows_for_page(rows, 92)

        self.assertEqual([r["point_text"] for r in page90], ["A", "B"])
        self.assertEqual([r["point_text"] for r in page91], ["C"])
        self.assertEqual(page92, [])

    def test_empty_source_page_excluded(self):
        rrp, _ = _import_rrp()
        rows = [_row(source_page=""), _row(source_page=90)]
        # First row's source_page is forced empty manually
        rows[0]["source_page"] = ""
        matched = rrp.filter_rows_for_page(rows, 90)
        self.assertEqual(len(matched), 1)

    def test_non_numeric_source_page_excluded(self):
        rrp, _ = _import_rrp()
        rows = [_row(source_page=90)]
        rows[0]["source_page"] = "not-a-page"
        matched = rrp.filter_rows_for_page(rows, 90)
        self.assertEqual(matched, [])


# ═══════════════════════════════════════════════════════════════════════════════
class TestRenderPageImages(unittest.TestCase):

    def test_renders_each_requested_page(self):
        import tempfile
        rrp, mock_doc = _import_rrp(num_pages=200)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            image_paths = rrp.render_page_images("fake.pdf", [90, 91, 92], out_dir)

        self.assertEqual(set(image_paths.keys()), {90, 91, 92})
        for page, path in image_paths.items():
            self.assertEqual(path.name, f"page_{page}.png")
            self.assertEqual(path.parent.name, "pages")

    def test_out_of_range_page_skipped_not_raised(self):
        import tempfile
        rrp, mock_doc = _import_rrp(num_pages=5)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            image_paths = rrp.render_page_images("fake.pdf", [1, 999], out_dir)

        self.assertEqual(set(image_paths.keys()), {1})

    def test_pages_dir_created(self):
        import tempfile
        rrp, mock_doc = _import_rrp(num_pages=10)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            rrp.render_page_images("fake.pdf", [1], out_dir)
            self.assertTrue((out_dir / "pages").is_dir())


# ═══════════════════════════════════════════════════════════════════════════════
class TestBuildHtml(unittest.TestCase):
    """build_html: one section per requested page, rows filtered correctly,
    and an empty-but-present table for pages with zero matching rows."""

    def _fake_image_paths(self, pages: list[int]) -> dict:
        return {p: Path(f"/tmp/fake/pages/page_{p}.png") for p in pages}

    def test_one_section_per_requested_page(self):
        rrp, _ = _import_rrp()
        pages = [90, 91, 92]
        html_out = rrp.build_html(pages, [], self._fake_image_paths(pages))
        for p in pages:
            self.assertIn(f'id="page-{p}"', html_out)
        self.assertEqual(html_out.count('class="page-section"'), 3)

    def test_rows_filtered_correctly_per_page(self):
        rrp, _ = _import_rrp()
        pages = [90, 91]
        rows = [
            _row(source_page=90, point_text="UniqueTextForPage90"),
            _row(source_page=91, point_text="UniqueTextForPage91"),
        ]
        html_out = rrp.build_html(pages, rows, self._fake_image_paths(pages))

        # Split into per-page sections for a precise containment check.
        section_90 = html_out.split('id="page-90"')[1].split('id="page-91"')[0]
        section_91 = html_out.split('id="page-91"')[1]

        self.assertIn("UniqueTextForPage90", section_90)
        self.assertNotIn("UniqueTextForPage91", section_90)
        self.assertIn("UniqueTextForPage91", section_91)
        self.assertNotIn("UniqueTextForPage90", section_91)

    def test_page_with_zero_rows_still_renders_image_with_empty_table(self):
        rrp, _ = _import_rrp()
        pages = [90]
        image_paths = self._fake_image_paths(pages)
        html_out = rrp.build_html(pages, [], image_paths)  # no rows at all

        self.assertIn(f'src="pages/{image_paths[90].name}"', html_out)
        self.assertIn("<table>", html_out)
        self.assertIn("<thead>", html_out)
        # No <td> data cells should appear for this page's table body
        self.assertNotIn("<td>", html_out)
        self.assertIn("0 row(s) on this page.", html_out)

    def test_missing_image_path_shows_placeholder_not_crash(self):
        rrp, _ = _import_rrp()
        html_out = rrp.build_html([90], [], {})  # no image rendered for page 90
        self.assertIn("missing-image", html_out)
        self.assertIn("Page 90 image not available.", html_out)

    def test_point_text_is_html_escaped(self):
        rrp, _ = _import_rrp()
        rows = [_row(source_page=90, point_text="<script>alert(1)</script>")]
        html_out = rrp.build_html([90], rows, self._fake_image_paths([90]))
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)

    def test_table_columns_present(self):
        rrp, _ = _import_rrp()
        rows = [_row(source_page=90)]
        html_out = rrp.build_html([90], rows, self._fake_image_paths([90]))
        for label in ["Q#", "Part", "Point text", "Marks", "Verified",
                      "Excluded reason", "Artifact"]:
            self.assertIn(label, html_out)


# ═══════════════════════════════════════════════════════════════════════════════
class TestWriteReviewPacket(unittest.TestCase):
    """End-to-end: write_review_packet orchestrates parsing, rendering, and HTML."""

    def test_full_packet_written(self):
        import tempfile
        rrp, mock_doc = _import_rrp(num_pages=200)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "Economics_mark_scheme_review.csv"
            _write_csv(csv_path, [
                _row(source_page=90, point_text="Point on page 90"),
                _row(source_page=91, point_text="Point on page 91"),
            ])
            out_dir = tmp_path / "review_packet"

            review_path = rrp.write_review_packet(
                str(csv_path), "fake.pdf", "90-91", str(out_dir)
            )

            self.assertTrue(review_path.exists())
            self.assertEqual(review_path.name, "review.html")

            html_text = review_path.read_text(encoding="utf-8")
            self.assertIn('id="page-90"', html_text)
            self.assertIn('id="page-91"', html_text)
            self.assertIn("Point on page 90", html_text)
            self.assertIn("Point on page 91", html_text)

    def test_subject_label_derived_from_csv_filename(self):
        import tempfile
        rrp, mock_doc = _import_rrp(num_pages=200)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "Economics_mark_scheme_review.csv"
            _write_csv(csv_path, [_row(source_page=90)])
            out_dir = tmp_path / "review_packet"

            review_path = rrp.write_review_packet(
                str(csv_path), "fake.pdf", "90", str(out_dir)
            )
            html_text = review_path.read_text(encoding="utf-8")
            self.assertIn("Economics", html_text)


if __name__ == "__main__":
    unittest.main()
