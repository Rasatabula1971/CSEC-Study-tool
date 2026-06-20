"""
tests/test_ingest_v2/test_adapters.py
=====================================
Unit tests for the four ingest_v2 adapters: matches() routing, and extract()
behaviour with synthetic inputs. No Ollama, no orchestrator (adapters are pure).
"""

from pathlib import Path

import pytest

from _common import make_locked_db, make_pdf, make_docx, OBJECTIVES

from backend.ingest_v2.objective_index import ObjectiveIndex
from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.normalize import _moji
from backend.ingest_v2.adapters.caribbean_ai import CaribbeanAIAdapter
from backend.ingest_v2.adapters.moe_slms import MoESLMSAdapter, parse_moe_filename
from backend.ingest_v2.adapters.kerwin_mcq import KerwinMCQAdapter
from backend.ingest_v2.adapters.generic_pdf import GenericPDFAdapter

import ingest as v1  # noqa: E402


@pytest.fixture
def oindex():
    db = make_locked_db()
    yield ObjectiveIndex(db, "Economics")
    db.close()


def _manifest(mcq_path: Path | None = None) -> SubjectManifest:
    return SubjectManifest(
        subject_id="Economics", display_name="Economics", source_root=".",
        syllabus_csv=".", mcq_topic_map=str(mcq_path) if mcq_path else ".",
    )


# ---------------------------------------------------------------------------
# matches() routing
# ---------------------------------------------------------------------------
def test_matches_routing():
    cai, moe, kmc, gpdf = (CaribbeanAIAdapter(), MoESLMSAdapter(),
                           KerwinMCQAdapter(), GenericPDFAdapter())
    P = Path
    assert cai.matches(P(r"x/Notes/Caribbean AI/l.md"))
    assert not cai.matches(P(r"x/Notes/T&T MoE SLMS/l.md"))
    assert moe.matches(P(r"x/Notes/T&T MoE SLMS/S1 Obj 1-7.docx"))
    # Scoped out: same vendor folder under Practice Questions / SBA is NOT MoE's.
    assert not moe.matches(P(r"x/Practice Questions/T&T MoE SLMS/f.docx"))
    assert not moe.matches(P(r"x/SBA/T&T MoE SLMS/f.pdf"))
    assert kmc.matches(P(r"x/Practice Questions/Kerwin Springer/b.json"))
    assert not kmc.matches(P(r"x/Practice Questions/Kerwin Springer/b.pdf"))
    assert gpdf.matches(P(r"x/Past Papers/p.pdf"))
    assert not gpdf.matches(P(r"x/Notes/Caribbean AI/l.md"))


# ---------------------------------------------------------------------------
# CaribbeanAIAdapter
# ---------------------------------------------------------------------------
def test_caribbean_valid_frontmatter(tmp_path, oindex):
    md = tmp_path / "Caribbean AI" / "lesson.md"
    md.parent.mkdir(parents=True)
    body = ("Economics studies choices.\n\n"
            "<wiki>Scarcity</wiki> means limited resources" + _moji("—") + "always.")
    md.write_text("---\nsyllabus_section: 1\nsyllabus_objectives: [1, 2]\n---\n" + body,
                  encoding="utf-8")
    recs = list(CaribbeanAIAdapter().extract(md, _manifest(), oindex))
    assert recs and all(r.confidence == "high" for r in recs)
    assert {r.objective_id for r in recs} == {"ECON-1.1", "ECON-1.2"}
    assert all(r.content_type == "notes" and r.source_family == "caribbean_ai" for r in recs)
    text = recs[0].chunk_text
    assert "<wiki>" not in text and "Scarcity" in text       # tag unwrapped, inner kept
    assert _moji("—") not in text and "—" in text       # mojibake repaired


def test_caribbean_missing_frontmatter_is_review(tmp_path, oindex):
    md = tmp_path / "Caribbean AI" / "bad.md"
    md.parent.mkdir(parents=True)
    md.write_text("Just a body, no front-matter.", encoding="utf-8")
    recs = list(CaribbeanAIAdapter().extract(md, _manifest(), oindex))
    assert len(recs) == 1
    assert recs[0].confidence == "review"
    assert recs[0].review_reason == "caribbean_ai_missing_frontmatter"


def test_caribbean_unknown_objective_is_review(tmp_path, oindex):
    md = tmp_path / "Caribbean AI" / "l.md"
    md.parent.mkdir(parents=True)
    md.write_text("---\nsyllabus_section: 1\nsyllabus_objectives: [99]\n---\nbody",
                  encoding="utf-8")
    recs = list(CaribbeanAIAdapter().extract(md, _manifest(), oindex))
    assert [r.review_reason for r in recs] == ["objective_id_not_in_syllabus"]
    assert recs[0].objective_id == "ECON-1.99"


# ---------------------------------------------------------------------------
# MoESLMSAdapter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stem,section,objs", [
    ("S1 Obj 1-7", 1, [1, 2, 3, 4, 5, 6, 7]),
    ("S2 Obj 1 2", 2, [1, 2]),
    ("S2 Obj 4-7", 2, [4, 5, 6, 7]),
    ("S2 Obj 10", 2, [10]),
])
def test_moe_filename_patterns(stem, section, objs):
    sec, parsed, conf = parse_moe_filename(stem)
    assert (sec, parsed, conf) == (section, objs, "high")


