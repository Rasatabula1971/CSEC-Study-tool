"""
tests/test_study_plan.py
========================
Stage 8 (Study Plan) tests for the deterministic plan engine: seeding, batch
selection priority, status transitions, and progress aggregation.

All DB work uses an in-memory schema DB; Ollama is never contacted. State
arithmetic is pure Python -- no LLM is involved in any assertion here.

Run: pytest tests/test_study_plan.py -v
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import study_plan  # noqa: E402
import controller  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"


def open_migrated_db() -> sqlite3.Connection:
    """A real in-memory DB with schema.sql + apply_runtime_migrations (so the
    objective_lessons / lesson_generation_queue tables exist). check_same_thread is
    off because the TestClient runs sync endpoints in a worker thread."""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping study_plan API tests")
    import app as app_module

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


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping study_plan tests")
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


def seed(db: sqlite3.Connection, n_objectives: int = 4) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        ("Principles_of_Business", "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-1', 'Principles_of_Business', 'Nature of Business', '1')",
    )
    for i in range(1, n_objectives + 1):
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt) VALUES (?, 'SEC-1', 'Principles_of_Business', ?, ?)",
            (f"POB-1.{i}", f"1.{i}", f"Objective {i}"),
        )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


SUBJECT = "Principles_of_Business"


# ---------------------------------------------------------------------------
# init_plan_for_subject
# ---------------------------------------------------------------------------
def test_init_plan_seeds_unmet_rows(db):
    inserted = study_plan.init_plan_for_subject(db, SUBJECT)
    assert inserted == 4
    rows = db.execute(
        "SELECT status FROM study_plan WHERE subject_id = ?", (SUBJECT,)
    ).fetchall()
    assert len(rows) == 4
    assert all(r["status"] == "unmet" for r in rows)


def test_init_plan_is_idempotent(db):
    assert study_plan.init_plan_for_subject(db, SUBJECT) == 4
    assert study_plan.init_plan_for_subject(db, SUBJECT) == 0  # nothing new
    assert db.execute("SELECT COUNT(*) FROM study_plan").fetchone()[0] == 4


# ---------------------------------------------------------------------------
# get_next_batch
# ---------------------------------------------------------------------------
def test_get_next_batch_unmet_in_syllabus_order(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    batch = study_plan.get_next_batch(db, SUBJECT, batch_size=3)
    assert [o["objective_id"] for o in batch] == ["POB-1.1", "POB-1.2", "POB-1.3"]
    # full objective rows carry section info
    assert batch[0]["section_title"] == "Nature of Business"
    assert batch[0]["source"] == "new"


def test_get_next_batch_prioritises_leitner_due(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    today = date.today().isoformat()
    # POB-1.3 is due for review -> must come before any unmet objective.
    db.execute(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES ('POB-1.3', ?, 40, 1, ?)",
        (SUBJECT, today),
    )
    db.commit()
    batch = study_plan.get_next_batch(db, SUBJECT, batch_size=3)
    assert batch[0]["objective_id"] == "POB-1.3"
    assert batch[0]["source"] == "review"
    # remaining slots fill with unmet, in syllabus order, skipping the due one
    assert [o["objective_id"] for o in batch[1:]] == ["POB-1.1", "POB-1.2"]


def test_get_next_batch_due_ordered_by_box(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    today = date.today().isoformat()
    db.executemany(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("POB-1.1", SUBJECT, 50, 3, today),
            ("POB-1.2", SUBJECT, 50, 1, today),  # lower box -> first
        ],
    )
    db.commit()
    batch = study_plan.get_next_batch(db, SUBJECT, batch_size=2)
    assert [o["objective_id"] for o in batch] == ["POB-1.2", "POB-1.1"]


def test_get_next_batch_returns_fewer_when_scarce(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    batch = study_plan.get_next_batch(db, SUBJECT, batch_size=10)
    assert len(batch) == 4  # only 4 objectives exist


# ---------------------------------------------------------------------------
# mark_objective_outcome
# ---------------------------------------------------------------------------
def _set_last_met(db, objective_id, iso_date):
    """Backdate last_met_at so a 'new day' pass can be simulated deterministically."""
    db.execute(
        "UPDATE study_plan SET last_met_at = ? WHERE subject_id = ? AND objective_id = ?",
        (iso_date, SUBJECT, objective_id),
    )
    db.commit()


def _status(db, objective_id) -> dict:
    r = db.execute(
        "SELECT status, met_count, last_met_at FROM study_plan "
        "WHERE subject_id = ? AND objective_id = ?",
        (SUBJECT, objective_id),
    ).fetchone()
    return dict(r)


def test_unmet_to_met_once_to_mastered_across_days(db):
    study_plan.init_plan_for_subject(db, SUBJECT)

    # First passing session -> met_once.
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80)
    s = _status(db, "POB-1.1")
    assert s["status"] == "met_once" and s["met_count"] == 1

    # Simulate a previous day, then a second passing session -> mastered.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    _set_last_met(db, "POB-1.1", yesterday)
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 90)
    s = _status(db, "POB-1.1")
    assert s["status"] == "mastered" and s["met_count"] == 2
    assert s["last_met_at"] == date.today().isoformat()


def test_fail_resets_to_unmet(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80)  # met_once
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 40)  # fail
    s = _status(db, "POB-1.1")
    assert s["status"] == "unmet" and s["met_count"] == 0
    assert s["last_met_at"] is None


def test_same_day_double_pass_does_not_short_circuit_mastery(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80)  # met_once today
    # Second pass the SAME day must NOT advance to mastered.
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 95)
    s = _status(db, "POB-1.1")
    assert s["status"] == "met_once" and s["met_count"] == 1


def test_mark_outcome_updates_weakness_log(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80)
    row = db.execute(
        "SELECT score_pct FROM weakness_log WHERE objective_id = 'POB-1.1'"
    ).fetchone()
    assert row is not None and row["score_pct"] == 80


def test_mark_outcome_can_suppress_weakness_log(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80, update_weakness=False)
    row = db.execute(
        "SELECT COUNT(*) FROM weakness_log WHERE objective_id = 'POB-1.1'"
    ).fetchone()[0]
    assert row == 0  # study_plan advanced, but no weakness write
    assert _status(db, "POB-1.1")["status"] == "met_once"


# ---------------------------------------------------------------------------
# get_plan_progress
# ---------------------------------------------------------------------------
def test_get_plan_progress_counts(db):
    study_plan.init_plan_for_subject(db, SUBJECT)
    # POB-1.1 -> mastered (two days), POB-1.2 -> met_once, others unmet.
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80)
    _set_last_met(db, "POB-1.1", (date.today() - timedelta(days=1)).isoformat())
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.1", 80)
    study_plan.mark_objective_outcome(db, SUBJECT, "POB-1.2", 80)

    prog = study_plan.get_plan_progress(db, SUBJECT)
    assert prog["total"] == 4
    assert prog["mastered"] == 1
    assert prog["met_once"] == 1
    assert prog["unmet"] == 2
    assert prog["percent_mastered"] == 25


# ---------------------------------------------------------------------------
# Batch step alignment: lesson objective == question objective == batch position
#
# Regression for the "teach POB-3.10 / quiz POB-1.1" bug: the teach route used to
# ignore the named objective_id and semantic-search on the generic lesson query,
# so the lesson taught an arbitrary objective while the question used the real
# batch objective. Within every step the three must be identical.
# ---------------------------------------------------------------------------
def _fake_chat(messages, system, schema=None):
    """Deterministic stand-in for Ollama -- the lesson/question text is irrelevant
    to objective alignment, so just echo a stable stem."""
    return "Example CSEC-style question for this objective."


def test_batch_lesson_and_question_share_objective(db):
    # Build a fresh 5-objective subject so a full batch has all five positions.
    db.execute("DELETE FROM objectives")
    for i in range(1, 6):
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt) VALUES (?, 'SEC-1', ?, ?, ?)",
            (f"POB-1.{i}", SUBJECT, f"1.{i}", f"Content for objective {i}"),
        )
    db.commit()

    started = controller.handle_request(
        db, {"route": "start_batch", "subject_id": SUBJECT}, chat_fn=_fake_chat
    )
    batch_id = started["batch_id"]
    # The order the stepper sees (state.plan.objectives) -- positions 0..4.
    expected = [o["objective_id"] for o in started["objectives"]]
    assert len(expected) == 5

    for step in range(1, 6):
        expected_oid = expected[step - 1]

        # Lesson: the stepper passes objectives[step-1].objective_id to route=teach.
        lesson = controller.handle_request(
            db,
            {
                "route": "teach",
                "subject_id": SUBJECT,
                "objective_id": expected_oid,
                "query": "Teach me this objective",
            },
            chat_fn=_fake_chat,
        )
        # Question: the stepper passes the 1-based step to route=batch_question.
        question = controller.handle_request(
            db,
            {"route": "batch_question", "batch_id": batch_id, "step": str(step)},
            chat_fn=_fake_chat,
        )

        assert lesson["objective_id"] == expected_oid, (
            f"step {step}: lesson taught {lesson['objective_id']}, expected {expected_oid}"
        )
        assert question["objective_id"] == expected_oid, (
            f"step {step}: question on {question['objective_id']}, expected {expected_oid}"
        )
        # The crux: lesson and question agree within the step.
        assert lesson["objective_id"] == question["objective_id"]

        # And grading resolves the SAME objective from the stored question_id.
        resolved = controller._resolve_question_objective(db, question["question_id"])
        assert resolved is not None and resolved[0] == expected_oid


def test_teach_named_objective_ignores_generic_query(db):
    # Even with notes chunks present for OTHER objectives, a named-objective teach
    # must return exactly the named objective (never a semantic nearest match).
    started = controller.handle_request(
        db, {"route": "start_batch", "subject_id": SUBJECT}, chat_fn=_fake_chat
    )
    target = started["objectives"][0]["objective_id"]
    lesson = controller.handle_request(
        db,
        {"route": "teach", "subject_id": SUBJECT, "objective_id": target,
         "query": "Teach me this objective"},
        chat_fn=_fake_chat,
    )
    assert lesson["objective_id"] == target
    # No canonical lesson yet -> placeholder (runtime no longer generates). The key
    # invariant holds: a named-objective teach resolves EXACTLY the named objective.
    assert lesson["lesson_source"] == "placeholder"
    assert lesson["source_file"] is None


# ---------------------------------------------------------------------------
# /plan page + GET /api/objective/{id}  (backs the "Jump to objective" input)
#
# These drive the real FastAPI app against a real in-memory DB. The endpoint
# routes through controller.handle_request with route='teach', so it shares the
# canonical-lesson / placeholder code path with the batch loader.
# ---------------------------------------------------------------------------
from starlette.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402


def _seed_jump(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-3', ?, 'Production', '3')",
        (SUBJECT,),
    )
    # POB-3.1 has a canonical lesson; POB-1.11 deliberately has none (placeholder path).
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt) VALUES ('POB-3.1', 'SEC-3', ?, '3.1', 'Explain the levels of production')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt) VALUES ('POB-1.11', 'SEC-3', ?, '1.11', 'Describe forms of business')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, "
        "recall_questions, source_chunk_ids, confidence) "
        "VALUES ('L31', 'POB-3.1', ?, 'Production has primary, secondary and tertiary levels.', "
        "'[\"Name the three levels of production.\"]', '[\"c1\"]', 90)",
        (SUBJECT,),
    )
    db.commit()


@pytest.fixture
def jump_client():
    db = open_migrated_db()
    _seed_jump(db)
    app_module.app.state.db = db
    yield TestClient(app_module.app)
    db.close()


def test_plan_page_returns_200(jump_client):
    res = jump_client.get("/plan")
    assert res.status_code == 200


def test_objective_endpoint_returns_lesson_and_recall(jump_client):
    res = jump_client.get("/api/objective/POB-3.1")
    assert res.status_code == 200
    body = res.json()
    assert body["objective_id"] == "POB-3.1"
    assert body["lesson_source"] == "canonical"
    assert body["lesson_text"]
    assert body["recall_questions"] == ["Name the three levels of production."]


def test_objective_endpoint_404_when_unknown(jump_client):
    res = jump_client.get("/api/objective/POB-99.99")
    assert res.status_code == 404


def test_objective_endpoint_placeholder_when_no_lesson(jump_client):
    res = jump_client.get("/api/objective/POB-1.11")
    assert res.status_code == 200
    body = res.json()
    # Existing placeholder contract (CLAUDE.md "Lesson quality fix"): no canonical
    # lesson -> honest placeholder, no fabricated recall questions, no source.
    assert body["lesson_source"] == "placeholder"
    assert body["recall_questions"] == []
    assert body["source_file"] is None
    assert body["page"] is None
    assert body["context_source"] == "syllabus"
