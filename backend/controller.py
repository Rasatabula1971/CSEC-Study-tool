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

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from llm_router import chat_for_grading  # noqa: E402
from scope import is_in_scope, subject_is_locked, get_objective  # noqa: E402
from retrieval import get_context, has_structured_key, _structured_lookup  # noqa: E402
from grade import (  # noqa: E402
    grade_answer, grade_against_syllabus, fetch_mark_points, grade_synthesis,
)
from schedule import get_due_objectives  # noqa: E402
from weakness import log_weakness  # noqa: E402
from study_plan import (  # noqa: E402
    init_plan_for_subject, get_next_batch, mark_objective_outcome, get_plan_progress,
)

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


def _objective_context(db, objective_id: str) -> dict | None:
    """Context for a teach request that NAMES an objective explicitly.

    The lesson must be grounded in the *named* objective (CLAUDE.md Rule 1), never a
    semantic match -- a generic query like "Teach me this objective" would otherwise
    embed to an arbitrary nearest chunk and teach the wrong topic. Prefer a real
    notes chunk for the objective; fall back to its syllabus content statement so a
    lesson is still possible before notes are ingested. Returns None only when the
    objective_id is unknown. Crucially, ctx["objective_id"] always equals the input.
    """
    row = db.execute(
        """
        SELECT c.objective_id, c.chunk_text, c.page, d.source_file
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.objective_id   = ?
          AND  d.content_type   = 'notes'
        ORDER  BY c.id
        LIMIT  1
        """,
        (objective_id,),
    ).fetchone()
    if row is not None:
        return dict(row)

    obj = get_objective(db, objective_id)
    if obj is None:
        return None
    return {
        "objective_id": objective_id,
        "chunk_text": obj["content_stmt"],
        "source_file": "syllabus",
        "page": None,
    }


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

    # If the caller named an objective, the lesson MUST be on that objective (the
    # Study Plan stepper relies on lesson == question == graded). Gate it, then
    # ground the lesson on that exact objective -- never a semantic match, which
    # would teach an arbitrary topic from a generic query. Only free-text teach
    # (no objective_id) falls through to semantic retrieval.
    explicit = request.get("objective_id")
    if explicit:
        if not is_in_scope(db, subject_id, explicit):
            return OUT_OF_SCOPE
        ctx = _objective_context(db, explicit)
    else:
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


