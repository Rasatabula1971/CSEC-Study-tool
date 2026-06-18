"""
tests/test_upload_ingest.py
===========================
Upload session 4 -- unit tests for backend/upload_ingest.py.

Real in-memory SQLite (schema.sql + apply_runtime_migrations + sqlite-vec, FKs ON)
so the m016 columns, the documents FK, and the ingestion_log writes are genuinely
exercised. The heavy ingest.ingest_document is replaced with a fake that records its
call and inserts a documents row (keeping the ingested_doc_id FK valid) -- no Ollama,
no PyMuPDF. KB_ROOT points at a per-test tempdir.

  1. ingest_staged_file moves the file and records the documents row.
  2. overridden decision uses review_folder over recommended_folder.
  3. rejected classification raises ValueError (no file moved, no DB change).
  4. already-ingested file raises ValueError on a second call.
  5. failed ingestion leaves the file in 06_UPLOAD_STAGING.
  6. successful ingestion stales matching lessons.
  7. the preferred_objectives hint reaches the ingester.
  8. a destination collision appends an _N suffix.

Run: pytest tests/test_upload_ingest.py -v
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module        # noqa: E402
import ingest                   # noqa: E402
import upload_ingest            # noqa: E402

SUBJECT = "Principles_of_Business"


# --- fixtures --------------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping upload-ingest tests")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    app_module.apply_runtime_migrations(db)
    return db


def seed(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    for oid, num in (("POB-1.2", "1.2"), ("POB-5.6", "5.6")):
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt, command_words) VALUES (?, 'SEC-1', ?, ?, ?, '[\"Discuss\"]')",
            (oid, SUBJECT, num, f"Content for {oid}"),
        )
    db.commit()


@pytest.fixture
def env(tmp_path, monkeypatch):
    kb = tmp_path / "kb"
    staging = tmp_path / "staging" / SUBJECT
    staging.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KB_ROOT", str(kb))
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    return {"kb": kb, "staging": staging, "tmp": tmp_path}


@pytest.fixture
def db(env):
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


def stage_and_classify(db, env, name="paper.pdf", *, decision="accepted",
                       recommended_folder="02_PAST_PAPERS", review_folder=None,
                       objectives_json="[]", review_objectives_json=None,
                       extracted_text="A business document about supply.",
                       file_type="pdf"):
    """Insert a staged file (with a real file on disk) + its classification row."""
    db.execute(
        """
        INSERT INTO upload_staging
            (subject_id, original_name, stored_path, file_type, file_size_bytes,
             extracted_text, extract_status, status, truncated, ingestion_status)
        VALUES (?, ?, '', ?, 100, ?, 'ready', 'staged', 0, 'not_started')
        """,
        (SUBJECT, name, file_type, extracted_text),
    )
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    src = env["staging"] / f"{sid}_{name}"
    src.write_bytes(b"%PDF-1.4 fake content " + str(sid).encode())
    db.execute("UPDATE upload_staging SET stored_path = ? WHERE staging_id = ?",
               (str(src), sid))
    db.execute(
        """
        INSERT INTO upload_classifications
            (staging_id, recommended_folder, folder_confidence, objectives_json,
             rationale, model_used, review_decision, review_folder, review_objectives_json)
        VALUES (?, ?, 90, ?, 'r', 'gemini', ?, ?, ?)
        """,
        (sid, recommended_folder, objectives_json, decision, review_folder,
         review_objectives_json),
    )
    db.commit()
    return sid, src


def make_fake_ingest(doc_id="doc-x", chunks=5, objectives_hit=None, raises=False):
    """A stand-in for ingest.ingest_document: records its kwargs, inserts a documents
    row (so the ingested_doc_id FK holds), and returns the standard result dict."""
    rec = {}

    def fake(db, *, path, subject_id, content_type, objectives, embed_fn=None,
             preferred_objectives=None, full_text=None, source_file=None, min_overlap=2):
        rec["called"] = True
        rec["preferred_objectives"] = preferred_objectives
        rec["content_type"] = content_type
        rec["path"] = str(path)
        if raises:
            raise RuntimeError("simulated ingest failure")
        db.execute(
            "INSERT INTO documents (doc_id, subject_id, content_type, source_file, "
            "content_hash) VALUES (?, ?, ?, ?, ?)",
            (doc_id, subject_id, content_type, str(path), "hash-" + doc_id),
        )
        return {"doc_id": doc_id, "chunks_created": chunks,
                "objectives_hit": objectives_hit or [], "skipped_duplicate": False}

    return fake, rec


def seed_lesson(db, objective_id="POB-1.2"):
    db.execute(
        "INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
        "recall_questions, source_chunk_ids, confidence) "
        "VALUES (?, ?, ?, 'lesson body', '[]', '[]', 80)",
        (f"L-{objective_id}", objective_id, SUBJECT),
    )
    db.commit()


# --- Test 1 ----------------------------------------------------------------
def test_ingest_moves_file_and_records_doc(db, env, monkeypatch):
    sid, src = stage_and_classify(db, env)
    fake, _rec = make_fake_ingest(doc_id="doc-1")
    monkeypatch.setattr(ingest, "ingest_document", fake)

    res = upload_ingest.ingest_staged_file(db, sid)

    assert not src.exists()  # moved out of staging
    dest = env["kb"] / SUBJECT / "02_PAST_PAPERS" / "paper.pdf"
    assert dest.exists()
    row = db.execute(
        "SELECT ingestion_status, ingested_doc_id FROM upload_staging WHERE staging_id = ?",
        (sid,),
    ).fetchone()
    assert row["ingestion_status"] == "ingested"
    assert row["ingested_doc_id"] == "doc-1"
    assert res["chunks_created"] == 5


# --- Test 2 ----------------------------------------------------------------
def test_overridden_uses_review_folder(db, env, monkeypatch):
    sid, _src = stage_and_classify(
        db, env, name="notes.pdf", decision="overridden",
        recommended_folder="04_NOTES", review_folder="02_PAST_PAPERS",
    )
    fake, rec = make_fake_ingest(doc_id="doc-2")
    monkeypatch.setattr(ingest, "ingest_document", fake)

    upload_ingest.ingest_staged_file(db, sid)

    assert (env["kb"] / SUBJECT / "02_PAST_PAPERS" / "notes.pdf").exists()
    assert not (env["kb"] / SUBJECT / "04_NOTES" / "notes.pdf").exists()
    assert rec["content_type"] == "past_paper"   # 02_PAST_PAPERS -> past_paper


# --- Test 3 ----------------------------------------------------------------
def test_rejected_raises(db, env, monkeypatch):
    sid, src = stage_and_classify(db, env, decision="rejected")
    fake, rec = make_fake_ingest()
    monkeypatch.setattr(ingest, "ingest_document", fake)

    with pytest.raises(ValueError):
        upload_ingest.ingest_staged_file(db, sid)

    assert src.exists()                       # nothing moved
    assert "called" not in rec                # ingester never invoked
    assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


# --- Test 4 ----------------------------------------------------------------
def test_already_ingested_raises_on_second_call(db, env, monkeypatch):
    sid, _src = stage_and_classify(db, env)
    fake, _rec = make_fake_ingest(doc_id="doc-4")
    monkeypatch.setattr(ingest, "ingest_document", fake)

    upload_ingest.ingest_staged_file(db, sid)   # first: succeeds
    with pytest.raises(ValueError):
        upload_ingest.ingest_staged_file(db, sid)  # second: already ingested


# --- Test 5 ----------------------------------------------------------------
def test_failed_ingestion_leaves_file_in_staging(db, env, monkeypatch):
    sid, src = stage_and_classify(db, env)
    fake, _rec = make_fake_ingest(raises=True)
    monkeypatch.setattr(ingest, "ingest_document", fake)

    with pytest.raises(RuntimeError):
        upload_ingest.ingest_staged_file(db, sid)

    assert src.exists()  # still in staging -- no half-move
    assert not (env["kb"] / SUBJECT / "02_PAST_PAPERS" / "paper.pdf").exists()
    row = db.execute(
        "SELECT ingestion_status, ingestion_error FROM upload_staging WHERE staging_id = ?",
        (sid,),
    ).fetchone()
    assert row["ingestion_status"] == "failed"
    assert "simulated ingest failure" in (row["ingestion_error"] or "")
    # the failure is also logged
    assert db.execute(
        "SELECT success FROM ingestion_log WHERE staging_id = ?", (sid,)
    ).fetchone()["success"] == 0


# --- Test 6 ----------------------------------------------------------------
def test_successful_ingestion_stales_matching_lessons(db, env, monkeypatch):
    seed_lesson(db, "POB-1.2")
    sid, _src = stage_and_classify(
        db, env, objectives_json=json.dumps([{"objective_id": "POB-1.2", "confidence": 90}]),
    )
    fake, _rec = make_fake_ingest(doc_id="doc-6", objectives_hit=["POB-1.2"])
    monkeypatch.setattr(ingest, "ingest_document", fake)

    res = upload_ingest.ingest_staged_file(db, sid)

    lesson = db.execute(
        "SELECT is_stale, stale_reason FROM objective_lessons WHERE objective_id = 'POB-1.2'"
    ).fetchone()
    assert lesson["is_stale"] == 1
    assert lesson["stale_reason"] == "new_source_material_added"
    assert res["lessons_staled"] == ["POB-1.2"]


# --- Test 7 ----------------------------------------------------------------
def test_preferred_objectives_reach_ingester(db, env, monkeypatch):
    sid, _src = stage_and_classify(
        db, env,
        objectives_json=json.dumps([
            {"objective_id": "POB-1.2", "confidence": 90},
            {"objective_id": "POB-5.6", "confidence": 80},
        ]),
    )
    fake, rec = make_fake_ingest(doc_id="doc-7")
    monkeypatch.setattr(ingest, "ingest_document", fake)

    upload_ingest.ingest_staged_file(db, sid)

    assert rec["preferred_objectives"] == ["POB-1.2", "POB-5.6"]


# --- Test 8 ----------------------------------------------------------------
def test_destination_collision_appends_suffix(db, env, monkeypatch):
    sid, _src = stage_and_classify(db, env, name="paper.pdf")
    # Pre-create a file at the destination so the move must avoid overwriting it.
    dest_dir = env["kb"] / SUBJECT / "02_PAST_PAPERS"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "paper.pdf").write_bytes(b"existing")

    fake, _rec = make_fake_ingest(doc_id="doc-8")
    monkeypatch.setattr(ingest, "ingest_document", fake)

    res = upload_ingest.ingest_staged_file(db, sid)

    assert res["destination"].endswith("paper_2.pdf")
    assert (dest_dir / "paper_2.pdf").exists()
    assert (dest_dir / "paper.pdf").read_bytes() == b"existing"  # original untouched
