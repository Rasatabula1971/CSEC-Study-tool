"""
tests/test_api.py
=================
Stage 6 tests for the FastAPI layer. app.state.db and controller.handle_request
are mocked throughout, so these tests need no SSD, no real database, and no Ollama.

The app's lifespan (SSD check + DB open) is NOT triggered: a plain TestClient
(used without the `with` context manager) does not run lifespan events, so we
set app.state.db ourselves and patch the controller/health hooks.

Run: pytest tests/test_api.py -v
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module  # noqa: E402
import controller  # noqa: E402
import notes as notes_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.state.db = MagicMock()
    return TestClient(app_module.app)


# ---------------------------------------------------------------------------
# Runtime DB durability: open_db must put the connection in WAL mode so an
# unclean process kill (taskkill /F -- the lifespan's finally: db.close() never
# runs) cannot corrupt the DB and committed writes survive on the next open.
# ---------------------------------------------------------------------------
def test_open_db_uses_wal_journal_mode(tmp_path):
    db_path = tmp_path / "wal_check.sqlite"
    db = app_module.open_db(str(db_path))
    try:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        sync = db.execute("PRAGMA synchronous").fetchone()[0]
        assert str(mode).lower() == "wal"
        assert sync == 1  # 1 == NORMAL, the recommended WAL companion
    finally:
        db.close()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
def test_health_returns_200(client, monkeypatch):
    monkeypatch.setattr(app_module, "ollama_health", lambda: False)  # no network wait
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body == {"status": "ok", "ollama": False, "db": True}


def test_health_200_when_ollama_unreachable_cold_start(monkeypatch):
    """launch.bat polls /health to detect FastAPI readiness on cold start.

    Ollama may not be up yet (or model still loading) at the moment the first
    poll fires.  /health must return 200 with ollama=False rather than raising
    or returning a non-200 — the handler must never let an Ollama failure
    propagate as an HTTP error.
    """
    monkeypatch.setattr(app_module, "ollama_health", lambda: False)
    app_module.app.state.db = MagicMock()
    client = TestClient(app_module.app)
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["ollama"] is False
    assert body["db"] is True


def test_health_200_before_db_initialised(monkeypatch):
    """If /health is hit in the narrow window before the DB is opened, it must
    still return 200 — db=False is the correct degraded response, not a 500."""
    monkeypatch.setattr(app_module, "ollama_health", lambda: False)
    # Simulate no db attribute on app.state yet
    if hasattr(app_module.app.state, "db"):
        del app_module.app.state.db
    client = TestClient(app_module.app)
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["db"] is False


# ---------------------------------------------------------------------------
# /api/status  (grading-engine indicator -- driven by CLOUD_MODE)
# ---------------------------------------------------------------------------
def test_status_offline_mode_reports_ollama_no_ping(client, monkeypatch):
    """CLOUD_MODE=0: grading_engine 'ollama', gemini_available False, NO Gemini ping.

    is_gemini_available must not even be called in offline mode -- the status must
    report the truth without touching the cloud.
    """
    monkeypatch.setenv("CLOUD_MODE", "0")
    monkeypatch.setattr(app_module, "ollama_health", lambda: True)
    ping = MagicMock()
    monkeypatch.setattr(app_module, "is_gemini_available", ping)
    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json()
    assert body["cloud_mode"] is False
    assert body["gemini_available"] is False
    assert body["grading_engine"] == "ollama"
    assert body["ollama"] is True and body["db"] is True
    ping.assert_not_called()  # no cloud reachability call in offline mode


def test_status_cloud_mode_reachable_reports_gemini(client, monkeypatch):
    """CLOUD_MODE=1 and Gemini reachable -> grading_engine 'gemini', available True."""
    monkeypatch.setenv("CLOUD_MODE", "1")
    monkeypatch.setattr(app_module, "ollama_health", lambda: True)
    monkeypatch.setattr(app_module, "is_gemini_available", lambda: True)
    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json()
    assert body["cloud_mode"] is True
    assert body["gemini_available"] is True
    assert body["grading_engine"] == "gemini"


def test_status_cloud_mode_unreachable_reports_truth(client, monkeypatch):
    """CLOUD_MODE=1 but Gemini unreachable -> 200, grading_engine 'gemini',
    gemini_available False (honestly: configured for cloud, currently down)."""
    monkeypatch.setenv("CLOUD_MODE", "1")
    monkeypatch.setattr(app_module, "ollama_health", lambda: True)
    monkeypatch.setattr(app_module, "is_gemini_available", lambda: False)
    res = client.get("/api/status")
    assert res.status_code == 200
    body = res.json()
    assert body["cloud_mode"] is True
    assert body["gemini_available"] is False
    assert body["grading_engine"] == "gemini"


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------
def test_chat_valid_payload_returns_json(client, monkeypatch):
    captured = {}

    def fake_handle(db, req, *args, **kwargs):
        captured["req"] = req
        return {"route": "teach", "objective_id": "POB-1.1", "lesson": "A business..."}

    monkeypatch.setattr(app_module, "handle_request", fake_handle)

    res = client.post("/api/chat", json={
        "message": "explain business",
        "subject_id": "Principles_of_Business",
        "route": "teach",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["objective_id"] == "POB-1.1"
    # the message is mapped onto the controller's request shape
    assert captured["req"]["query"] == "explain business"
    assert captured["req"]["student_answer"] == "explain business"
    assert captured["req"]["route"] == "teach"


def test_chat_missing_subject_id_returns_422(client):
    res = client.post("/api/chat", json={"message": "hi", "route": "teach"})
    assert res.status_code == 422


def test_chat_empty_message_returns_422(client):
    res = client.post("/api/chat", json={
        "message": "",
        "subject_id": "Principles_of_Business",
        "route": "teach",
    })
    assert res.status_code == 422


def test_chat_plan_result_is_aliased_for_ui(client, monkeypatch):
    """The UI reads `objectives`; the controller returns `tasks`. app.py bridges."""
    monkeypatch.setattr(app_module, "handle_request", lambda db, req, *a, **k: {
        "route": "plan", "subject_id": "Principles_of_Business", "due_count": 1,
        "tasks": [{"objective_id": "POB-1.1", "leitner_box": 1, "next_review": "2026-06-14"}],
    })
    res = client.post("/api/chat", json={
        "message": "(plan)", "subject_id": "Principles_of_Business", "route": "plan",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["objectives"] == body["tasks"]  # alias present, original kept


# ---------------------------------------------------------------------------
# GET /api/subjects and /api/due/{subject_id}
# ---------------------------------------------------------------------------
def test_subjects_returns_list(client):
    row = {"subject_id": "Principles_of_Business", "display_name": "Principles of Business"}
    app_module.app.state.db.execute.return_value.fetchall.return_value = [row]
    res = client.get("/api/subjects")
    assert res.status_code == 200
    assert res.json() == [row]


def test_subjects_empty_is_ok(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/subjects")
    assert res.status_code == 200
    assert res.json() == []


def test_due_returns_list(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/due/Principles_of_Business")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


# ---------------------------------------------------------------------------
# GET /api/questions/{subject_id}
# ---------------------------------------------------------------------------
def test_questions_returns_labelled_list(client):
    row = {
        "question_id": "POB-2026Jan-P2-q1a",
        "objective_id": "POB-1.14",
        "question_text": "List THREE careers ... (3 marks)",
        "question_num": "1(a)",
        "paper": "Paper 2 - January 2026",
        "year": 2026,
        "marks": 3,
    }
    app_module.app.state.db.execute.return_value.fetchall.return_value = [row]
    res = client.get("/api/questions/Principles_of_Business")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    q = body[0]
    assert q["question_id"] == "POB-2026Jan-P2-q1a"   # the key grade.py needs
    assert q["marks"] == 3
    assert q["label"] == "Paper 2 - January 2026 · Q1(a)"  # built for display


def test_questions_empty_is_ok(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/questions/Principles_of_Business")
    assert res.status_code == 200
    assert res.json() == []


def _real_db() -> sqlite3.Connection:
    """A real in-memory DB (schema.sql + sqlite-vec) so the actual SQL join runs.

    The other /api/questions tests mock app.state.db, which never exercises the
    join. This one must, to prove the '-stem' join fix end-to-end.
    """
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping real-DB API test")
    # check_same_thread=False: TestClient runs the sync endpoint in a worker thread.
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    return conn


def test_questions_join_matches_stem_chunk_id():
    """ingest_solutions stores mark_points.question_id == chunk_id (already '-stem').

    The grade picker joins on equality, not by appending '-stem'. This also proves
    the apply_runtime_migrations() data fix: an OLD-convention mark_point (no '-stem')
    is normalised at startup so it, too, joins to its '-stem' chunk and returns real
    question_text.

    Note on ordering: apply_runtime_migrations() runs AFTER the legacy row is seeded
    -- exactly as it does in production (startup runs against an existing DB whose
    rows already exist). Running it before seeding would find 0 rows and could not
    migrate a row that does not yet exist.
    """
    conn = _real_db()
    try:
        conn.execute(
            "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
            "VALUES ('Principles_of_Business', 'Principles of Business', 1)"
        )
        conn.execute(
            "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
            "VALUES ('POB-SEC-1', 'Principles_of_Business', 'Nature of Business', '1')"
        )
        conn.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt) "
            "VALUES ('POB-1.1', 'POB-SEC-1', 'Principles_of_Business', '1.1', 'Define the term business.')"
        )
        conn.execute(
            "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, source_file, content_hash) "
            "VALUES ('sol-1', 'Principles_of_Business', 'mark_scheme', 'Paper 2 - June 2024', 2024, "
            "'june2024.txt', 'hash-1')"
        )
        # Two chunks, both with '-stem' chunk_ids (the canonical convention).
        conn.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, question_num, chunk_id) "
            "VALUES ('sol-1', 'POB-1.1', 'Principles_of_Business', "
            "'Define the term business and give one example.', '1', 'POB-1.1-June2024-q1-stem')"
        )
        conn.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, question_num, chunk_id) "
            "VALUES ('sol-1', 'POB-1.1', 'Principles_of_Business', "
            "'State two functions of an entrepreneur.', '2', 'POB-1.1-June2024-q2-stem')"
        )
        # New-convention mark_point: question_id already ends in '-stem'.
        conn.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, point_text, "
            "marks_value, point_order) VALUES ('POB-1.1-June2024-q1-stem-mp1', 'POB-1.1', "
            "'POB-1.1-June2024-q1-stem', 'sol-1', 'An organisation supplying goods or services.', 1, 1)"
        )
        # OLD-convention mark_point: question_id WITHOUT '-stem'. Its chunk_id is the
        # question_id + '-stem'; the migration must normalise it for the join to hit.
        conn.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, point_text, "
            "marks_value, point_order) VALUES ('POB-1.1-June2024-q2-mp1', 'POB-1.1', "
            "'POB-1.1-June2024-q2', 'sol-1', 'Organising the factors of production.', 1, 1)"
        )
        conn.commit()

        # Startup migration normalises the legacy row's question_id to '-stem'.
        app_module.apply_runtime_migrations(conn)
        migrated = conn.execute(
            "SELECT question_id FROM mark_points WHERE mark_point_id = 'POB-1.1-June2024-q2-mp1'"
        ).fetchone()["question_id"]
        assert migrated == "POB-1.1-June2024-q2-stem"   # the UPDATE ran

        app_module.app.state.db = conn
        res = TestClient(app_module.app).get("/api/questions/Principles_of_Business")
        assert res.status_code == 200
        body = res.json()
        assert len(body) == 2                              # both questions returned
        by_qid = {item["question_id"]: item for item in body}

        new = by_qid["POB-1.1-June2024-q1-stem"]
        assert new["question_text"] is not None            # was None before the join fix
        assert new["question_num"] == "1"

        # The migrated legacy row now joins to its '-stem' chunk and has real text.
        legacy = by_qid["POB-1.1-June2024-q2-stem"]
        assert legacy["question_text"] is not None
        assert legacy["question_num"] == "2"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /quiz  and  GET /api/questions?subject_id=...  (quiz page additions)
# ---------------------------------------------------------------------------
def test_quiz_page_returns_200(client):
    res = client.get("/quiz")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_plan_page_returns_200(client):
    """The standalone Study Plan page is served at /plan."""
    res = client.get("/plan")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_quiz_page_no_study_plan_pill(client):
    """quiz.html now offers only Past Paper | Syllabus Practice -- no Study Plan
    pill. (A 'Study Plan →' nav link in the topbar is fine; the mode selector
    must not contain a Study Plan mode.)"""
    res = client.get("/quiz")
    assert res.status_code == 200
    html = res.text
    # Isolate the mode selector (.mode-toggle block) and assert no Study Plan mode.
    start = html.index('class="mode-toggle"')
    end = html.index("</div>", start)
    mode_selector = html[start:end]
    assert "Study Plan" not in mode_selector
    assert "modePlan" not in mode_selector
    # Sanity: the two intended modes remain.
    assert "Past Paper" in mode_selector
    assert "Syllabus Practice" in mode_selector


def test_questions_query_param_returns_list(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/questions", params={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    assert res.json() == []
    assert isinstance(res.json(), list)


# ---------------------------------------------------------------------------
# GET /api/filters?subject_id=...
# ---------------------------------------------------------------------------
def test_filters_returns_papers_and_years(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/filters", params={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    body = res.json()
    assert "papers" in body and "years" in body
    assert isinstance(body["papers"], list)
    assert isinstance(body["years"], list)


# ---------------------------------------------------------------------------
# GET /api/sections?subject_id=...  (quiz-page Syllabus Practice mode)
# ---------------------------------------------------------------------------
def test_sections_returns_list(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/sections", params={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    assert isinstance(res.json(), list)


# ---------------------------------------------------------------------------
# POST /api/chat  route="practice"
# ---------------------------------------------------------------------------
def test_chat_practice_returns_practice_question_id(client, monkeypatch):
    """A practice turn returns a generated question whose id is prefixed 'practice-'."""
    monkeypatch.setattr(app_module, "handle_request", lambda db, req, *a, **k: {
        "route": "practice",
        "question_id": "practice-POB-1.1-20260613090000000000",
        "question_num": "Practice", "paper": "Syllabus Practice", "year": None,
        "stem": "Explain the functions of a business.", "marks_total": None,
        "objective_id": "POB-1.1",
    })
    res = client.post("/api/chat", json={
        "message": "(practice)", "subject_id": "Principles_of_Business",
        "route": "practice", "objective_id": "POB-1.1",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["question_id"].startswith("practice-")
    assert body["objective_id"] == "POB-1.1"
    assert body["paper"] == "Syllabus Practice"


# ---------------------------------------------------------------------------
# Study Plan endpoints
# ---------------------------------------------------------------------------
def test_plan_start_batch_returns_objectives_and_batch_id(client, monkeypatch):
    objectives = [{"objective_id": f"POB-1.{i}", "content_stmt": f"o{i}"} for i in range(1, 6)]
    monkeypatch.setattr(app_module, "handle_request", lambda db, req, *a, **k: {
        "route": "start_batch", "batch_id": 7, "subject_id": "Principles_of_Business",
        "objectives": objectives,
        "progress": {"total": 87, "mastered": 0, "met_once": 0, "in_progress": 0,
                     "unmet": 87, "percent_mastered": 0},
    })
    res = client.post("/api/plan/start_batch", json={"subject_id": "Principles_of_Business"})
    assert res.status_code == 200
    body = res.json()
    assert body["batch_id"] == 7
    assert len(body["objectives"]) == 5
    assert body["progress"]["total"] == 87


def test_plan_progress_returns_progress_dict(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = [
        {"status": "mastered", "c": 23},
        {"status": "unmet", "c": 64},
    ]
    res = client.get("/api/plan/progress/Principles_of_Business")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 87
    assert body["mastered"] == 23
    assert body["unmet"] == 64
    assert body["percent_mastered"] == 26
    assert set(body) >= {"total", "mastered", "met_once", "in_progress", "unmet", "percent_mastered"}


def test_explain_missed_returns_feedback(client, monkeypatch):
    """A request with missed points returns 200 and a feedback string."""
    captured = {}

    def fake_handle(db, req, *a, **k):
        captured["req"] = req
        return {"feedback": "You should have said money did not exist yet, so people swapped goods."}

    monkeypatch.setattr(app_module, "handle_request", fake_handle)
    res = client.post("/api/plan/explain_missed", json={
        "subject_id": "Principles_of_Business",
        "objective_id": "POB-1.1",
        "missed_points": [
            {"mark_point_id": "POB-1.1-syn-1", "expected": "no money existed yet",
             "evidence": "not mentioned"},
        ],
    })
    assert res.status_code == 200
    body = res.json()
    assert "feedback" in body and body["feedback"]
    assert captured["req"]["route"] == "explain_missed"
    assert captured["req"]["objective_id"] == "POB-1.1"
    assert len(captured["req"]["missed_points"]) == 1


def test_explain_missed_empty_returns_empty_without_llm(client, monkeypatch):
    """Empty missed_points short-circuits to {"feedback": ""} with NO LLM call.

    Runs the REAL controller but injects a chat_fn that raises -- if the empty-list
    branch ever reached the model, this would surface as an error, not a clean 200.
    """
    def boom_chat(*a, **k):
        raise AssertionError("LLM must not be called for empty missed_points")

    def real_handle_no_llm(db, req, *a, **k):
        return controller.handle_request(db, req, chat_fn=boom_chat)

    monkeypatch.setattr(app_module, "handle_request", real_handle_no_llm)
    res = client.post("/api/plan/explain_missed", json={
        "subject_id": "Principles_of_Business",
        "objective_id": "POB-1.1",
        "missed_points": [],
    })
    assert res.status_code == 200
    assert res.json() == {"feedback": ""}


# ---------------------------------------------------------------------------
# Welcome page routing  (GET /  and  GET /chat)
# ---------------------------------------------------------------------------
def test_root_serves_first_launch_when_unseen(client):
    """UI overhaul session 2: GET / serves the one-time first-launch message until the
    welcome flag is set. The `client` fixture's MagicMock db reports the flag unset, so
    the first-launch page is served (a stable marker from its copy)."""
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "next best thing" in res.text   # first_launch.html marker


def test_welcome_page_moved_to_welcome_path(client):
    """The redesigned Welcome page is served at /welcome (and at / once seen)."""
    res = client.get("/welcome")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Continue studying" in res.text   # a Welcome-page-only marker


def test_chat_page_served_at_chat_path(client):
    """The chat UI moved from / to /chat."""
    res = client.get("/chat")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


# ---------------------------------------------------------------------------
# GET /api/objectives/{subject_id}
# ---------------------------------------------------------------------------
def test_objectives_returns_list(client):
    row = {"objective_id": "POB-1.1", "content_stmt": "Define a business",
           "objective_num": "1.1", "section_title": "Nature of Business",
           "section_num": "1"}
    app_module.app.state.db.execute.return_value.fetchall.return_value = [row]
    res = client.get("/api/objectives/Principles_of_Business")
    assert res.status_code == 200
    body = res.json()
    assert len(body) == 1
    assert body[0]["objective_id"] == "POB-1.1"


def test_objectives_empty_is_ok(client):
    app_module.app.state.db.execute.return_value.fetchall.return_value = []
    res = client.get("/api/objectives/Principles_of_Business")
    assert res.status_code == 200
    assert res.json() == []


# ---------------------------------------------------------------------------
# POST /api/notes/classify
# ---------------------------------------------------------------------------
def test_notes_classify_returns_subject_and_objectives(client, monkeypatch):
    """A classify call returns subject_id, confidence, reasoning, suggested_objectives.

    The LLM (chat_fn) and embeddings (embed_fn) are stubbed so no Ollama is needed;
    the objective ranking runs the real deterministic cosine pass over mock rows.
    """
    notes_module._OBJ_EMBED_CACHE.clear()
    monkeypatch.setattr(app_module, "ollama_chat", lambda msgs, system, schema=None:
                        '{"subject_id": "Principles_of_Business", '
                        '"confidence": "high", "reasoning": "Business ownership."}')
    monkeypatch.setattr(app_module, "ollama_embed", lambda text: [0.1, 0.2, 0.3])

    objectives = [
        {"objective_id": "POB-2.1", "content_stmt": "Types of business ownership"},
        {"objective_id": "POB-2.2", "content_stmt": "Advantages of sole trader"},
    ]
    app_module.app.state.db.execute.return_value.fetchall.return_value = objectives

    res = client.post("/api/notes/classify", json={
        "text": "A sole trader is a business owned by one person...",
        "available_subjects": ["Principles_of_Business"],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["subject_id"] == "Principles_of_Business"
    assert body["confidence"] == "high"
    assert "suggested_objectives" in body
    assert len(body["suggested_objectives"]) == 2
    assert body["suggested_objectives"][0]["objective_id"] in ("POB-2.1", "POB-2.2")


def test_notes_classify_null_subject_falls_back(client, monkeypatch):
    """An LLM subject not in available_subjects collapses to subject_id=None, [] objs."""
    notes_module._OBJ_EMBED_CACHE.clear()
    monkeypatch.setattr(app_module, "ollama_chat", lambda msgs, system, schema=None:
                        '{"subject_id": null, "confidence": "low", "reasoning": "Unclear."}')
    monkeypatch.setattr(app_module, "ollama_embed", lambda text: [0.1, 0.2, 0.3])

    res = client.post("/api/notes/classify", json={
        "text": "Some ambiguous text",
        "available_subjects": ["Principles_of_Business"],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["subject_id"] is None
    assert body["suggested_objectives"] == []


# ---------------------------------------------------------------------------
# POST /api/notes/upload
# ---------------------------------------------------------------------------
def test_notes_upload_text_creates_chunks(client, monkeypatch):
    """Uploading pasted text under a confirmed objective returns chunks_created > 0."""
    monkeypatch.setattr(app_module, "ollama_embed", lambda text: [0.0, 0.0, 0.0])
    # save_notes validates the objective belongs to the subject; MagicMock fetchone
    # is truthy by default, so the validation passes.
    res = client.post("/api/notes/upload", data={
        "subject_id": "Principles_of_Business",
        "objective_id": "POB-2.1",
        "text": "A sole trader is a business owned and controlled by one person.",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["objective_id"] == "POB-2.1"
    assert body["chunks_created"] > 0
    assert body["doc_id"].startswith("notes-")


def test_notes_upload_rejects_unknown_objective(client, monkeypatch):
    """An objective not in the subject is refused (400) -- no chunk indexed unmapped."""
    monkeypatch.setattr(app_module, "ollama_embed", lambda text: [0.0, 0.0, 0.0])
    app_module.app.state.db.execute.return_value.fetchone.return_value = None
    res = client.post("/api/notes/upload", data={
        "subject_id": "Principles_of_Business",
        "objective_id": "NOPE-9.9",
        "text": "Some notes.",
    })
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/notes/classify_file  and  /api/notes/upload (file path)
# ---------------------------------------------------------------------------
def test_notes_classify_file_txt_returns_subject_and_objectives(client, monkeypatch):
    """A TXT upload is extracted server-side, then classified like /classify.

    First db.execute().fetchall() returns the locked-subject list (so the LLM's
    subject is accepted); the second returns the subject's objectives for the
    deterministic cosine ranking. extracted_text_length reflects the file text.
    """
    notes_module._OBJ_EMBED_CACHE.clear()
    monkeypatch.setattr(app_module, "ollama_chat", lambda msgs, system, schema=None:
                        '{"subject_id": "Principles_of_Business", '
                        '"confidence": "high", "reasoning": "Business ownership."}')
    monkeypatch.setattr(app_module, "ollama_embed", lambda text: [0.1, 0.2, 0.3])

    app_module.app.state.db.execute.return_value.fetchall.side_effect = [
        [{"subject_id": "Principles_of_Business"}],                       # locked subjects
        [{"objective_id": "POB-2.1", "content_stmt": "Types of business ownership"},
         {"objective_id": "POB-2.2", "content_stmt": "Advantages of sole trader"}],  # objectives
    ]

    content = b"A sole trader is a business owned and controlled by one person."
    res = client.post("/api/notes/classify_file",
                      files={"file": ("notes.txt", content, "text/plain")})
    assert res.status_code == 200
    body = res.json()
    assert body["subject_id"] == "Principles_of_Business"
    assert body["confidence"] == "high"
    assert len(body["suggested_objectives"]) == 2
    assert body["extracted_text_length"] == len(content)


def test_notes_classify_file_unsupported_type_returns_400(client):
    """An unsupported extension is rejected with a 400, never a 500."""
    res = client.post("/api/notes/classify_file",
                      files={"file": ("data.xlsx", b"\x00\x01", "application/octet-stream")})
    assert res.status_code == 400


def test_notes_upload_file_creates_chunks(client, monkeypatch):
    """Uploading a TXT file (multipart) under a confirmed objective indexes chunks."""
    monkeypatch.setattr(app_module, "ollama_embed", lambda text: [0.0, 0.0, 0.0])
    # save_notes validates the objective belongs to the subject; MagicMock fetchone
    # is truthy by default, so the validation passes.
    content = b"A sole trader is a business owned and controlled by one person."
    res = client.post(
        "/api/notes/upload",
        data={"subject_id": "Principles_of_Business", "objective_id": "POB-2.1"},
        files={"file": ("notes.txt", content, "text/plain")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["objective_id"] == "POB-2.1"
    assert body["chunks_created"] > 0
    assert body["doc_id"].startswith("notes-")


# ---------------------------------------------------------------------------
# Offline-first routing (v3.0 PDR): grading must never reach the cloud.
# ---------------------------------------------------------------------------
def test_grade_route_uses_ollama_only_never_gemini(monkeypatch):
    """POST /api/chat route='grade' calls ollama_chat once and NO gemini_client fn.

    Drives the REAL controller against a real in-memory DB, seeded so the grade
    falls to the syllabus grader -- the exact path that used to prefer Gemini
    (controller grade_fn -> chat_for_grading). The fix routes it to Ollama only,
    so:
      * llm_router.ollama_chat (what chat_for_grading now calls) fires exactly once,
      * gemini_client.is_gemini_available and gemini_client.gemini_chat are never
        called (asserted via unittest.mock.patch).
    """
    from unittest.mock import patch

    import gemini_client  # noqa: E402  -- patched, must not be called
    import llm_router  # noqa: E402  -- chat_for_grading looks up ollama_chat here

    monkeypatch.setenv("CLOUD_MODE", "0")  # offline mode: grading is Ollama-only

    conn = _real_db()
    try:
        conn.execute(
            "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
            "VALUES ('Principles_of_Business', 'Principles of Business', 1)"
        )
        conn.execute(
            "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
            "VALUES ('POB-SEC-1', 'Principles_of_Business', 'Nature of Business', '1')"
        )
        conn.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
            "content_stmt, command_words) VALUES ('POB-1.1', 'POB-SEC-1', "
            "'Principles_of_Business', '1.1', 'Define the term business.', '[\"Define\"]')"
        )
        conn.execute(
            "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
            "source_file, content_hash) VALUES ('pp-1', 'Principles_of_Business', "
            "'past_paper', 'Paper 2 - June 2024', 2024, 'june2024.txt', 'hash-grade-1')"
        )
        # A question chunk with NO mark_points -> grade falls to grade_against_syllabus,
        # which routes through grade_fn (chat_for_grading) -- the path under test.
        conn.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, "
            "question_num, chunk_id) VALUES ('pp-1', 'POB-1.1', 'Principles_of_Business', "
            "'Define the term business.', '1', 'POB-1.1-grade-q1')"
        )
        conn.commit()
        app_module.apply_runtime_migrations(conn)
        app_module.app.state.db = conn

        graded_json = (
            '{"objective_id": "POB-1.1", "question_id": "POB-1.1-grade-q1", '
            '"points": [{"mark_point_id": "POB-1.1-syn-1", "awarded": true, '
            '"evidence": "Student stated a business supplies goods and services."}]}'
        )

        with patch.object(llm_router, "ollama_chat",
                          return_value=graded_json) as mock_ollama, \
             patch.object(gemini_client, "is_gemini_available") as mock_is_avail, \
             patch.object(gemini_client, "gemini_chat") as mock_gemini_chat:
            res = TestClient(app_module.app).post("/api/chat", json={
                "message": "A business supplies goods and services to customers.",
                "subject_id": "Principles_of_Business",
                "route": "grade",
                "question_id": "POB-1.1-grade-q1",
            })

        assert res.status_code == 200
        body = res.json()
        assert body["score_pct"] == 100            # Python scored it, not the model
        assert body["objective_id"] == "POB-1.1"

        # The grading call went to Ollama exactly once...
        assert mock_ollama.call_count == 1
        # ...and the cloud path was never touched.
        mock_is_avail.assert_not_called()
        mock_gemini_chat.assert_not_called()
    finally:
        conn.close()


def test_plan_grade_batch_routes_synthesis_question(client, monkeypatch):
    captured = {}

    def fake_handle(db, req, *args, **kwargs):
        captured["req"] = req
        return {"route": "grade_batch_question", "is_synthesis": True,
                "score_pct": 80, "awarded": 4, "total": 5, "points": [],
                "progress": {"total": 87, "mastered": 0, "met_once": 5,
                             "in_progress": 0, "unmet": 82, "percent_mastered": 0}}

    monkeypatch.setattr(app_module, "handle_request", fake_handle)
    res = client.post("/api/plan/grade_batch", json={
        "batch_id": 7, "question_id": "synthesis-7", "answer": "a connected answer here",
    })
    assert res.status_code == 200
    # the endpoint forwards a grade_batch_question route with the synthesis id
    assert captured["req"]["route"] == "grade_batch_question"
    assert captured["req"]["question_id"] == "synthesis-7"
    assert captured["req"]["batch_id"] == 7
    body = res.json()
    assert body["is_synthesis"] is True
    assert body["progress"]["met_once"] == 5


# ---------------------------------------------------------------------------
# Stage 13 panel-shell endpoints (real in-memory DB, not a MagicMock)
# ---------------------------------------------------------------------------
import sqlite_vec  # noqa: E402

SCHEMA_PATH_S13 = ROOT / "backend" / "db" / "schema.sql"
SUBJECT_S13 = "Principles_of_Business"


def _open_real_db():
    # check_same_thread=False: the TestClient runs sync endpoints in a worker thread.
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH_S13.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    app_module.apply_runtime_migrations(db)
    return db


def _seed_panel_data(db):
    """Subject + 1 section + 2 objectives; one with a lesson, a doc with a '-stem'
    question + mark points, a weakness_log row, and one negative feedback."""
    db.execute("INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, 'Principles of Business', 1)", (SUBJECT_S13,))
    db.execute("INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) VALUES ('S1', ?, 'Nature of Business', '1')", (SUBJECT_S13,))
    db.execute("INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt, command_words, skill_type) VALUES ('POB-1.1','S1',?,'1.1','Explain the concept of a business','[\"Explain\"]','Understanding')", (SUBJECT_S13,))
    db.execute("INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, content_stmt, command_words, skill_type) VALUES ('POB-1.2','S1',?,'1.2','State the functions of a business','[\"State\"]','Knowledge')", (SUBJECT_S13,))
    # POB-1.1 has a canonical lesson; POB-1.2 does not.
    db.execute("INSERT INTO objective_lessons (lesson_id, objective_id, subject_id, lesson_text, recall_questions, source_chunk_ids, confidence) VALUES ('L1','POB-1.1',?,'lesson body','[\"Q1?\"]','[\"c1\"]',85)", (SUBJECT_S13,))
    # A gradeable paper: doc + '-stem' question chunk + mark points.
    db.execute("INSERT INTO documents (doc_id, subject_id, content_type, paper, year, source_file, content_hash) VALUES ('D1',?,'mark_scheme','Paper 2 - June 2024',2024,'d1.pdf','d1h')", (SUBJECT_S13,))
    db.execute("INSERT INTO documents (doc_id, subject_id, content_type, paper, year, source_file, content_hash) VALUES ('D2',?,'mark_scheme','Paper 2 - June 2023',2023,'d2.pdf','d2h')", (SUBJECT_S13,))
    db.execute("INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, question_num, chunk_id) VALUES ('D1','POB-1.1',?,'Explain two functions of a business. (6 marks)','2b','POB-1.1-q2b-stem')", (SUBJECT_S13,))
    db.execute("INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, question_num, chunk_id) VALUES ('D2','POB-1.2',?,'State three functions. (3 marks)','1a','POB-1.2-q1a-stem')", (SUBJECT_S13,))
    for i in range(1, 4):
        db.execute("INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, point_text, marks_value, point_order, source_type) VALUES (?,?,?,?,?,?,?, 'past_paper')",
                   (f"POB-1.1-q2b-mp{i}", "POB-1.1", "POB-1.1-q2b-stem", "D1", f"point {i}", 2, i))
    # POB-1.1 has weakness + a session; POB-1.2 has neither (null values in progress).
    db.execute("INSERT INTO weakness_log (objective_id, subject_id, score_pct, leitner_box, next_review) VALUES ('POB-1.1',?,60,2,'2026-06-20')", (SUBJECT_S13,))
    db.execute("INSERT INTO study_sessions (subject_id, objective_id, mode, outcome, score_pct, created_at) VALUES (?, 'POB-1.1','grade','fail',60,'2026-06-17 10:00:00')", (SUBJECT_S13,))
    db.execute("INSERT INTO user_feedback (objective_id, subject_id, feedback_type, sentiment) VALUES ('POB-1.1',?,'lesson','negative')", (SUBJECT_S13,))
    db.commit()
    app_module.apply_runtime_migrations(db)  # backfill source_rank for the new mark points


@pytest.fixture
def real_client():
    db = _open_real_db()
    _seed_panel_data(db)
    app_module.app.state.db = db
    yield TestClient(app_module.app)
    db.close()


def test_syllabus_endpoint_returns_tree(real_client):
    res = real_client.get(f"/api/syllabus/{SUBJECT_S13}")
    assert res.status_code == 200
    body = res.json()
    assert body["subject_id"] == SUBJECT_S13
    assert body["display_name"] == "Principles of Business"
    assert len(body["sections"]) == 1
    objs = body["sections"][0]["objectives"]
    by_id = {o["objective_id"]: o for o in objs}
    # every objective carries the three derived fields
    for o in objs:
        assert "has_lesson" in o and "mark_point_count" in o and "best_source_rank" in o
    assert by_id["POB-1.1"]["has_lesson"] is True
    assert by_id["POB-1.1"]["mark_point_count"] == 3
    assert by_id["POB-1.1"]["best_source_rank"] == 3       # past_paper -> 3
    assert by_id["POB-1.1"]["command_words"] == ["Explain"]
    assert by_id["POB-1.2"]["has_lesson"] is False
    assert by_id["POB-1.2"]["mark_point_count"] == 0
    assert by_id["POB-1.2"]["best_source_rank"] is None    # no mark points


def test_progress_endpoint_includes_all_objectives(real_client):
    res = real_client.get(f"/api/progress/{SUBJECT_S13}")
    assert res.status_code == 200
    objs = {o["objective_id"]: o for o in res.json()["objectives"]}
    assert set(objs) == {"POB-1.1", "POB-1.2"}             # ALL objectives, not just weak ones
    assert objs["POB-1.1"]["leitner_box"] == 2
    assert objs["POB-1.1"]["latest_score_pct"] == 60
    assert objs["POB-1.1"]["feedback_negative"] == 1
    # POB-1.2 has no weakness_log / sessions -> null values present, not missing
    assert objs["POB-1.2"]["leitner_box"] is None
    assert objs["POB-1.2"]["latest_score_pct"] is None
    assert objs["POB-1.2"]["next_review"] is None


def test_past_papers_endpoint_sorted_year_desc(real_client):
    res = real_client.get(f"/api/past-papers/{SUBJECT_S13}")
    assert res.status_code == 200
    papers = res.json()["papers"]
    assert len(papers) == 2
    assert [p["year"] for p in papers] == [2024, 2023]     # year descending
    p2024 = papers[0]
    assert p2024["doc_id"] == "D1"
    assert p2024["question_count"] == 1
    assert p2024["objectives_covered"] == ["POB-1.1"]


def test_practice_question_endpoint_returns_question(real_client):
    res = real_client.get("/api/practice-question/D1/2b")
    assert res.status_code == 200
    body = res.json()
    assert body["question_id"] == "POB-1.1-q2b-stem"
    assert body["question_num"] == "2b"
    assert body["objective_id"] == "POB-1.1"
    assert body["marks_total"] == 3
    assert body["command_words"] == ["Explain"]


def test_practice_question_endpoint_404_when_missing(real_client):
    res = real_client.get("/api/practice-question/D1/9z")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Stage V2: GET /api/videos/{objective_id}
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_videos():
    """real_client fixture extended with one objective_videos row on POB-1.1."""
    db = _open_real_db()
    _seed_panel_data(db)
    db.execute(
        """
        INSERT INTO objective_videos
            (objective_id, subject_id, url, title, channel, duration_str, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "POB-1.1", SUBJECT_S13,
            "https://www.youtube.com/watch?v=abc123",
            "Intro to Business",
            "BizChannel",
            "8:45",
            "test_source.csv",
        ),
    )
    db.commit()
    app_module.app.state.db = db
    yield TestClient(app_module.app)
    db.close()


def test_videos_endpoint_returns_list(client_with_videos):
    res = client_with_videos.get("/api/videos/POB-1.1")
    assert res.status_code == 200
    body = res.json()
    assert "videos" in body
    assert len(body["videos"]) == 1
    v = body["videos"][0]
    assert v["title"] == "Intro to Business"
    assert v["url"] == "https://www.youtube.com/watch?v=abc123"
    assert v["channel"] == "BizChannel"
    assert v["duration"] == "8:45"


def test_videos_endpoint_empty_when_none(real_client):
    """Objective with no videos returns empty list, never 404."""
    res = real_client.get("/api/videos/POB-1.2")
    assert res.status_code == 200
    assert res.json() == {"videos": []}
