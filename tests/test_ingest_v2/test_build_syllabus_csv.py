"""
tests/test_ingest_v2/test_build_syllabus_csv.py
===============================================
Unit tests for the syllabus master-map -> syllabus_parser CSV converter. All
fixtures are synthetic temp files -- the real Economics master map is never read
here. No DB, no Ollama, no cloud.
"""

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from backend.ingest_v2.subject_prefix import prefix_for  # noqa: E402
from backend.ingest_v2.tools import build_syllabus_csv as bsc  # noqa: E402

# The converter's CSV cell for command_words round-trips through the SAME function
# syllabus_parser uses, so we assert the eventual live-DB JSON format, not a guess.
from backend.db.syllabus_parser import (  # noqa: E402
    REQUIRED_COLUMNS,
    command_words_to_json,
)

MASTER_MAP_COLUMNS = [
    "subject", "context", "objective_number", "page", "objective",
    "top_confidence", "top_score", "top_resource", "top_resource_path",
]


def write_master_map(tmp_path: Path, rows: list[dict], name: str = "master.csv") -> Path:
    """Write a synthetic master map with the real 9-column header."""
    path = tmp_path / name
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MASTER_MAP_COLUMNS)
        writer.writeheader()
        for r in rows:
            full = {c: "" for c in MASTER_MAP_COLUMNS}
            full.update(r)
            writer.writerow(full)
    return path


def mm_row(subject, context, objective_number, objective) -> dict:
    return {
        "subject": subject,
        "context": context,
        "objective_number": str(objective_number),
        "objective": objective,
        "page": "1",
        "top_confidence": "high",
        "top_score": "10",
        "top_resource": "x",
        "top_resource_path": "x",
    }


# --- realistic fixture shared by several tests ------------------------------

S1 = "SECTION 1: THE NATURE OF ECONOMICS"
S2_TRUNC = "SECTION 2: PRODUCTION, ECONOMIC RESOURCES AND RESOURCE"
S2_FULL = "SECTION 2: PRODUCTION, ECONOMIC RESOURCES AND RESOURCE ALLOCATION"
S3 = "SECTION 3: MONEY AND FINANCIAL INSTITUTIONS"


def good_records() -> list[dict]:
    return [
        # A "General" methodology note -- must be excluded even though it has a verb.
        mm_row("Economics", "General", 3,
               "Use a variety of methodologies such as role plays and case studies."),
        mm_row("Economics", S1, 1, 'define the term "economics";'),
        mm_row("Economics", S1, 2, "explain the branches of economics;"),
        # Section 2 appears truncated first, full later -> canonicalize to full.
        mm_row("Economics", S2_TRUNC, 1, "describe the factors of production;"),
        mm_row("Economics", S2_FULL, 2, "calculate and explain the costs of production;"),
        # No seed verb at all -> UNCLASSIFIED, empty command_words.
        mm_row("Economics", S3, 1, "money and its main characteristics;"),
        # A different subject in the same map -- must be ignored.
        mm_row("Mathematics", "SECTION 1: NUMBER", 1, "compute simple interest;"),
    ]


def build_good():
    return bsc.build_syllabus_rows(good_records(), "Economics")


def test_canonicalization_picks_longest_title():
    rows, report = build_good()
    sec2 = [r for r in rows if r["section_num"] == "2"]
    assert sec2, "expected section 2 rows"
    for r in sec2:
        assert r["section_title"] == "PRODUCTION, ECONOMIC RESOURCES AND RESOURCE ALLOCATION"
    # The disagreement is reported, not silent.
    assert "2" in report["title_disagreements"]
    assert report["title_disagreements"]["2"]["chosen"].endswith("ALLOCATION")


def test_general_rows_excluded_not_converted():
    rows, report = build_good()
    assert report["excluded_count"] == 1
    assert report["excluded"][0][0] == "General"
    # No output row carries the methodology text.
    assert all("methodologies" not in r["content_stmt"].lower() for r in rows)


def test_other_subjects_ignored():
    rows, _ = build_good()
    assert all(r["objective_id"].startswith("ECON-") for r in rows)
    assert not any("compute simple interest" in r["content_stmt"].lower() for r in rows)


def test_duplicate_pair_raises_with_clear_message():
    records = [
        mm_row("Economics", S1, 1, "define economics;"),
        mm_row("Economics", S1, 1, "a second objective sharing 1.1;"),
    ]
    with pytest.raises(ValueError) as exc:
        bsc.build_syllabus_rows(records, "Economics")
    msg = str(exc.value)
    assert "duplicate" in msg.lower()
    assert "ECON-1.1" in msg  # names the offending objective_id


def test_ids_match_subject_prefix():
    rows, _ = build_good()
    prefix = prefix_for("Economics")
    assert prefix == "ECON"
    by_content = {r["content_stmt"]: r for r in rows}
    define = by_content['Define the term "economics"']
    assert define["objective_id"] == f"{prefix}-1.1"
    assert define["section_id"] == f"{prefix}-S1"


