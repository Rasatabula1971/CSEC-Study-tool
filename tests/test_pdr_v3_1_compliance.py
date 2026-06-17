"""
tests/test_pdr_v3_1_compliance.py
=================================
Locks in the PDR v3.1 build-time / runtime split (see CSEC_AI_Study_Partner_PDR_v3_1
and CLAUDE.md "Offline-First: What It Means and What It Does Not Mean"). Three tests,
each named for the PDR acceptance criterion it enforces:

  * VAL-01 -- runtime is Ollama-only. No PHASE: runtime module may import a cloud
    client (static check), and a full student session works with the cloud blocked.
  * VAL-10 -- build-time cloud content is flagged (source_model='gemini'), queued in
    ingest_review_queue, and grading surfaces a pending_review gate until reviewed.

PHASE markers live on the first line of every backend module. This file reads them;
files with NO marker are SKIPPED (not failed) and surfaced in a printed warning, so a
future module added without a marker shows up here rather than silently passing.

Run: pytest tests/test_pdr_v3_1_compliance.py -v
"""

import ast
import json
import re
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

BACKEND = ROOT / "backend"
SCHEMA_PATH = BACKEND / "db" / "schema.sql"
EMBED_DIM = 768

import app as app_module  # noqa: E402
import controller  # noqa: E402
import grade  # noqa: E402
import gemini_client  # noqa: E402
import llm_router  # noqa: E402
import derive_syllabus_mark_points as dsmp  # noqa: E402


# ===========================================================================
# Shared helpers
# ===========================================================================
PHASE_RE = re.compile(r"#\s*PHASE:\s*(\w+)")

# Cloud clients a PHASE: runtime module must never import (VAL-01).
FORBIDDEN_CLOUD_MODULES = (
    "gemini_client",
    "google.generativeai",
    "google.genai",
    "anthropic",
    "openai",
    "cohere",
)


def _phase_marker(path: Path) -> str | None:
    """Read the first 5 lines and return the PHASE value, or None if unmarked."""
    with open(path, encoding="utf-8") as fh:
        for _ in range(5):
            line = fh.readline()
            if not line:
                break
            m = PHASE_RE.search(line)
            if m:
                return m.group(1)
    return None


def _module_scope_imports(tree: ast.Module) -> list[tuple[str, int]]:
    """(module_name, lineno) for every import at MODULE scope (not inside a def)."""
    names: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            names.append((node.module or "", node.lineno))
    return names


def _is_cloud_module(modname: str) -> bool:
    return any(
        modname == f or modname.startswith(f + ".") for f in FORBIDDEN_CLOUD_MODULES
    )


def _open_schema_db() -> sqlite3.Connection:
    """In-memory DB with the full schema + sqlite-vec + runtime migrations applied."""
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping PDR compliance DB tests")
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


SUBJECT = "Principles_of_Business"
OBJECTIVE = "POB-1.1"