def test_moe_unparsable_filename_is_review(tmp_path, oindex):
    p = tmp_path / "Notes" / "T&T MoE SLMS" / "random handout.docx"
    make_docx(p, ["content"])
    recs = list(MoESLMSAdapter().extract(p, _manifest(), oindex))
    assert len(recs) == 1 and recs[0].review_reason == "moe_slms_filename_unparsable"


def test_moe_docx_extract_binds_all_objectives(tmp_path, oindex):
    p = tmp_path / "Notes" / "T&T MoE SLMS" / "S1 Obj 1 2.docx"
    make_docx(p, ["Economics is about scarcity and choice.",
                  "Microeconomics studies individual markets."])
    recs = [r for r in MoESLMSAdapter().extract(p, _manifest(), oindex)
            if not r.needs_review]
    assert recs and {r.objective_id for r in recs} == {"ECON-1.1", "ECON-1.2"}
    assert all(r.confidence == "high" and r.content_type == "notes" for r in recs)


# ---------------------------------------------------------------------------
# KerwinMCQAdapter
# ---------------------------------------------------------------------------
TOPIC_MAP = (
    'topic_map:\n'
    '  "Basic Economic Concepts":\n'
    '    default_objective: ECON-1.1\n'
    '    subtopic_overrides:\n'
    '      "Scarcity": ECON-1.5\n'
    'unmapped_objective: REVIEW\n'
)


def _kerwin_bank(path: Path):
    """Mirrors the real Kerwin schema: question 'id', dict 'options', letter 'answer'."""
    import json
    bank = {
        "subjectId": "eco21",
        "topics": ["Basic Economic Concepts"],
        "questions": [
            {"id": "eco21-001", "topic": "Basic Economic Concepts", "subtopic": "Scarcity",
             "difficulty": "core", "stem": "What is scarcity?",
             "options": {"A": "Limited", "B": "Unlimited", "C": "Free", "D": "None"},
             "answer": "A", "explanation": "limited resources"},
            {"id": "eco21-002", "topic": "Basic Economic Concepts",
             "stem": "Economics studies?", "options": {"A": "choices", "B": "stars"},
             "answer": "A"},
            {"id": "eco21-003", "topic": "Totally Unknown Topic",
             "stem": "Huh?", "options": {"A": "x", "B": "y"}, "answer": "A"},
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bank), encoding="utf-8")


def test_kerwin_resolution_tiers(tmp_path, oindex):
    mcq_map = tmp_path / "map.yaml"
    mcq_map.write_text(TOPIC_MAP, encoding="utf-8")
    bank = tmp_path / "Practice Questions" / "Kerwin Springer" / "eco21.json"
    _kerwin_bank(bank)

    recs = list(KerwinMCQAdapter().extract(bank, _manifest(mcq_map), oindex))
    assert len(recs) == 3
    assert all(r.is_mcq and r.content_type == "mcq" for r in recs)
    # subtopic override -> high -> ECON-1.5
    assert (recs[0].objective_id, recs[0].confidence) == ("ECON-1.5", "high")
    # topic default -> medium -> ECON-1.1
    assert (recs[1].objective_id, recs[1].confidence) == ("ECON-1.1", "medium")
    # unmapped topic -> review (terminal sentinel)
    assert recs[2].confidence == "review" and recs[2].objective_id == "REVIEW"
    assert recs[2].review_reason == "mcq_unmapped_topic"
    # mcq_payload shape: mcq_id = {PREFIX}-{question.id}, letter answer, dict options
    p = recs[0].mcq_payload
    assert p["mcq_id"] == "ECON-eco21-001" and p["correct_option"] == "A"
    assert p["options"] == {"A": "Limited", "B": "Unlimited", "C": "Free", "D": "None"}
    assert p["source"] == "kerwin_springer" and p["difficulty"] == "core"


# ---------------------------------------------------------------------------
# GenericPDFAdapter -- byte-equivalence to v1
# ---------------------------------------------------------------------------
def test_generic_pdf_matches_v1(tmp_path, oindex):
    pdf = tmp_path / "02_PAST_PAPERS" / "paper.pdf"
    make_pdf(pdf, ["Demand and supply determine the market equilibrium price in a market.",
                   "Opportunity cost reflects scarcity and the need to choose."])
    objectives = [{"objective_id": o, "content_stmt": s} for o, _, s in
                  [(a, b, c) for a, b, c in OBJECTIVES]]

    # v1 expectation: replicate v1.ingest_page's per-chunk matching over the same PDF.
    expected = []
    for page, text in v1.extract_pdf_pages(pdf):
        for seq, raw in enumerate(v1.chunk_page(text)):
            c = raw.strip()
            if not c:
                continue
            oid, _ = v1.best_objective(c, objectives)
            expected.append((page, seq, c, oid))

    recs = list(GenericPDFAdapter().extract(pdf, _manifest(), oindex))
    got = [(r.page, r.chunk_seq, r.chunk_text,
            None if r.needs_review else r.objective_id) for r in recs]
    assert got == expected
    assert all(r.content_type == "past_paper" and r.source_family == "generic_pdf"
               for r in recs)
