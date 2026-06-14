"""
backend/controller.py
=====================
Workflow router (CLAUDE.md "Deterministic vs LLM"). Wires the deterministic
modules and the LLM roles together for three routes: teach, grade, plan.

Ordering guarantee: the subject-lock gate runs BEFORE any retrieval, so an
out-of-scope request returns immediately with no LLM and no embedding call
(CLAUDE.md "Scope Check"). The resolved objective is re-checked after retrieval
so nothing outside the locked syllabus is ever taught or graded.

`chat_fn` / `embed_fn` are injectable so the controller is testable without Ollama.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from scope import is_in_scope, subject_is_locked, get_objective  # noqa: E402
from retrieval import get_context, has_structured_key, _structured_lookup  # noqa: E402
from grade import grade_answer, grade_against_syllabus, fetch_mark_points  # noqa: E402
from schedule import get_due_objectives  # noqa: E402
from weakness import log_weakness  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
OUT_OF_SCOPE = {"error": "out_of_scope"}


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _outcome(score_pct: int) -> str:
    return "pass" if score_pct >= 70 else "fail"


def _resolve_question_objective(db, question_id: str) -> tuple[str, str] | None:
    """Resolve a question_id with no mark scheme to (objective_id, question_stem).

    A past-paper question_id is a chunk_id (chunks carry the objective FK and the
    stem text); a practice question_id lives in practice_questions. Returns None if
    the id is unknown to both -- the caller refuses rather than grading blind.
    """
    row = db.execute(
        "SELECT objective_id, chunk_text FROM chunks WHERE chunk_id = ?",
        (question_id,),
    ).fetchone()
    if row is not None:
        return row["objective_id"], row["chunk_text"]

    row = db.execute(
        "SELECT objective_id, stem FROM practice_questions WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    if row is not None:
        return row["objective_id"], row["stem"]

    return None


def _pick_practice_objective(db, subject_id: str) -> str | None:
    """Pick an in-subject objective, weighted toward ones the student is weak on.

    Objectives present in weakness_log sort first, lowest score first; ties and the
    no-weakness case fall back to a random in-subject objective. Deterministic
    SQLite -- no LLM. Returns None if the subject has no objectives.
    """
    row = db.execute(
        """
        SELECT o.objective_id
        FROM   objectives o
        LEFT   JOIN weakness_log w
               ON w.objective_id = o.objective_id AND w.subject_id = o.subject_id
        WHERE  o.subject_id = ?
        ORDER  BY CASE WHEN w.score_pct IS NULL THEN 1 ELSE 0 END,
                  w.score_pct ASC,
                  RANDOM()
        LIMIT  1
        """,
        (subject_id,),
    ).fetchone()
    return row["objective_id"] if row is not None else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _handle_teach(db, request, chat_fn, embed_fn) -> dict:
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    # If the caller named an objective, gate on it before spending an embedding.
    explicit = request.get("objective_id")
    if explicit and not is_in_scope(db, subject_id, explicit):
        return OUT_OF_SCOPE

    ctx = get_context(db, request, embed_fn=embed_fn)
    if not ctx:
        return {"error": "no_context"}

    objective_id = ctx["objective_id"]
    if not is_in_scope(db, subject_id, objective_id):
        return OUT_OF_SCOPE

    user_msg = (
        f"OBJECTIVE: {objective_id}\n"
        f"STUDENT REQUEST: {request.get('query', '')}\n\n"
        f"SOURCE MATERIAL (ground your lesson in this, do not invent beyond it):\n"
        f"{ctx['chunk_text']}"
    )
    lesson = chat_fn([{"role": "user", "content": user_msg}], system=_load_prompt("tutor.txt"))
    return {
        "route": "teach",
        "objective_id": objective_id,
        "source_file": ctx["source_file"],
        "page": ctx["page"],
        "lesson": lesson,
    }


def _handle_grade(db, request, chat_fn, embed_fn) -> dict:
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    explicit = request.get("objective_id")
    if explicit and not is_in_scope(db, subject_id, explicit):
        return OUT_OF_SCOPE

    question_id = request.get("question_id")
    student_answer = request.get("student_answer", "")

    # Mark-scheme path is unchanged: if the question has mark_points, grade against
    # them. Otherwise fall back to grading against the syllabus objective -- either
    # a past-paper question with no mark scheme, or a generated practice question.
    if fetch_mark_points(db, question_id):
        grading = grade_answer(db, question_id, student_answer,
                               request.get("messages"), chat_fn=chat_fn)
    else:
        resolved = _resolve_question_objective(db, question_id)
        if resolved is None:
            return {"error": "no_question"}
        obj_id, stem = resolved
        # Gate before spending the LLM call (CLAUDE.md scope rule).
        if not is_in_scope(db, subject_id, obj_id):
            return OUT_OF_SCOPE
        grading = grade_against_syllabus(db, obj_id, stem, student_answer,
                                         request.get("messages"), chat_fn=chat_fn)
        # Keep the real question_id on the result -- the model is not told it.
        grading["question_id"] = question_id

    if "error" in grading:
        return grading

    objective_id = grading["objective_id"]
    if not is_in_scope(db, subject_id, objective_id):
        return OUT_OF_SCOPE

    # Attach source traceability when an exact key was supplied (no embedding).
    if has_structured_key(request):
        src = _structured_lookup(db, request)
        if src:
            grading["source_file"] = src["source_file"]
            grading["page"] = src["page"]

    cur = db.execute(
        "INSERT INTO study_sessions (subject_id, objective_id, mode, outcome, score_pct) "
        "VALUES (?, ?, 'grade', ?, ?)",
        (subject_id, objective_id, _outcome(grading["score_pct"]), grading["score_pct"]),
    )
    db.commit()
    session_id = cur.lastrowid

    grading["subject_id"] = subject_id
    grading["weakness"] = log_weakness(db, grading, session_id)
    grading["session_id"] = session_id
    return grading


def _handle_practice(db, request, chat_fn) -> dict:
    """Generate ONE practice question from a syllabus objective and persist it.

    Takes subject_id and an optional objective_id (a weakness-weighted random
    in-subject objective is chosen when omitted). The Tutor prompt generates the
    question; it is stored in practice_questions so the grade route can resolve it
    by question_id later. Returns the question in the same shape the quiz page
    renders past-paper questions with.
    """
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    objective_id = request.get("objective_id")
    if objective_id:
        if not is_in_scope(db, subject_id, objective_id):
            return OUT_OF_SCOPE
    else:
        objective_id = _pick_practice_objective(db, subject_id)
        if not objective_id:
            return {"error": "no_objective"}

    objective = get_objective(db, objective_id)
    user_msg = (
        f"OBJECTIVE: {objective_id}\n"
        f"CONTENT STATEMENT: {objective['content_stmt']}\n\n"
        "Generate exactly ONE CSEC exam-style practice question that tests this "
        "objective. Output only the question itself -- no lesson, no answer, no "
        "preamble."
    )
    stem = chat_fn([{"role": "user", "content": user_msg}], system=_load_prompt("tutor.txt"))

    # A microsecond timestamp keeps the PRIMARY KEY unique across rapid requests.
    question_id = f"practice-{objective_id}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    db.execute(
        "INSERT INTO practice_questions (question_id, objective_id, subject_id, stem) "
        "VALUES (?, ?, ?, ?)",
        (question_id, objective_id, subject_id, stem),
    )
    db.commit()

    return {
        "route": "practice",
        "question_id": question_id,
        "question_num": "Practice",
        "paper": "Syllabus Practice",
        "year": None,
        "stem": stem,
        "marks_total": None,
        "objective_id": objective_id,
    }


def _handle_plan(db, request) -> dict:
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    due = get_due_objectives(db, subject_id)
    tasks = [
        {
            "objective_id": r["objective_id"],
            "leitner_box": r["leitner_box"],
            "next_review": r["next_review"],
            "score_pct": r["score_pct"],
            "reason": r["reason"],
            "task_type": "review",
        }
        for r in due
    ]
    return {
        "route": "plan",
        "subject_id": subject_id,
        "due_count": len(tasks),
        "tasks": tasks,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handle_request(db: sqlite3.Connection, request: dict,
                   chat_fn=ollama_chat, embed_fn=ollama_embed) -> dict:
    """Route a request to teach / grade / plan. Out-of-scope -> immediate refusal."""
    route = request.get("route")
    if route == "teach":
        return _handle_teach(db, request, chat_fn, embed_fn)
    if route == "grade":
        return _handle_grade(db, request, chat_fn, embed_fn)
    if route == "practice":
        return _handle_practice(db, request, chat_fn)
    if route == "plan":
        return _handle_plan(db, request)
    return {"error": "unknown_route", "route": route}
