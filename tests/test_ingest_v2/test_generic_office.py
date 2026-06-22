"""
tests/test_ingest_v2/test_generic_office.py
===========================================
Unit tests for GenericOfficeAdapter: matches() routing, the three filename-pattern
parsers, file-level binding on a clean filename match, keyword fallback on an
unpatterned filename, and review routing when nothing matches. No Ollama, no
orchestrator (adapters are pure); .docx files are built with make_docx.
"""

from pathlib import Path

import pytest

from _common import make_locked_db, make_docx, open_db, SCHEMA_PATH, apply_migration

from backend.ingest_v2.objective_index import ObjectiveIndex
from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.adapters.generic_office import (
    GenericOfficeAdapter, parse_office_filename,
)


@pytest.fixture
def oindex():
    db = make_locked_db()           # Economics, prefix ECON, OBJECTIVES incl. ECON-1.5 / ECON-3.9
    yield ObjectiveIndex(db, "Economics")
    db.close()


def _manifest(subject: str = "Economics") -> SubjectManifest:
    return SubjectManifest(
        subject_id=subject, display_name=subject, source_root=".",
        syllabus_csv=".", mcq_topic_map=".",
    )


def _isci_locked_db():
    """An Integrated_Science DB whose ONE locked objective is 'INTSCI-10.1' -- the
    framework prefix (subject_prefix.prefix_for('Integrated_Science') == 'INTSCI'),
    DELIBERATELY different from the 'ISCI' prefix the real Bridge filenames use. Used
    to prove the prefix-agnostic rebuild-and-validate path on the exact mismatch found
    during this build, not merely to assert it in a docstring."""
    db = open_db()
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    db.commit()
    apply_migration(db, "m018_mcq_questions")
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?,?,?)",
        ("Integrated_Science", "Integrated Science", 1),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?,?,?,?)", ("INTSCI-S10", "Integrated_Science", "Cells", "10"),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt) VALUES (?,?,?,?,?)",
        ("INTSCI-10.1", "INTSCI-S10", "Integrated_Science", "10.1",
         "Describe the cell as the basic structural and functional unit of life"),
    )
    db.commit()
    return db


# ---------------------------------------------------------------------------
# matches() routing
# ---------------------------------------------------------------------------
def test_matches_office_formats_only():
    a = GenericOfficeAdapter()
    assert a.matches(Path("x/Notes/Bridge/POB-1.2 Bridge Lesson - X.docx"))
    assert a.matches(Path("x/y/deck.pptx"))
    assert a.matches(Path("x/y/deck.pptm"))
    assert not a.matches(Path("x/y/paper.pdf"))      # GenericPDFAdapter's job
    assert not a.matches(Path("x/Notes/Caribbean AI/l.md"))


# ---------------------------------------------------------------------------
# parse_office_filename -- the three real conventions
# ---------------------------------------------------------------------------
def test_parse_objective_id_form():
    # Bridge-lesson naming across subjects -- prefix-agnostic; section.objective only.
    assert parse_office_filename("POB-1.2 Bridge Lesson - THE NATURE OF BUSINESS") == (1, [2], "high")
    assert parse_office_filename("ISCI-10.1 Bridge Lesson - UNITS OF LIFE") == (10, [1], "high")
    assert parse_office_filename("ECON-3.9 Bridge Lesson - Elasticity") == (3, [9], "high")


def test_parse_underscore_form():
    assert parse_office_filename("S08_Obj3_Caribbean Economies - Bridge Lesson") == (8, [3], "high")
    assert parse_office_filename("S06_Obj8_Nominal Real and Potential Output") == (6, [8], "high")
    # range form (safety): S2_Obj4-7
    assert parse_office_filename("S2_Obj4-7 Something") == (2, [4, 5, 6, 7], "high")


def test_parse_moe_space_form():
    # Reuses parse_moe_filename; the real MoE naming with a leading subject label.
    assert parse_office_filename("CSEC Economics S3 Obj 9 Elasticity") == (3, [9], "high")
    assert parse_office_filename("S1 Obj 1-7 Nature of Economics") == (1, [1, 2, 3, 4, 5, 6, 7], "high")


def test_parse_no_pattern_returns_none():
    assert parse_office_filename("lecture-13-and-14") is None
    assert parse_office_filename("lecture-15161718-marketing-13") is None
    assert parse_office_filename("Some Random Notes") is None