def _handle_grade(db, request, grade_fn, local_fn, embed_fn) -> dict:
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    explicit = request.get("objective_id")
    if explicit and not is_in_scope(db, subject_id, explicit):
        return OUT_OF_SCOPE

    question_id = request.get("question_id")
    student_answer = request.get("student_answer", "")

    # Mark-scheme path is unchanged: if the question has mark_points, grade against
    # them. The mark-scheme grader just matches the answer to GIVEN points, so it
    # stays on local Ollama (local_fn). The syllabus fallback GENERATES the expected
    # points, where model quality matters more -- it routes through grade_fn
    # (Gemini-preferred). Used when no mark scheme exists: a past-paper question
    # without one, or a generated practice question.
    if fetch_mark_points(db, question_id):
        grading = grade_answer(db, question_id, student_answer,
                               request.get("messages"), chat_fn=local_fn)
    else:
        resolved = _resolve_question_objective(db, question_id)
        if resolved is None:
            return {"error": "no_question"}
        obj_id, stem = resolved
        # Gate before spending the LLM call (CLAUDE.md scope rule).
        if not is_in_scope(db, subject_id, obj_id):
            return OUT_OF_SCOPE
        grading = grade_against_syllabus(db, obj_id, stem, student_answer,
                                         request.get("messages"), chat_fn=grade_fn)
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
# Study Plan routes (batched study with synthesis)
# ---------------------------------------------------------------------------
def _load_batch(db, batch_id) -> dict | None:
    row = db.execute(
        "SELECT batch_id, subject_id, objective_ids, synthesis_qid, status "
        "FROM study_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _handle_start_batch(db, request) -> dict:
    """Seed the plan (idempotent), pick the next N objectives, open a batch."""
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    init_plan_for_subject(db, subject_id)
    objectives = get_next_batch(db, subject_id)
    if not objectives:
        return {"error": "no_objectives"}

    objective_ids = [o["objective_id"] for o in objectives]
    cur = db.execute(
        "INSERT INTO study_batches (subject_id, objective_ids, status) "
        "VALUES (?, ?, 'active')",
        (subject_id, json.dumps(objective_ids)),
    )
    db.commit()
    return {
        "route": "start_batch",
        "batch_id": cur.lastrowid,
        "subject_id": subject_id,
        "objectives": objectives,
        "progress": get_plan_progress(db, subject_id),
    }


def _handle_batch_question(db, request, chat_fn) -> dict:
    """Generate the question for one step of a batch (per-objective or synthesis).

    step = "1".."N" -> a Tutor-generated question on objectives[step-1].
    step = "synthesis" -> ONE multi-part question connecting all N objectives.
    Both are stored in practice_questions so the grade route resolves them later.
    """
    batch = _load_batch(db, request.get("batch_id"))
    if batch is None:
        return {"error": "no_batch"}
    subject_id = batch["subject_id"]
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    objective_ids = json.loads(batch["objective_ids"])
    step = str(request.get("step", ""))

    if step == "synthesis":
        objectives = [get_objective(db, oid) for oid in objective_ids]
        if any(o is None for o in objectives):
            return {"error": "unknown_objective"}
        topics = "\n".join(
            f"- {o['objective_num']} {o['content_stmt']}" for o in objectives
        )
        user_msg = (
            "Write ONE multi-part CSEC exam-style question that requires the "
            "student to CONNECT all of the following syllabus objectives in a "
            "single answer. The parts should build on each other so the topics "
            "are linked, not asked separately. Output only the question itself "
            "-- no lesson, no answer, no preamble.\n\nTOPICS:\n" + topics
        )
        stem = chat_fn([{"role": "user", "content": user_msg}],
                       system=_load_prompt("tutor.txt"))
        question_id = f"synthesis-{batch['batch_id']}"
        # The synthesis question spans N objectives; store under the first for the
        # NOT NULL FK -- grading routes by batch, never by this stored objective.
        db.execute(
            "INSERT OR REPLACE INTO practice_questions "
            "(question_id, objective_id, subject_id, stem) VALUES (?, ?, ?, ?)",
            (question_id, objective_ids[0], subject_id, stem),
        )
        db.execute(
            "UPDATE study_batches SET synthesis_qid = ? WHERE batch_id = ?",
            (question_id, batch["batch_id"]),
        )
        db.commit()
        return {
            "route": "batch_question",
            "batch_id": batch["batch_id"],
            "step": "synthesis",
            "question_id": question_id,
            "objective_ids": objective_ids,
            "stem": stem,
            "is_synthesis": True,
        }

    # Per-objective step.
    try:
        idx = int(step) - 1
    except (TypeError, ValueError):
        return {"error": "bad_step"}
    if idx < 0 or idx >= len(objective_ids):
        return {"error": "bad_step"}

    objective_id = objective_ids[idx]
    objective = get_objective(db, objective_id)
    if objective is None:
        return {"error": "unknown_objective"}

    user_msg = (
        f"OBJECTIVE: {objective_id}\n"
        f"CONTENT STATEMENT: {objective['content_stmt']}\n\n"
        "Generate exactly ONE CSEC exam-style practice question that tests this "
        "objective. Output only the question itself -- no lesson, no answer, no "
        "preamble."
    )
    stem = chat_fn([{"role": "user", "content": user_msg}],
                   system=_load_prompt("tutor.txt"))
    question_id = f"batch-{batch['batch_id']}-step-{step}"
    db.execute(
        "INSERT OR REPLACE INTO practice_questions "
        "(question_id, objective_id, subject_id, stem) VALUES (?, ?, ?, ?)",
        (question_id, objective_id, subject_id, stem),
    )
    db.commit()
    return {
        "route": "batch_question",
        "batch_id": batch["batch_id"],
        "step": step,
        "question_id": question_id,
        "objective_id": objective_id,
        "objective_num": objective["objective_num"],
        "stem": stem,
        "is_synthesis": False,
    }


def _handle_grade_batch_question(db, request, chat_fn) -> dict:
    """Grade one batch answer, update the plan, complete the batch on synthesis."""
    batch = _load_batch(db, request.get("batch_id"))
    if batch is None:
        return {"error": "no_batch"}
    subject_id = batch["subject_id"]
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    question_id = request.get("question_id")
    answer = request.get("answer", request.get("student_answer", ""))
    objective_ids = json.loads(batch["objective_ids"])
    is_synthesis = question_id == batch.get("synthesis_qid") or \
        question_id == f"synthesis-{batch['batch_id']}"

    if is_synthesis:
        grading = grade_synthesis(db, batch["batch_id"], answer,
                                  request.get("messages"), chat_fn=chat_fn)
        if "error" in grading:
            return grading
        # grade_synthesis already wrote weakness_log per objective; only advance
        # the plan here (update_weakness=False avoids a double Leitner bump).
        awarded_by_id = {
            p.get("mark_point_id"): bool(p.get("awarded"))
            for p in grading.get("points", [])
        }
        for oid in objective_ids:
            awarded = awarded_by_id.get(f"{batch['batch_id']}-syn-{oid}", False)
            mark_objective_outcome(db, subject_id, oid, 100 if awarded else 0,
                                   update_weakness=False)
        db.execute(
            "UPDATE study_batches SET status = 'completed', "
            "completed_at = datetime('now') WHERE batch_id = ?",
            (batch["batch_id"],),
        )
        db.commit()
    else:
        resolved = _resolve_question_objective(db, question_id)
        if resolved is None:
            return {"error": "no_question"}
        obj_id, stem = resolved
        if not is_in_scope(db, subject_id, obj_id):
            return OUT_OF_SCOPE
        grading = grade_against_syllabus(db, obj_id, stem, answer,
                                         request.get("messages"), chat_fn=chat_fn)
        if "error" in grading:
            return grading
        grading["question_id"] = question_id
        mark_objective_outcome(db, subject_id, obj_id, grading["score_pct"],
                               update_weakness=True)

    grading["subject_id"] = subject_id
    grading["batch_id"] = batch["batch_id"]
    grading["is_synthesis"] = is_synthesis
    grading["progress"] = get_plan_progress(db, subject_id)
    return grading


def _handle_explain_missed(db, request, chat_fn) -> dict:
    """Teach the concepts a student missed on a per-objective step.

    Low-risk language generation (Tutor-style): given the missed points for one
    objective, returns a short plain-language explanation of what they should have
    included. Returns {"feedback": ""} with NO LLM call when nothing was missed.
    """
    subject_id = request.get("subject_id")
    if not subject_is_locked(db, subject_id):
        return OUT_OF_SCOPE

    objective_id = request.get("objective_id")
    if not objective_id or not is_in_scope(db, subject_id, objective_id):
        return OUT_OF_SCOPE

    # Nothing missed -> no work, no LLM call.
    missed = request.get("missed_points") or []
    if not missed:
        return {"feedback": ""}

    objective = get_objective(db, objective_id)
    if objective is None:
        return OUT_OF_SCOPE

    section = db.execute(
        "SELECT title FROM syllabus_sections WHERE section_id = ?",
        (objective["section_id"],),
    ).fetchone()
    title = section["title"] if section is not None else objective_id

    # Each missed point describes an expected idea (point_text) and/or what was
    # absent (evidence); prefer the expected idea, fall back to the evidence.
    lines = []
    for mp in missed:
        text = (mp.get("expected") or mp.get("evidence") or "").strip()
        if text:
            lines.append(f"- {text}")
    missed_block = "\n".join(lines) if lines else "- the key ideas the question asked for"

    filled = (
        _load_prompt("missed_feedback.txt")
        .replace("[OBJECTIVE TITLE]", title)
        .replace("[LIST OF MISSED POINTS]", missed_block)
        .replace("[CONTENT_STMT]", objective["content_stmt"])
    )
    feedback = chat_fn([{"role": "user", "content": "Explain what I missed."}],
                       system=filled)
    return {"feedback": feedback}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handle_request(db: sqlite3.Connection, request: dict,
                   chat_fn=None, embed_fn=ollama_embed) -> dict:
    """Route a request to teach / grade / plan. Out-of-scope -> immediate refusal.

    LLM routing (the cloud-grading upgrade): when `chat_fn` is provided (tests, or
    a caller that wants one model for everything) it serves EVERY LLM call --
    grading and generation alike -- preserving the injectable-stub contract. When
    omitted (production), grading-quality calls (syllabus/synthesis graders,
    explain_missed) route through chat_for_grading (Gemini preferred, Ollama silent
    fallback), while generation (teach, practice, question generation) and the
    mark-scheme grader stay on local Ollama.
    """
    if chat_fn is not None:
        grade_fn = local_fn = chat_fn
    else:
        grade_fn = chat_for_grading
        local_fn = ollama_chat

    route = request.get("route")
    if route == "teach":
        return _handle_teach(db, request, local_fn, embed_fn)
    if route == "grade":
        return _handle_grade(db, request, grade_fn, local_fn, embed_fn)
    if route == "practice":
        return _handle_practice(db, request, local_fn)
    if route == "plan":
        return _handle_plan(db, request)
    if route == "start_batch":
        return _handle_start_batch(db, request)
    if route == "batch_question":
        return _handle_batch_question(db, request, local_fn)
    if route == "grade_batch_question":
        return _handle_grade_batch_question(db, request, grade_fn)
    if route == "explain_missed":
        return _handle_explain_missed(db, request, grade_fn)
    return {"error": "unknown_route", "route": route}
