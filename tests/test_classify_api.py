"""
tests/test_classify_api.py
==========================
Upload session 3 -- endpoint tests for the classification + review routes.

Real in-memory SQLite through a Starlette TestClient (lifespan does NOT run, so we
own app.state.db, like test_upload_api.py). The background classification task is
monkeypatched to a no-op so no model/network is touched -- the queued COUNT is
computed synchronously, which is what these tests assert.

  1. POST /api/staging/{subject}/classify-all queues eligible files only.
  2. POST /api/staging/{id}/review records an 'accepted' decision.
  3. POST /api/staging/{id}/review with 'overridden' stores the overrides.
  4. POST /api/staging/{id}/unskip clears the skip flag.
  5. GET  /api/staging/{subject}/classifications orders classified-first.

Run: pytest tests/test_classify_api.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module   # noqa: E402

SUBJECT = "Principles_of_Business"


# --- fixtures --------------------------------------------------------------
def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping classify API tests")
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
    db.commit()


def insert_staging(db, name, *, extract_status="ready", skip_classification=0,
                   classification_status="unclassified", extracted_text="x",
                   created_at="2026-06-17 09:00:00"):
    db.execute(
        """
        INSERT INTO upload_staging
            (subject_id, original_name, stored_path, file_type, file_size_bytes,
             extracted_text, extract_status, status, created_at,
             skip_classification, classification_status)
        VALUES (?, ?, ?, 'pdf', 1234, ?, ?, 'staged', ?, ?, ?)
        """,
        (SUBJECT, name, f"/tmp/{name}", extracted_text, extract_status, created_at,
         skip_classification, classification_status),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def insert_classification(db, sid, *, folder="02_PAST_PAPERS", conf=90,
                          objectives_json="[]", model_used="gemini"):
    db.execute(
        "INSERT INTO upload_classifications "
        "(staging_id, recommended_folder, folder_confidence, objectives_json, "
        " rationale, model_used) VALUES (?, ?, ?, ?, 'rationale', ?)",
        (sid, folder, conf, objectives_json, model_used),
    )
    db.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SSD_ROOT", str(tmp_path))
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(db, monkeypatch):
    app_module.app.state.db = db
    # Neutralise the background classification so no model/network runs in tests.
    monkeypatch.setattr(app_module, "_run_classification", lambda *a, **k: None)
    return TestClient(app_module.app)


# --- Test 1: classify-all queues only eligible files -----------------------
def test_classify_all_queues_eligible_only(client, db):
    insert_staging(db, "elig-1.pdf")
    insert_staging(db, "elig-2.pdf")
    insert_staging(db, "elig-3.pdf")
    insert_staging(db, "skipped.pdf", skip_classification=1,
                   classification_status="skipped")
    insert_staging(db, "done.pdf", classification_status="classified")

    res = client.post(f"/api/staging/{SUBJECT}/classify-all", json={})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["queued"] == 3


# --- Test 2: review records an accepted decision ---------------------------
def test_review_records_accepted(client, db):
    sid = insert_staging(db, "paper.pdf", classification_status="classified")
    insert_classification(db, sid)

    res = client.post(f"/api/staging/{sid}/review", json={"decision": "accepted"})
    assert res.status_code == 200
    assert res.json()["ok"] is True

    row = db.execute(
        "SELECT review_decision, reviewed_at FROM upload_classifications WHERE staging_id = ?",
        (sid,),
    ).fetchone()
    assert row["review_decision"] == "accepted"
    assert row["reviewed_at"] is not None


# --- Test 3: review overridden stores the overrides ------------------------
def test_review_overridden_stores_overrides(client, db):
    sid = insert_staging(db, "notes.pdf", classification_status="classified")
    insert_classification(db, sid, folder="02_PAST_PAPERS")

    res = client.post(f"/api/staging/{sid}/review", json={
        "decision": "overridden",
        "override_folder": "04_NOTES",
        "override_objectives": [{"objective_id": "POB-1.1"}, {"objective_id": "POB-1.2"}],
        "notes": "Actually lecture notes.",
    })
    assert res.status_code == 200

    row = db.execute(
        "SELECT review_decision, review_folder, review_objectives_json, review_notes "
        "FROM upload_classifications WHERE staging_id = ?", (sid,),
    ).fetchone()
    assert row["review_decision"] == "overridden"
    assert row["review_folder"] == "04_NOTES"
    import json
    objs = json.loads(row["review_objectives_json"])
    assert [o["objective_id"] for o in objs] == ["POB-1.1", "POB-1.2"]
    assert row["review_notes"] == "Actually lecture notes."


# --- Test 4: unskip clears the skip flag -----------------------------------
def test_unskip_clears_flag(client, db):
    sid = insert_staging(db, "scan.pdf", skip_classification=1,
                         classification_status="skipped")

    res = client.post(f"/api/staging/{sid}/unskip")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    row = db.execute(
        "SELECT skip_classification, classification_status FROM upload_staging "
        "WHERE staging_id = ?", (sid,),
    ).fetchone()
    assert row["skip_classification"] == 0
    assert row["classification_status"] == "unclassified"


# --- Test 5: classifications ordering --------------------------------------
def test_classifications_ordering(client, db):
    # Insert in a deliberately shuffled order; the endpoint must reorder.
    s_failed = insert_staging(db, "failed.pdf", classification_status="failed")
    s_skip = insert_staging(db, "skip.pdf", skip_classification=1,
                            classification_status="skipped")
    s_unclass = insert_staging(db, "unclass.pdf", classification_status="unclassified")
    s_class = insert_staging(db, "class.pdf", classification_status="classified")
    insert_classification(db, s_class)

    res = client.get(f"/api/staging/{SUBJECT}/classifications")
    assert res.status_code == 200
    items = res.json()["items"]
    order = [it["staging_id"] for it in items]
    assert order == [s_class, s_unclass, s_skip, s_failed]
    # The classified row carries its classification payload; others do not.
    by_id = {it["staging_id"]: it for it in items}
    assert by_id[s_class]["classification"]["recommended_folder"] == "02_PAST_PAPERS"
    assert by_id[s_unclass]["classification"] is None