def _seed_locked_objective(db: sqlite3.Connection) -> None:
    """Locked subject + one section + one objective (command word 'Explain')."""
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?, ?, 1)",
        (SUBJECT, "Principles of Business"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES ('SEC-1', ?, 'Nature of Business', '1')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO objectives (objective_id, section_id, subject_id, objective_num, "
        "content_stmt, skill_type, command_words) "
        "VALUES (?, 'SEC-1', ?, '1.1', 'Explain the concept of a business', "
        "'Understanding', '[\"Explain\"]')",
        (OBJECTIVE, SUBJECT),
    )
    db.commit()


# ===========================================================================
# TEST 1 -- VAL-01: runtime modules import no cloud client (static)
# ===========================================================================
def test_val_01_runtime_modules_have_no_cloud_imports():
    """Every PHASE: runtime backend module must be free of cloud-client imports.

    Unmarked files are skipped (not failed) and reported in a warning, so a module
    added later without a PHASE marker surfaces here instead of slipping through.
    """
    unmarked: list[str] = []
    runtime_checked = 0

    for path in sorted(BACKEND.rglob("*.py")):
        phase = _phase_marker(path)
        rel = path.relative_to(ROOT)
        if phase is None:
            unmarked.append(str(rel))
            continue
        if phase != "runtime":
            continue  # dual gates cloud internally; build never runs in a session

        runtime_checked += 1
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for modname, lineno in _module_scope_imports(tree):
            assert not _is_cloud_module(modname), (
                f"VAL-01 violation: PHASE: runtime module '{rel}' imports cloud "
                f"client '{modname}' at line {lineno}. Runtime paths must be "
                f"Ollama-only -- route cloud access through the PHASE: dual router."
            )

    assert runtime_checked > 0, "no PHASE: runtime modules scanned -- marker read broke"

    if unmarked:
        print(
            "\n[WARN] backend/*.py with NO PHASE marker (skipped, not failed) -- "
            "assign a marker or they escape VAL-01:"
        )
        for rel in unmarked:
            print(f"  - {rel}")


# ===========================================================================
# TEST 2 -- VAL-01: a full student session works with the cloud blocked
# ===========================================================================
def test_val_01_student_session_works_with_cloud_blocked(monkeypatch):
    """With CLOUD_MODE=0 and Gemini hard-blocked, every student-facing endpoint
    returns a valid response and gemini_chat is never invoked."""
    db = _open_schema_db()
    _seed_locked_objective(db)
    # 1 chunk grounds the teach lesson; 2 mark points back the grade request.
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES ('doc-1', ?, 'notes', 'notes.pdf', 'hash-1')",
        (SUBJECT,),
    )
    db.execute(
        "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, chunk_id) "
        "VALUES ('doc-1', ?, ?, 'A business supplies goods and services to satisfy "
        "needs and wants.', 1, ?)",
        (OBJECTIVE, SUBJECT, f"{OBJECTIVE}-c1"),
    )
    for i in (1, 2):
        db.execute(
            "INSERT INTO mark_points (mark_point_id, objective_id, question_id, doc_id, "
            "point_text, marks_value, point_order) VALUES (?, ?, 'q1', 'doc-1', ?, 1, ?)",
            (f"mp{i}", OBJECTIVE, f"point {i}", i),
        )
    db.commit()

    # --- block the cloud, in every place it could be reached ---------------
    monkeypatch.setenv("CLOUD_MODE", "0")
    gemini_chat_mock = MagicMock(side_effect=RuntimeError("cloud blocked at runtime"))
    monkeypatch.setattr(gemini_client, "gemini_chat", gemini_chat_mock)
    monkeypatch.setattr(llm_router, "gemini_chat", gemini_chat_mock)
    monkeypatch.setattr(gemini_client, "is_gemini_available", lambda: False)
    monkeypatch.setattr(llm_router, "is_gemini_available", lambda: False)
    monkeypatch.setattr(app_module, "is_gemini_available", lambda: False)
    monkeypatch.setattr(app_module, "ollama_health", lambda: False)  # no network wait

    grade_json = json.dumps({
        "objective_id": OBJECTIVE,
        "question_id": "q1",
        "points": [
            {"mark_point_id": "mp1", "awarded": True,
             "evidence": "the student explained that a business supplies goods"},
            {"mark_point_id": "mp2", "awarded": False, "evidence": "not addressed"},
        ],
    })

    def fake_ollama(messages, system, schema=None):
        # Grade calls pass GRADING_SCHEMA; teach/generation calls pass none.
        return grade_json if schema is not None else "A business supplies goods and services."

    monkeypatch.setattr(controller, "ollama_chat", fake_ollama)

    app_module.app.state.db = db
    from starlette.testclient import TestClient
    client = TestClient(app_module.app)  # no `with` -> lifespan not run, db stays ours

    # --- exercise the student-facing surface -------------------------------
    r = client.post("/api/chat", json={
        "message": "explain what a business is", "subject_id": SUBJECT,
        "route": "teach", "objective_id": OBJECTIVE,
    })
    assert r.status_code == 200, r.text
    assert r.json().get("lesson"), "teach must return a lesson"

    r = client.post("/api/chat", json={
        "message": "a business supplies goods and services", "subject_id": SUBJECT,
        "route": "grade", "question_id": "q1",
    })
    assert r.status_code == 200, r.text
    assert "score_pct" in r.json(), "grade must return a score"

    assert client.get(f"/api/due/{SUBJECT}").status_code == 200
    assert client.get("/api/subjects").status_code == 200
    assert client.get("/health").status_code == 200

    # /api/feedback is a Stage 12 endpoint -- exercise it only if it exists yet.
    feedback_route = any(
        getattr(route, "path", None) == "/api/feedback" for route in app_module.app.routes
    )
    if feedback_route:
        r = client.post("/api/feedback", json={
            "objective_id": OBJECTIVE, "subject_id": SUBJECT,
            "feedback_type": "lesson", "sentiment": "positive",
        })
        assert r.status_code == 200, r.text
    else:
        print("\n[INFO] /api/feedback not present yet (Stage 12) -- skipped in VAL-01.")

    # --- the guarantee: the cloud was never touched ------------------------
    gemini_chat_mock.assert_not_called()
    db.close()


