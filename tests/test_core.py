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

    # Evidence is >= 20 chars so the Stage 10 thin-evidence gate does not downgrade
    # the awarded points; the missed point is genuinely missed.
    valid_json = (
        '{"objective_id":"POB-1.1","question_id":"q1","points":['
        '{"mark_point_id":"mp1","awarded":true,"evidence":"the student named an organisation"},'
        '{"mark_point_id":"mp2","awarded":true,"evidence":"the answer supplies goods and services"},'
        '{"mark_point_id":"mp3","awarded":false,"evidence":"no purpose was mentioned at all"}]}'
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
# Stage 10 — confidence-aware grading
# ---------------------------------------------------------------------------
def _seed_mark_point(db, mark_point_id, question_id, marks_value=1):
    """Insert one mark point under POB-1.1 for a grade_answer test."""
    db.execute(
        "INSERT OR IGNORE INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES ('ms10', 'Principles_of_Business', 'mark_scheme', 'ms10.pdf', 'h10')",
    )
    db.execute(
        "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
        "point_text, marks_value, point_order) VALUES (?, 'POB-1.1', ?, 'ms10', 'point', ?, 1)",
        (mark_point_id, question_id, marks_value),
    )
    db.commit()


def test_A_weighted_scoring_uses_marks_value(db):
    """compute_score weights by DB marks_value: [1,2,1] missing the 2 -> 50%, not 67%."""
    grading = {"points": [
        {"mark_point_id": "m1", "awarded": True},
        {"mark_point_id": "m2", "awarded": False},   # the 2-mark point is missed
        {"mark_point_id": "m3", "awarded": True},
    ]}
    mark_points_db = [
        {"mark_point_id": "m1", "marks_value": 1},
        {"mark_point_id": "m2", "marks_value": 2},
        {"mark_point_id": "m3", "marks_value": 1},
    ]
    score = grade.compute_score(grading, mark_points_db)
    assert score["awarded"] == 2
    assert score["total"] == 4
    assert score["score_pct"] == 50           # weighted, NOT 67
    assert score["missed_points"] == ["m2"]


def test_B_evidence_too_thin_is_downgraded(db):
    """An awarded point with under-20-char evidence is auto-downgraded to missed."""
    _seed_mark_point(db, "mpB", "qB")
    thin_json = (
        '{"objective_id":"POB-1.1","question_id":"qB","confidence":80,"points":['
        '{"mark_point_id":"mpB","awarded":true,"evidence":"ok","confidence":80}]}'
    )
    result = grade.grade_answer(db, "qB", "a longer student answer here", chat_fn=lambda *a, **k: thin_json)
    assert result["points"][0]["awarded"] is False
    assert "mpB" in result["missed_points"]
    assert result["awarded"] == 0
    assert "[auto-downgraded" in result["points"][0]["evidence"]


def test_C_verbatim_echo_is_flagged_not_downgraded(db):
    """Evidence that echoes the answer verbatim (no connectors) is flagged, stays awarded."""
    _seed_mark_point(db, "mpC", "qC")
    student_answer = "the firm sells products to local customers every single day"
    # The evidence is a >=20-char substring of the answer with no explanation connector.
    echo_json = (
        '{"objective_id":"POB-1.1","question_id":"qC","confidence":90,"points":['
        '{"mark_point_id":"mpC","awarded":true,'
        '"evidence":"the firm sells products to local customers","confidence":90}]}'
    )
    result = grade.grade_answer(db, "qC", student_answer, chat_fn=lambda *a, **k: echo_json)
    assert result["points"][0]["awarded"] is True       # award stands
    assert "mpC" in result["review_flags"]              # but flagged for review
    assert "mpC" not in result["missed_points"]


def test_E_evidence_not_in_answer_is_flagged_not_downgraded(db):
    """Roadmap #1: awarded evidence absent from the student answer is flagged, not downgraded."""
    _seed_mark_point(db, "mpE", "qE")
    student_answer = "The business is owned by one person."
    # Evidence the student never wrote (>=20 chars, so the thin-evidence gate leaves
    # it awarded): a loose paraphrase / fabrication, not a substring of the answer.
    paraphrase_json = (
        '{"objective_id":"POB-1.1","question_id":"qE","confidence":85,"points":['
        '{"mark_point_id":"mpE","awarded":true,'
        '"evidence":"limited liability protects shareholders","confidence":85}]}'
    )
    result = grade.grade_answer(db, "qE", student_answer, chat_fn=lambda *a, **k: paraphrase_json)
    assert result["points"][0]["awarded"] is True       # NOT downgraded -- only flagged
    assert "mpE" in result["review_flags"]
    assert "mpE" not in result["missed_points"]
    assert result["awarded"] == 1                        # still counts in compute_score
    assert result["score_pct"] == 100


def test_D_examiner_prompt_has_command_word_gating():
    """prompts/examiner.txt carries the command-word + confidence + output sections."""
    text = (ROOT / "prompts" / "examiner.txt").read_text(encoding="utf-8")
    assert "EXPLAIN" in text       # command-word rules present
    assert "because" in text       # explanation-connector guidance present
    assert "CONFIDENCE" in text
    assert "OUTPUT FORMAT" in text


# ---------------------------------------------------------------------------
# grade_against_syllabus (syllabus-fallback grader) + controller fallback
# ---------------------------------------------------------------------------
SYLLABUS_GRADING_JSON = (
    '{"objective_id":"POB-1.1","question_id":"echoed-by-model","points":['
    '{"mark_point_id":"POB-1.1-syn-1","awarded":true,"evidence":"named a function"},'
    '{"mark_point_id":"POB-1.1-syn-2","awarded":true,"evidence":"gave an example"},'
    '{"mark_point_id":"POB-1.1-syn-3","awarded":false,"evidence":"no definition"}]}'
)


def test_grade_against_syllabus_returns_valid_dict(db):
    def fake_chat(messages, system, schema=None):
        return SYLLABUS_GRADING_JSON

    result = grade.grade_against_syllabus(
        db, "POB-1.1", "Explain the functions of a business.", "my answer",
        chat_fn=fake_chat,
    )
    # Same shape as grade_answer(): Python computes every number.
    assert result["objective_id"] == "POB-1.1"
    assert result["awarded"] == 2
    assert result["total"] == 3
    assert result["score_pct"] == 67
    assert result["missed_points"] == ["POB-1.1-syn-3"]


def test_grade_against_syllabus_synthetic_ids_match_pattern(db):
    import re

    def fake_chat(messages, system, schema=None):
        return SYLLABUS_GRADING_JSON

    result = grade.grade_against_syllabus(
        db, "POB-1.1", "stem", "answer", chat_fn=fake_chat,
    )
    for i, p in enumerate(result["points"], 1):
        assert p["mark_point_id"] == f"POB-1.1-syn-{i}"
        assert re.fullmatch(r"POB-1\.1-syn-\d+", p["mark_point_id"])


def test_grade_against_syllabus_unknown_objective(db):
    def fake_chat(messages, system, schema=None):  # must never be reached
        raise AssertionError("LLM called for an unknown objective")

    result = grade.grade_against_syllabus(
        db, "POB-9.9", "stem", "answer", chat_fn=fake_chat,
    )
    assert result == {"error": "unknown_objective"}


def test_controller_grade_falls_back_to_syllabus_when_no_mark_points(db):
    # A past-paper chunk carries the objective FK and stem but has NO mark_points,
    # so the grade route must fall back to grade_against_syllabus.
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        ("doc_pp", "Principles_of_Business", "past_paper", "pp.pdf", "hpp"),
    )
    db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, question_num, chunk_id) "
        "VALUES ('doc_pp', 'POB-1.1', 'Principles_of_Business', ?, '1a', ?)",
        ("Explain two functions of a business.", "POB-pp-q1a-stem"),
    )
    db.commit()

    def fake_chat(messages, system, schema=None):
        return SYLLABUS_GRADING_JSON

    def boom_embed(*a, **k):
        raise AssertionError("embedding called during structured grade fallback")

    out = controller.handle_request(
        db,
        {"route": "grade", "subject_id": "Principles_of_Business",
         "question_id": "POB-pp-q1a-stem", "student_answer": "an answer"},
        chat_fn=fake_chat, embed_fn=boom_embed,
    )
    assert out["objective_id"] == "POB-1.1"
    assert out["question_id"] == "POB-pp-q1a-stem"  # real id kept on the result
    assert out["score_pct"] == 67
    assert [p["mark_point_id"] for p in out["points"]] == [
        "POB-1.1-syn-1", "POB-1.1-syn-2", "POB-1.1-syn-3",
    ]
    # Weakness logged against the same objective_id (the FK is never lost).
    row = db.execute(
        "SELECT objective_id, score_pct FROM weakness_log WHERE objective_id = 'POB-1.1'"
    ).fetchone()
    assert row is not None and row["score_pct"] == 67


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
        raise AssertionError("teach must not generate a lesson at runtime")

    out = controller.handle_request(
        db,
        {"route": "teach", "subject_id": "Principles_of_Business", "query": "nature of business"},
        chat_fn=fake_chat, embed_fn=lambda t: [0.0] * 768,
    )
    assert out["route"] == "teach"
    assert out["objective_id"] == "POB-1.1"
    # No canonical lesson for this objective -> honest placeholder, no runtime
    # generation. source_file/page are None (nothing is grounded behind a placeholder).
    assert out["lesson_source"] == "placeholder"
    assert out["recall_questions"] == []
    assert out["source_file"] is None and out["page"] is None
