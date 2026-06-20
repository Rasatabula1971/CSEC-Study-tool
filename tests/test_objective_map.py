"""
tests/test_objective_map.py
===========================
UI overhaul session 3: GET /api/objectives/{subject}/map.

The map groups objectives by section and tags each with a status. 'mastered'
reuses the SAME study_plan model as get_plan_progress (the "X of Y mastered"
header), so counting map 'mastered' rows must equal get_plan_progress()['mastered']
exactly. is_next_due flags objectives in get_due_objectives().

Run: pytest tests/test_objective_map.py -v
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module          # noqa: E402
from study_plan import get_plan_progress  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"


def make_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping objective-map tests")
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

    db.execute("INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
               (SUBJECT, "Principles of Business"))
    db.execute("INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
               "VALUES ('SEC-1', ?, 'Nature of Business', '1')", (SUBJECT,))
    for i in (1, 2, 3):
        db.execute("INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
                   "content_stmt) VALUES (?, 'SEC-1', ?, ?, ?)",
                   (f"POB-1.{i}", SUBJECT, f"1.{i}", f"Objective {i}"))
    # study_plan: 1.1 mastered, 1.2 + 1.3 unmet (so get_plan_progress mastered == 1).
    db.execute("INSERT INTO study_plan (subject_id, objective_id, status, met_count) "
               "VALUES (?, 'POB-1.1', 'mastered', 2)", (SUBJECT,))
    db.execute("INSERT INTO study_plan (subject_id, objective_id, status) VALUES (?, 'POB-1.2', 'unmet')", (SUBJECT,))
    db.execute("INSERT INTO study_plan (subject_id, objective_id, status) VALUES (?, 'POB-1.3', 'unmet')", (SUBJECT,))
    # 1.2 has a study_sessions row -> 'attempted'; 1.3 has none -> 'not_started'.
    db.execute("INSERT INTO study_sessions (subject_id, objective_id, mode, outcome, score_pct) "
               "VALUES (?, 'POB-1.2', 'grade', 'fail', 40)", (SUBJECT,))
    # 1.3 is due for review today -> is_next_due.
    db.execute("INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
               "VALUES ('POB-1.3', ?, 40, 1, ?)", (SUBJECT, date.today().isoformat()))
    db.commit()
    return db


def _client(db):
    app_module.app.state.db = db
    return TestClient(app_module.app)


def test_map_status_counts_match_mastery():
    db = make_db()
    body = _client(db).get(f"/api/objectives/{SUBJECT}/map").json()
    objs = {o["objective_id"]: o for s in body["sections"] for o in s["objectives"]}
    assert set(objs) == {"POB-1.1", "POB-1.2", "POB-1.3"}

    # 'mastered' count from the map must equal get_plan_progress exactly.
    map_mastered = sum(1 for o in objs.values() if o["status"] == "mastered")
    assert map_mastered == get_plan_progress(db, SUBJECT)["mastered"] == 1

    assert objs["POB-1.1"]["status"] == "mastered"
    assert objs["POB-1.2"]["status"] == "attempted"     # has a study_sessions row
    assert objs["POB-1.3"]["status"] == "not_started"   # no study_sessions row


def test_is_next_due_flags_due_objectives():
    db = make_db()
    body = _client(db).get(f"/api/objectives/{SUBJECT}/map").json()
    objs = {o["objective_id"]: o for s in body["sections"] for o in s["objectives"]}
    assert objs["POB-1.3"]["is_next_due"] is True       # weakness_log due today
    assert objs["POB-1.1"]["is_next_due"] is False
    assert objs["POB-1.2"]["is_next_due"] is False


def test_map_groups_by_section():
    db = make_db()
    body = _client(db).get(f"/api/objectives/{SUBJECT}/map").json()
    assert len(body["sections"]) == 1
    sec = body["sections"][0]
    assert sec["section_id"] == "SEC-1"
    assert sec["title"] == "Nature of Business"
    assert [o["objective_id"] for o in sec["objectives"]] == ["POB-1.1", "POB-1.2", "POB-1.3"]
