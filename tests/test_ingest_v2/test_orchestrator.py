"""
tests/test_ingest_v2/test_orchestrator.py
=========================================
Integration tests for IngestOrchestrator against a small synthetic corpus spanning
all four adapters, with a temp DB and Ollama stubbed (embed_fn).
"""

import json
from pathlib import Path

import pytest

from _common import make_locked_db, make_pdf, make_docx, write_manifest, stub_embed

from backend.ingest_v2.registry import wire_adapters
from backend.ingest_v2.orchestrator import IngestOrchestrator, OrchestratorError
from backend.ingest_v2.manifest import ManifestError

TOPIC_MAP = (
    'topic_map:\n'
    '  "Basic Economic Concepts":\n'
    '    default_objective: ECON-1.1\n'
    '    subtopic_overrides:\n'
    '      "Scarcity": ECON-1.5\n'
    'unmapped_objective: REVIEW\n'
)


@pytest.fixture(autouse=True)
def _wire():
    wire_adapters()


def _build_corpus(root: Path) -> None:
    # Caribbean AI markdown -> notes (high)
    md = root / "Notes" / "Caribbean AI" / "lesson.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text("---\nsyllabus_section: 1\nsyllabus_objectives: [1, 2]\n---\n"
                  "Economics studies scarcity and the choices people make.",
                  encoding="utf-8")
    # MoE SLMS docx -> notes (high), many-to-many over objs 1,2
    make_docx(root / "Notes" / "T&T MoE SLMS" / "S1 Obj 1 2.docx",
              ["Scarcity forces choice.", "Branches: micro and macro economics."])
    # Kerwin MCQ json -> mcq_questions (+1 review)
    bank = {
        "subjectId": "eco21",
        "topics": ["Basic Economic Concepts"],
        "questions": [
            {"id": "eco21-001", "topic": "Basic Economic Concepts", "subtopic": "Scarcity",
             "stem": "What is scarcity?", "options": {"A": "Limited", "B": "Unlimited"},
             "answer": "A"},
            {"id": "eco21-002", "topic": "Basic Economic Concepts", "stem": "Economics is?",
             "options": {"A": "choices", "B": "stars"}, "answer": "A"},
            {"id": "eco21-003", "topic": "Unknown", "stem": "??",
             "options": {"A": "x", "B": "y"}, "answer": "A"},
        ],
    }
    kj = root / "Practice Questions" / "Kerwin Springer" / "eco21.json"
    kj.parent.mkdir(parents=True, exist_ok=True)
    kj.write_text(json.dumps(bank), encoding="utf-8")
    # Generic PDF past paper -> chunks
    make_pdf(root / "Past Papers" / "p2.pdf",
             ["Demand and supply determine the market equilibrium price."])
    # Skipped by pattern
    make_pdf(root / "_Review Needed" / "skip.pdf", ["should be skipped"])


def _counts(db):
    g = lambda t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return {t: g(t) for t in ("documents", "chunks", "mcq_questions",
                              "ingest_review_queue")}


def test_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "corpus"; _build_corpus(root)
    man = write_manifest(tmp_path, root, topic_map=TOPIC_MAP)
    db = make_locked_db()
    summary = IngestOrchestrator(man, db, dry_run=True, embed_fn=stub_embed).run()
    assert summary.files_processed == 4 and summary.files_seen == 4   # skip excluded
    assert _counts(db) == {"documents": 0, "chunks": 0, "mcq_questions": 0,
                           "ingest_review_queue": 0}
    db.close()


def test_real_run_writes_expected_rows(tmp_path):
    root = tmp_path / "corpus"; _build_corpus(root)
    man = write_manifest(tmp_path, root, topic_map=TOPIC_MAP)
    db = make_locked_db()
    summary = IngestOrchestrator(man, db, embed_fn=stub_embed).run()

    c = _counts(db)
    assert c["documents"] == 4                       # md, docx, json, pdf
    assert c["chunks"] > 0
    assert c["mcq_questions"] == 2                    # q1 + q2; q3 -> review
    assert c["ingest_review_queue"] >= 1             # q3 unmapped

    # every chunk carries a source_family; mcq files produce no chunks
    fams = {r[0] for r in db.execute("SELECT DISTINCT source_family FROM chunks")}
    assert fams <= {"caribbean_ai", "moe_slms", "generic_pdf"} and fams
    # mcq rows resolved correctly
    mcq = dict(db.execute("SELECT mcq_id, objective_id FROM mcq_questions").fetchall())
    assert mcq["ECON-eco21-001"] == "ECON-1.5"       # subtopic override
    assert mcq["ECON-eco21-002"] == "ECON-1.1"       # topic default
    # the unmapped MCQ went to review, never inserted
    assert "ECON-eco21-003" not in mcq
    assert summary.mcqs_inserted == 2

    # vec index populated (stub vectors) for indexed chunks
    vn = db.execute("SELECT COUNT(*) FROM vec_notes").fetchone()[0]
    assert vn > 0
    db.close()


def test_content_hash_dedup_on_second_run(tmp_path):
    root = tmp_path / "corpus"; _build_corpus(root)
    man = write_manifest(tmp_path, root, topic_map=TOPIC_MAP)
    db = make_locked_db()
    IngestOrchestrator(man, db, embed_fn=stub_embed).run()
    before = _counts(db)

    second = IngestOrchestrator(man, db, embed_fn=stub_embed).run()
    assert second.files_skipped_duplicate == 4
    assert second.chunks_indexed == 0 and second.mcqs_inserted == 0
    assert _counts(db) == before                     # no new rows
    db.close()


def test_refuses_unlocked_subject(tmp_path):
    root = tmp_path / "corpus"; _build_corpus(root)
    man = write_manifest(tmp_path, root, topic_map=TOPIC_MAP)
    db = make_locked_db(locked=False)
    with pytest.raises(OrchestratorError, match="not locked"):
        IngestOrchestrator(man, db, embed_fn=stub_embed).run()
    db.close()


def test_refuses_missing_manifest_paths(tmp_path):
    # source_root points nowhere -> manifest path validation fails loudly.
    man = write_manifest(tmp_path, tmp_path / "does_not_exist", topic_map=TOPIC_MAP)
    db = make_locked_db()
    with pytest.raises(ManifestError, match="source_root"):
        IngestOrchestrator(man, db, embed_fn=stub_embed).run()
    db.close()


def test_adapter_filter_runs_one_family(tmp_path):
    root = tmp_path / "corpus"; _build_corpus(root)
    man = write_manifest(tmp_path, root, topic_map=TOPIC_MAP)
    db = make_locked_db()
    summary = IngestOrchestrator(man, db, embed_fn=stub_embed,
                                 adapter_filter="kerwin_mcq").run()
    # only the .json is claimed; the other files find no adapter
    assert summary.mcqs_inserted == 2
    assert _counts(db)["chunks"] == 0                # no prose adapter ran
    db.close()
