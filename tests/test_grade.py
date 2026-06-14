"""
tests/test_grade.py
===================
Stage 8 tests for the synthesis grader (grade.grade_synthesis), the Option-C
batch grader: exactly one expected point per objective, Python computes the
score, weakness_log updates once per objective.

ollama_chat is stubbed throughout -- no Ollama. Run: pytest tests/test_grade.py -v
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import grade  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
SUBJECT = "Principles_of_Business"


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping grade tests")
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


def seed_batch(db, n=5) -> int:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    objective_ids = []
    for i in range(1, n + 1):
        oid = f"POB-1.{i}"
        objective_ids.append(oid)
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt, command_words) VALUES (?, 'SEC-1', ?, ?, ?, ?)",
            (oid, SUBJECT, f"1.{i}", f"Objective {i}", '["Explain"]'),
        )
    cur = db.execute(
        "INSERT INTO study_batches (subject_id, objective_ids, status) VALUES (?, ?, 'active')",
        (SUBJECT, json.dumps(objective_ids)),
    )
    db.commit()
    return cur.lastrowid


@pytest.fixture
def db():
    conn = open_test_db()
    yield conn
    conn.close()


def _fake_chat_for(batch_id, objective_ids, awarded_flags):
    """Return a chat stub that emits one point per objective with given awards."""
    points = [
        {"mark_point_id": f"{batch_id}-syn-{oid}", "awarded": flag,
         "evidence": f"point for {oid}"}
        for oid, flag in zip(objective_ids, awarded_flags)
    ]
    payload = json.dumps({
        "objective_id": f"batch-{batch_id}",
        "question_id": f"synthesis-{batch_id}",
        "points": points,
    })

    def fake_chat(messages, system, schema=None):
        return payload

    return fake_chat


def test_grade_synthesis_returns_n_points(db):
    batch_id = seed_batch(db, n=5)
    oids = [f"POB-1.{i}" for i in range(1, 6)]
    fake = _fake_chat_for(batch_id, oids, [True, True, False, True, False])

    result = grade.grade_synthesis(db, batch_id, "my synthesis answer", chat_fn=fake)
    assert result["total"] == 5
    assert len(result["points"]) == 5
    assert result["awarded"] == 3
    assert result["score_pct"] == 60  # round(100 * 3 / 5)


def test_grade_synthesis_mark_point_id_pattern(db):
    batch_id = seed_batch(db, n=5)
    oids = [f"POB-1.{i}" for i in range(1, 6)]
    fake = _fake_chat_for(batch_id, oids, [True] * 5)

    result = grade.grade_synthesis(db, batch_id, "answer", chat_fn=fake)
    for p in result["points"]:
        assert re.fullmatch(rf"{batch_id}-syn-POB-1\.\d+", p["mark_point_id"])
    # exactly the expected ids, one per objective
    assert {p["mark_point_id"] for p in result["points"]} == {
        f"{batch_id}-syn-{oid}" for oid in oids
    }


def test_grade_synthesis_logs_weakness_once_per_objective(db, monkeypatch):
    batch_id = seed_batch(db, n=5)
    oids = [f"POB-1.{i}" for i in range(1, 6)]
    fake = _fake_chat_for(batch_id, oids, [True, False, True, False, True])

    calls = []
    real_log = grade.log_weakness

    def spy(db_, grading_result, session_id):
        calls.append((grading_result["objective_id"], grading_result["score_pct"]))
        return real_log(db_, grading_result, session_id)

    monkeypatch.setattr(grade, "log_weakness", spy)

    grade.grade_synthesis(db, batch_id, "answer", chat_fn=fake)
    # one call per objective in the batch, no more
    assert [c[0] for c in calls] == oids
    assert len(calls) == 5
    # awarded -> 100, missed -> 0
    assert dict(calls) == {
        "POB-1.1": 100, "POB-1.2": 0, "POB-1.3": 100, "POB-1.4": 0, "POB-1.5": 100,
    }


def test_grade_synthesis_unknown_batch(db):
    def boom(*a, **k):
        raise AssertionError("LLM called for an unknown batch")

    assert grade.grade_synthesis(db, 999, "answer", chat_fn=boom) == {
        "error": "unknown_batch"
    }


# ---------------------------------------------------------------------------
# reconcile_grading: deterministic repair of self-contradictory 3B output
# ---------------------------------------------------------------------------
def test_reconcile_flips_contradictory_miss():
    result = {"points": [
        {"mark_point_id": "POB-1.1-syn-1", "awarded": False,
         "evidence": "student gave the reason for barter clearly"},
    ]}
    out = grade.reconcile_grading(result)
    p = out["points"][0]
    assert p["awarded"] is True
    assert p["evidence"].startswith("[auto-corrected]")


def test_reconcile_ignores_genuine_miss():
    result = {"points": [
        {"mark_point_id": "POB-1.1-syn-1", "awarded": False,
         "evidence": "answer does not mention double coincidence of wants"},
    ]}
    out = grade.reconcile_grading(result)
    assert out["points"][0]["awarded"] is False


def test_reconcile_fills_empty_evidence():
    result = {"points": [
        {"mark_point_id": "POB-1.1-syn-1", "awarded": False, "evidence": ""},
    ]}
    out = grade.reconcile_grading(result)
    assert out["points"][0]["evidence"] == "No relevant content found in answer."


def test_reconcile_does_not_touch_awarded_true():
    result = {"points": [
        {"mark_point_id": "POB-1.1-syn-1", "awarded": True,
         "evidence": "student explained it"},
    ]}
    out = grade.reconcile_grading(result)
    p = out["points"][0]
    assert p["awarded"] is True
    assert p["evidence"] == "student explained it"
    assert "[auto-corrected]" not in p["evidence"]
