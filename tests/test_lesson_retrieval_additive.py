"""
tests/test_lesson_retrieval_additive.py
=======================================
Guards the additive mark-scheme/past-paper retrieval in
backend/ingest_lessons.candidate_chunks (the lesson-grounding path).

Before: mark_scheme/past_paper chunks were pulled ONLY when notes < 2. A
noisy/heading-only notes top-k (e.g. INTSCI-3.3.7 "Determine the conditions for
flotation", whose vec_notes top-15 was all syllabus headings + duplicate
source-card URL fragments) therefore never saw the real teaching content that
lived in answer keys / past papers.

After: the mark_scheme + past_paper pull is ADDITIVE -- it runs for every
objective, appended AFTER the notes (notes stay primary). Covered:

  (a) Strong-notes objective: notes are present and PRIMARY (first + not
      out-numbered) -- no composition regression.
  (b) Noisy/heading-only-notes objective (>= 2 notes, so the OLD gate would
      have skipped them): mark_scheme AND past_paper candidates now appear in
      the retrieval set.

All offline: embeddings are faked, DB is in-memory SQLite + sqlite-vec.
Run: pytest tests/test_lesson_retrieval_additive.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.ingest_lessons as il  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768
SUBJECT = "Integrated_Science"
OBJECTIVE = "INTSCI-3.3.7"
DOC_NOTES = "notes-doc-1"
DOC_MS = "ms-doc-1"
DOC_PP = "pp-doc-1"


def fake_embed(text: str) -> list[float]:
    """Deterministic dummy embedding -- no Ollama required."""
    return [0.0] * EMBED_DIM


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping retrieval tests")
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    return db


def _add_chunk(db, doc_id, text, chunk_id, vec_table):
    cur = db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, "
        "question_num, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, OBJECTIVE, SUBJECT, text, 1, None, chunk_id),
    )
    db.execute(
        f"INSERT INTO {vec_table}(rowid, embedding) VALUES (?, ?)",
        (cur.lastrowid, il.serialize_vec(fake_embed("x"))),
    )
    return cur.lastrowid


def _seed_doc(db, doc_id, content_type, hash_):
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, "
        "content_hash) VALUES (?, ?, ?, ?, ?)",
        (doc_id, SUBJECT, content_type, rf"E:\KB\{doc_id}.pdf", hash_),
    )


def seed(db, *, notes_chunks, ms_chunks, pp_chunks):
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Integrated Science"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("ISC-SEC-1", SUBJECT, "Water and the Aquatic Environment", "3.3"),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type, command_words) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (OBJECTIVE, "ISC-SEC-1", SUBJECT, "7",
         "Determine the conditions for flotation", "Application", '["Determine"]'),
    )
    if notes_chunks:
        _seed_doc(db, DOC_NOTES, "notes", "hash-notes")
        for i in range(notes_chunks):
            _add_chunk(db, DOC_NOTES, f"notes teaching text {i}", f"notes-c{i}", "vec_notes")
    if ms_chunks:
        _seed_doc(db, DOC_MS, "mark_scheme", "hash-ms")
        for i in range(ms_chunks):
            _add_chunk(db, DOC_MS,
                       "The principle of flotation: weight of a floating body equals "
                       f"the upthrust on it. {i}", f"ms-c{i}", "vec_mark_schemes")
    if pp_chunks:
        _seed_doc(db, DOC_PP, "past_paper", "hash-pp")
        for i in range(pp_chunks):
            _add_chunk(db, DOC_PP,
                       f"State Archimedes' principle and verify it. {i}",
                       f"pp-c{i}", "vec_past_papers")
    db.commit()


def _objective(db):
    return dict(db.execute(
        "SELECT * FROM objectives WHERE objective_id=?", (OBJECTIVE,)).fetchone())


def _doc_set(chunks):
    return {c["doc_id"] for c in chunks}


def test_strong_notes_objective_keeps_notes_primary():
    """(a) An objective with strong notes coverage keeps notes PRIMARY: notes are
    present, appear first, and are not out-numbered by the additive MS/PP pull."""
    db = open_test_db()
    seed(db, notes_chunks=12, ms_chunks=4, pp_chunks=4)
    chunks = il.candidate_chunks(db, SUBJECT, _objective(db), embed_fn=fake_embed)

    notes = [c for c in chunks if c["vec_table"] == il.NOTES_TABLE]
    non_notes = [c for c in chunks if c["vec_table"] != il.NOTES_TABLE]
    assert len(notes) == 12, "all notes retrieved (NOTES_K=15 >= 12)"
    # notes come FIRST (additive MS/PP are appended after)
    assert all(c["vec_table"] == il.NOTES_TABLE for c in chunks[:12])
    # strong notes coverage stays primary -- it out-numbers the bounded additive pull
    assert len(notes) > len(non_notes)
    db.close()


def test_noisy_notes_objective_now_gets_markscheme_and_pastpaper():
    """(b) An objective WITH >= 2 notes (the OLD gate would skip MS/PP) now also
    receives mark_scheme AND past_paper candidates -- the additive fix that
    unblocks noisy/heading-only-notes objectives like INTSCI-3.3.7."""
    db = open_test_db()
    # 15 'notes' chunks stands in for the heading/source-card noise that filled
    # 3.3.7's vec_notes top-k; well over MIN_NOTES_CHUNKS, so the old code's
    # `if len(chunks) < MIN_NOTES_CHUNKS` gate would NEVER have pulled MS/PP.
    seed(db, notes_chunks=15, ms_chunks=5, pp_chunks=5)
    assert il.MIN_NOTES_CHUNKS == 2  # the old gate threshold
    chunks = il.candidate_chunks(db, SUBJECT, _objective(db), embed_fn=fake_embed)
    docs = _doc_set(chunks)
    assert DOC_NOTES in docs, "notes still present"
    assert DOC_MS in docs, "mark_scheme candidates now included (was skipped when notes>=2)"
    assert DOC_PP in docs, "past_paper candidates now included (was skipped when notes>=2)"
    # bounded: additive pull capped at ADDITIVE_K per table
    ms = [c for c in chunks if c["vec_table"] == il.MARK_SCHEMES_TABLE]
    pp = [c for c in chunks if c["vec_table"] == il.PAST_PAPERS_TABLE]
    assert len(ms) <= il.ADDITIVE_K and len(pp) <= il.ADDITIVE_K
    db.close()
