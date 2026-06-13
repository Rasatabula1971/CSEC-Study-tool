"""
tests/test_core.py
==================
Stage 5 tests for the deterministic core: scope, retrieval routing, grade,
schedule, weakness, and a controller scope/teach smoke test.

All DB work uses an in-memory schema DB; Ollama is never contacted -- chat/embed
are injected stubs. Run: pytest tests/test_core.py -v
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import scope  # noqa: E402
import retrieval  # noqa: E402
import grade  # noqa: E402
import schedule  # noqa: E402
import weakness  # noqa: E402
import controller  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"


def open_test_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping core tests")
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


def seed(db: sqlite3.Connection, *, locked: int = 1) -> None:
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, ?)",
        ("Principles_of_Business", "Principles of Business", locked),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?, ?, ?, ?)",
        ("POB-SEC-1", "Principles_of_Business", "Nature of Business", "1"),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt) VALUES (?, ?, ?, ?, ?)",
        ("POB-1.1", "POB-SEC-1", "Principles_of_Business", "1.1",
         "Explain the nature and functions of a business"),
    )
    db.commit()


@pytest.fixture
def db():
    conn = open_test_db()
    seed(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# scope.py
# ---------------------------------------------------------------------------
def test_scope_in_scope_objective_true(db):
    assert scope.is_in_scope(db, "Principles_of_Business", "POB-1.1") is True


def test_scope_unlocked_subject_false():
    conn = open_test_db()
    seed(conn, locked=0)
    try:
        assert scope.is_in_scope(conn, "Principles_of_Business", "POB-1.1") is False
        assert scope.subject_is_locked(conn, "Principles_of_Business") is False
    finally:
        conn.close()


def test_scope_unknown_objective_false(db):
    assert scope.is_in_scope(db, "Principles_of_Business", "POB-9.9") is False


def test_get_objective(db):
    obj = scope.get_objective(db, "POB-1.1")
    assert obj is not None and obj["objective_id"] == "POB-1.1"
    assert scope.get_objective(db, "NOPE") is None


# ---------------------------------------------------------------------------
# schedule.py
# ---------------------------------------------------------------------------
def test_update_leitner_pass_moves_up():
    box, nxt = schedule.update_leitner(3, 75)
    assert box == 4
    assert nxt == (date.today() + timedelta(days=7)).isoformat()


def test_update_leitner_fail_resets_to_box_one():
    box, nxt = schedule.update_leitner(3, 40)
    assert box == 1
    assert nxt == (date.today() + timedelta(days=1)).isoformat()  # tomorrow


def test_update_leitner_cap_at_five():
    box, _ = schedule.update_leitner(5, 90)
    assert box == 5


def test_get_due_objectives_orders_by_box(db):
    today = date.today().isoformat()
    future = (date.today() + timedelta(days=10)).isoformat()
    db.executemany(
        "INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("POB-1.1", "Principles_of_Business", 40, 3, today),
            ("POB-1.1", "Principles_of_Business", 50, 1, today),   # lower box -> first
            ("POB-1.1", "Principles_of_Business", 90, 2, future),  # not due -> excluded
        ],
    )
    db.commit()
    due = schedule.get_due_objectives(db, "Principles_of_Business")
    assert [d["leitner_box"] for d in due] == [1, 3]


# ---------------------------------------------------------------------------
# grade.py
# ---------------------------------------------------------------------------
def test_grade_two_of_three_points(db):
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ms1", "Principles_of_Business", "mark_scheme", "ms.pdf", "h1"),
    )
    for i, mp in enumerate(["mp1", "mp2", "mp3"], 1):
        db.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (mp, "POB-1.1", "q1", "ms1", f"point {i}", i),
        )
    db.commit()

    valid_json = (
        '{"objective_id":"POB-1.1","question_id":"q1","points":['
        '{"mark_point_id":"mp1","awarded":true,"evidence":"a"},'
        '{"mark_point_id":"mp2","awarded":true,"evidence":"b"},'
        '{"mark_point_id":"mp3","awarded":false,"evidence":"missing"}]}'
    )

    def fake_chat(messages, system, schema=None):
        return valid_json

    result = grade.grade_answer(db, "q1", "my answer", chat_fn=fake_chat)
    assert result["awarded"] == 2
    assert result["total"] == 3
    assert result["score_pct"] == 67
    assert result["missed_points"] == ["mp3"]
    # point_text is joined in from the mark_points rows for display
    assert [p["point_text"] for p in result["points"]] == ["point 1", "point 2", "point 3"]


def test_grade_no_mark_scheme(db):
    def fake_chat(messages, system, schema=None):  # must never be reached
        raise AssertionError("LLM called despite no mark scheme")

    result = grade.grade_answer(db, "unknown-q", "answer", chat_fn=fake_chat)
    assert result == {"error": "no_mark_scheme"}


# ---------------------------------------------------------------------------
# weakness.py
# ---------------------------------------------------------------------------
def test_weakness_valid_insert_then_upsert(db):
    grading = {
        "objective_id": "POB-1.1",
        "subject_id": "Principles_of_Business",
        "score_pct": 40,
        "missed_points": ["mp3"],
    }
    out = weakness.log_weakness(db, grading, session_id=1)
    assert out["leitner_box"] == 1
    assert out["next_review"] == date.today().isoformat()
    row = db.execute("SELECT count(*) FROM weakness_log").fetchone()[0]
    assert row == 1

    # second grade, this time a pass -> box advances 1 -> 2
    grading["score_pct"] = 85
    out2 = weakness.log_weakness(db, grading, session_id=2)
    assert out2["leitner_box"] == 2
    assert db.execute("SELECT count(*) FROM weakness_log").fetchone()[0] == 1  # upsert, not new row


def test_weakness_invalid_raises_value_error(db):
    bad = {"objective_id": "POB-1.1", "score_pct": 40}  # missing subject_id
    with pytest.raises(ValueError):
        weakness.log_weakness(db, bad, session_id=1)


# ---------------------------------------------------------------------------
# retrieval.py routing
# ---------------------------------------------------------------------------
def test_retrieval_uses_structured_when_all_keys_present(monkeypatch):
    calls = {}
    monkeypatch.setattr(retrieval, "_structured_lookup",
                        lambda db, req: calls.setdefault("structured", True))
    monkeypatch.setattr(retrieval, "_semantic_lookup",
                        lambda *a, **k: calls.setdefault("semantic", True))
    req = {"subject_id": "S", "paper": "P1", "year": 2019, "question_num": "2"}
    retrieval.get_context(None, req)
    assert calls.get("structured") and not calls.get("semantic")


def test_retrieval_uses_semantic_when_keys_missing(monkeypatch):
    calls = {}
    monkeypatch.setattr(retrieval, "_structured_lookup",
                        lambda db, req: calls.setdefault("structured", True))
    monkeypatch.setattr(retrieval, "_semantic_lookup",
                        lambda db, req, **k: calls.setdefault("semantic", True))
    req = {"subject_id": "S", "query": "nature of business"}
    retrieval.get_context(None, req, embed_fn=lambda t: [0.0] * 768)
    assert calls.get("semantic") and not calls.get("structured")


# ---------------------------------------------------------------------------
# controller.py
# ---------------------------------------------------------------------------
def test_controller_out_of_scope_makes_no_llm_or_embed_call():
    conn = open_test_db()
    seed(conn, locked=0)  # subject not locked
    try:
        def boom_chat(*a, **k):
            raise AssertionError("LLM called while out of scope")

        def boom_embed(*a, **k):
            raise AssertionError("embedding called while out of scope")

        out = controller.handle_request(
            conn,
            {"route": "teach", "subject_id": "Principles_of_Business", "query": "x"},
            chat_fn=boom_chat, embed_fn=boom_embed,
        )
        assert out == {"error": "out_of_scope"}
    finally:
        conn.close()


def test_controller_teach_happy_path(db, monkeypatch):
    monkeypatch.setattr(controller, "get_context", lambda db, req, embed_fn=None: {
        "objective_id": "POB-1.1",
        "chunk_text": "A business supplies goods and services.",
        "source_file": "notes.pdf",
        "page": 3,
    })

    def fake_chat(messages, system):
        return "A business supplies goods/services.\nExample: ...\nQ: What is a business?"

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": "Principles_of_Business", "query": "nature of business"},
        chat_fn=fake_chat, embed_fn=lambda t: [0.0] * 768,
    )
    assert out["route"] == "teach"
    assert out["objective_id"] == "POB-1.1"
    assert out["source_file"] == "notes.pdf" and out["page"] == 3
    assert "Q:" in out["lesson"]
