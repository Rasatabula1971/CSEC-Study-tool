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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from scope import is_in_scope, subject_is_locked  # noqa: E402
from retrieval import get_context, has_structured_key, _structured_lookup  # noqa: E402
from grade import grade_answer  # noqa: E402
from schedule import get_due_objectives  # noqa: E402
from weakness import log_weakness  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
OUT_OF_SCOPE = {"error": "out_of_scope"}


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _outcome(score_pct: int) -> str:
    return "pass" if score_pct >= 70 else "fail"


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
    grading = grade_answer(db, question_id, student_answer,
                           request.get("messages"), chat_fn=chat_fn)
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
    if route == "plan":
        return _handle_plan(db, request)
    return {"error": "unknown_route", "route": route}