def test_content_stmt_cleaned_like_live_db():
    rows, _ = build_good()
    contents = {r["content_stmt"] for r in rows}
    # First letter capitalized, trailing ';' stripped, embedded quotes preserved.
    assert 'Define the term "economics"' in contents
    assert all(not c.endswith((";", ".")) for c in contents)
    assert all(c == "" or c[0].isupper() for c in contents)


def test_command_words_roundtrip_to_live_db_json():
    rows, _ = build_good()
    by_content = {r["content_stmt"]: r for r in rows}

    # Single verb -> '["Define"]' exactly as stored in the live DB.
    define_cell = by_content['Define the term "economics"']["command_words"]
    assert define_cell == "Define"
    assert command_words_to_json(define_cell) == '["Define"]'

    # Multiple verbs preserve order of first appearance and round-trip to a JSON array.
    multi = by_content["Calculate and explain the costs of production"]
    assert multi["command_words"] == "Calculate|Explain"
    assert command_words_to_json(multi["command_words"]) == '["Calculate", "Explain"]'


def test_no_seed_verb_is_unclassified_not_defaulted():
    rows, report = build_good()
    money = next(r for r in rows if r["content_stmt"].startswith("Money"))
    assert money["skill_type"] == "UNCLASSIFIED"
    assert money["command_words"] == ""
    assert report["unclassified_count"] == 1


def test_skill_type_precedence_application_beats_understanding():
    # "calculate and explain" -> Application (calculate) outranks Understanding (explain).
    rows, _ = build_good()
    multi = next(r for r in rows
                 if r["content_stmt"] == "Calculate and explain the costs of production")
    assert multi["skill_type"] == "Application"


def test_exam_weight_defaults_to_tbd_placeholder():
    rows, report = build_good()
    assert rows and all(r["exam_weight"] == "TBD" for r in rows)
    assert report["exam_weight"] == "TBD"


def test_exam_weight_override_applied_verbatim():
    rows, report = bsc.build_syllabus_rows(good_records(), "Economics", exam_weight="Both")
    assert rows and all(r["exam_weight"] == "Both" for r in rows)
    assert report["exam_weight"] == "Both"


def test_output_header_matches_syllabus_parser_expectation(tmp_path):
    rows, _ = build_good()
    out = bsc.write_csv(rows, tmp_path / "economics.csv")
    with open(out, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh))
    assert header == bsc.OUTPUT_COLUMNS
    # Every column syllabus_parser REQUIRES must be present.
    assert REQUIRED_COLUMNS.issubset(set(header))


def test_unknown_subject_raises():
    with pytest.raises(ValueError):
        bsc.build_syllabus_rows([], "NotASubject")


# --- supplement (human-confirmed missing objectives) ------------------------

def supplement_records() -> list[dict]:
    return [{
        "subject": "Economics",
        "context": f"{S2_FULL}",          # already SECTION-shaped
        "objective_number": "7",
        "objective": "illustrate the production cost curves;",
    }]


def test_supplement_row_merged_and_derived_identically():
    rows, report = bsc.build_syllabus_rows(
        good_records(), "Economics", supplement_records=supplement_records())
    by_id = {r["objective_id"]: r for r in rows}
    assert "ECON-2.7" in by_id            # was a gap; now present
    supp = by_id["ECON-2.7"]
    # Same derivation as every other row: cleaned content, derived verb + skill.
    assert supp["content_stmt"] == "Illustrate the production cost curves"
    assert supp["command_words"] == "Illustrate"
    assert supp["skill_type"] == "Understanding"
    assert supp["exam_weight"] == "TBD"
    assert report["supplement_count"] == 1
    assert report["supplement_ids"] == ["ECON-2.7"]


def test_supplement_row_sorted_into_numeric_position():
    rows, _ = bsc.build_syllabus_rows(
        good_records(), "Economics", supplement_records=supplement_records())
    sec2_nums = [int(r["objective_num"]) for r in rows if r["section_num"] == "2"]
    assert sec2_nums == sorted(sec2_nums)  # 1,2,7 -> in order, not appended last


def test_supplement_duplicate_raises():
    # Supplement re-adds a number already present in the master map -> loud failure.
    dup_supp = [{
        "subject": "Economics",
        "context": S1,
        "objective_number": "1",
        "objective": "duplicate of 1.1;",
    }]
    with pytest.raises(ValueError) as exc:
        bsc.build_syllabus_rows(good_records(), "Economics", supplement_records=dup_supp)
    assert "ECON-1.1" in str(exc.value)


def test_load_supplement_parses_small_shape(tmp_path):
    path = tmp_path / "supp.csv"
    path.write_text(
        "subject,section_num,section_title,objective_number,objective\n"
        'Economics,7,INTERNATIONAL TRADE,4,Calculate balance of payments surpluses and deficits\n'
        ",,,,\n",  # blank/spacer row -> skipped
        encoding="utf-8",
    )
    recs = bsc.load_supplement(path)
    assert len(recs) == 1
    assert recs[0]["context"] == "SECTION 7: INTERNATIONAL TRADE"
    assert recs[0]["objective_number"] == "4"