# ===========================================================================
# TEST 3 -- VAL-10: build-phase review gate
# ===========================================================================
def test_val_10_build_phase_review_gate(monkeypatch):
    """Build-time Gemini extraction is stored flagged + queued, and grading against
    those points reports grading_basis='syllabus_derived' with pending_review set."""
    db = _open_schema_db()
    _seed_locked_objective(db)
    # Notes chunks (with vec rows) so the derivation has source material to ground in.
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, source_file, content_hash) "
        "VALUES ('doc-n', ?, 'notes', 'notes.pdf', 'hash-n')",
        (SUBJECT,),
    )
    for i in range(2):
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, chunk_id) "
            "VALUES ('doc-n', ?, ?, 'A business supplies goods and services to satisfy "
            "the needs and wants of consumers.', 1, ?)",
            (OBJECTIVE, SUBJECT, f"notes-c{i}"),
        )
        db.execute(
            "INSERT INTO vec_notes(rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, dsmp.serialize_vec([0.0] * EMBED_DIM)),
        )
    db.commit()

    # --- build-time cloud is enabled and reachable -------------------------
    monkeypatch.setenv("CLOUD_MODE", "1")
    extraction = {"points": [
        {"point_text": "Defines a business as an entity that supplies goods and services",
         "marks_value": 1, "confidence": 90,
         "evidence_quote": "supplies goods and services"},
        {"point_text": "States that a business satisfies consumers' needs and wants",
         "marks_value": 1, "confidence": 85,
         "evidence_quote": "satisfy the needs and wants of consumers"},
    ]}
    gemini_chat_mock = MagicMock(return_value=json.dumps(extraction))
    monkeypatch.setattr(gemini_client, "gemini_chat", gemini_chat_mock)
    monkeypatch.setattr(gemini_client, "is_gemini_available", lambda: True)

    # Invoke the derivation's main logic (chat_fn defaults to the build router).
    summary = dsmp.derive_syllabus_mark_points(
        db, SUBJECT, embed_fn=lambda text: [0.0] * EMBED_DIM, verbose=False,
    )
    assert summary["points_written"] == 2
    gemini_chat_mock.assert_called()  # the build engine routed to Gemini

    rows = db.execute(
        "SELECT source_model, source_type FROM mark_points WHERE objective_id = ?",
        (OBJECTIVE,),
    ).fetchall()
    assert len(rows) == 2, "two derived mark points written"
    assert all(r["source_model"] == "gemini" for r in rows), "flagged as cloud-authored"
    assert all(r["source_type"] == "syllabus_derived" for r in rows)

    queue = db.execute(
        "SELECT reason FROM ingest_review_queue WHERE objective_id = ?",
        (OBJECTIVE,),
    ).fetchall()
    assert len(queue) == 2, "one review-queue row per derived point"
    assert all(r["reason"] == "syllabus_derived_first_run" for r in queue)

    # --- the gate: grade against the derived points ------------------------
    # Derived points carry no question_id; attach one so the mark-scheme grader
    # resolves them (the UI grades a generated question keyed to these points).
    db.execute(
        "UPDATE mark_points SET question_id = 'q-derived' WHERE objective_id = ?",
        (OBJECTIVE,),
    )
    db.commit()
    mp_ids = [
        r["mark_point_id"] for r in db.execute(
            "SELECT mark_point_id FROM mark_points WHERE objective_id = ? ORDER BY point_order",
            (OBJECTIVE,),
        ).fetchall()
    ]
    grade_json = json.dumps({
        "objective_id": OBJECTIVE,
        "question_id": "q-derived",
        "points": [
            {"mark_point_id": mp_ids[0], "awarded": True,
             "evidence": "the student supplied goods and services"},
            {"mark_point_id": mp_ids[1], "awarded": False, "evidence": "not stated"},
        ],
    })
    result = grade.grade_answer(
        db, "q-derived", "a business supplies goods and services",
        chat_fn=lambda messages, system, schema=None: grade_json,
    )

    assert result["grading_basis"] == "syllabus_derived"
    assert result["pending_review"] is True, (
        "points still awaiting sign-off in ingest_review_queue must flag pending_review"
    )
    db.close()