# ---------------------------------------------------------------------------
# extract() -- clean filename match binds every chunk (file-level, high)
# ---------------------------------------------------------------------------
def test_extract_filename_match_binds_high(tmp_path, oindex):
    path = tmp_path / "ECON-3.9 Bridge Lesson - Elasticity.docx"
    make_docx(path, ["Price elasticity of demand measures how responsive quantity is to price.",
                     "Elastic demand has a value greater than one."])
    recs = list(GenericOfficeAdapter().extract(path, _manifest(), oindex))
    assert recs, "expected chunk records"
    assert all(r.objective_id == "ECON-3.9" for r in recs)
    assert all(r.confidence == "high" for r in recs)
    assert all(r.source_family == "generic_office" for r in recs)


def test_extract_prefix_mismatch_isci_resolves_against_intsci(tmp_path):
    """The exact mismatch found during this build: a Bridge file named with prefix
    'ISCI' must resolve against a syllabus whose objective uses prefix 'INTSCI', SAME
    (section, objective). The adapter must DISCARD the filename prefix, rebuild the id
    with the subject's own prefix, validate INTSCI-10.1 against the locked syllabus,
    and bind every chunk at high confidence -- prefix-agnostic, proven not asserted."""
    db = _isci_locked_db()
    try:
        oindex = ObjectiveIndex(db, "Integrated_Science")
        # The syllabus genuinely uses the OTHER prefix -- the mismatch is real.
        assert "INTSCI-10.1" in oindex.all_objective_ids()
        assert "ISCI-10.1" not in oindex.all_objective_ids()
        # And the filename genuinely carries the 'wrong' prefix.
        assert parse_office_filename("ISCI-10.1 Bridge Lesson - UNITS OF LIFE") == (10, [1], "high")

        path = tmp_path / "ISCI-10.1 Bridge Lesson - UNITS OF LIFE.docx"
        make_docx(path, ["The cell is the basic structural and functional unit of all "
                         "living organisms; tissues are groups of similar cells."])
        recs = list(GenericOfficeAdapter().extract(
            path, _manifest("Integrated_Science"), oindex))
        assert recs, "expected chunk records"
        assert all(r.objective_id == "INTSCI-10.1" for r in recs), \
            "filename 'ISCI-10.1' must rebind to the syllabus's 'INTSCI-10.1'"
        assert all(r.confidence == "high" for r in recs)
        assert all(r.review_reason is None for r in recs)
        assert all(r.source_family == "generic_office" for r in recs)
    finally:
        db.close()


def test_extract_filename_match_unknown_objective_routes_review(tmp_path, oindex):
    # S9 Obj 99 is a clean filename parse but not in the locked syllabus -> review.
    path = tmp_path / "S9_Obj99_Imaginary Topic - Bridge Lesson.docx"
    make_docx(path, ["Body text."])
    recs = list(GenericOfficeAdapter().extract(path, _manifest(), oindex))
    assert len(recs) == 1
    assert recs[0].confidence == "review"
    assert recs[0].review_reason == "objective_id_not_in_syllabus"
    assert recs[0].objective_id == "ECON-9.99"   # what it tried to map to


# ---------------------------------------------------------------------------
# extract() -- no filename pattern -> per-chunk keyword fallback
# ---------------------------------------------------------------------------
def test_extract_keyword_fallback_matches_medium(tmp_path, oindex):
    path = tmp_path / "lecture-1.docx"   # no parseable pattern
    make_docx(path, ["Scarcity is the basic economic problem: resources are scarce while wants "
                     "are unlimited. Opportunity cost is the next best choice forgone when a "
                     "scarce resource is used for one purpose instead of another choice."])
    recs = list(GenericOfficeAdapter().extract(path, _manifest(), oindex))
    assert recs, "expected at least one chunk record"
    matched = [r for r in recs if r.confidence == "medium"]
    assert matched, "keyword fallback should have matched an objective at medium confidence"
    assert all(r.objective_id in oindex.all_objective_ids() for r in matched)


def test_extract_no_match_routes_review(tmp_path, oindex):
    path = tmp_path / "random-handout.docx"   # no pattern + no keyword overlap
    make_docx(path, ["Zzqq xkcd flooble wibble plover frobnicate quux baz."])
    recs = list(GenericOfficeAdapter().extract(path, _manifest(), oindex))
    assert recs, "unparseable content must still produce a record, not be skipped"
    assert all(r.confidence == "review" for r in recs)
    assert all(r.review_reason == "no_objective_match_via_keywords" for r in recs)
    assert all(r.objective_id == "REVIEW" for r in recs)
