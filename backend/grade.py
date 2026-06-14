"""
backend/grade.py
================
Point-matching grader (CLAUDE.md "Grading Contract"). The Examiner LLM produces
ONE schema-constrained JSON object: one boolean + evidence per mark point.
Python computes every number -- the model never adds, averages, or picks a date.

Flow (grade_answer):
  1. Fetch the question's mark_points from the DB. None -> {"error": "no_mark_scheme"}.
  2. Load prompts/examiner.txt as the system prompt.
  3. Call ollama_chat with GRADING_SCHEMA as the JSON-format constraint.
  4. json.loads the response, then compute_score() in pure Python.

`chat_fn` is injectable so tests grade against a stub and never hit Ollama.
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_chat  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

GRADING_SCHEMA = {
    "type": "object",
    "required": ["objective_id", "question_id", "points"],
    "properties": {
        "objective_id": {"type": "string"},
        "question_id": {"type": "string"},
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["mark_point_id", "awarded", "evidence"],
                "properties": {
                    "mark_point_id": {"type": "string"},
                    "awarded": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}


# The syllabus grader must GENERATE its expected points (the mark-scheme grader is
# handed them), so a small local model emits an empty "points" array unless the
# schema demands items. Identical point shape to GRADING_SCHEMA, with the 3-6 count
# enforced -- the shared GRADING_SCHEMA above is left untouched for the mark grader.
SYLLABUS_GRADING_SCHEMA = {
    "type": "object",
    "required": ["objective_id", "question_id", "points"],
    "properties": {
        "objective_id": {"type": "string"},
        "question_id": {"type": "string"},
        "points": {
            "type": "array",
            "minItems": 3,
            "maxItems": 6,
            "items": GRADING_SCHEMA["properties"]["points"]["items"],
        },
    },
}


def compute_score(grading: dict) -> dict:
    """Deterministic scoring. Never delegated to the model."""
    pts = grading["points"]
    awarded = sum(1 for p in pts if p["awarded"])
    total = len(pts)
    pct = round(100 * awarded / total) if total else 0
    missed = [p["mark_point_id"] for p in pts if not p["awarded"]]
    return {
        "score_pct": pct,
        "awarded": awarded,
        "total": total,
        "missed_points": missed,
    }


def fetch_mark_points(db: sqlite3.Connection, question_id: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT mark_point_id, objective_id, point_text, marks_value, point_order
        FROM   mark_points
        WHERE  question_id = ?
        ORDER  BY point_order
        """,
        (question_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_examiner_prompt() -> str:
    return (PROMPTS_DIR / "examiner.txt").read_text(encoding="utf-8")


def _load_syllabus_examiner_prompt() -> str:
    return (PROMPTS_DIR / "syllabus_examiner.txt").read_text(encoding="utf-8")


def _build_user_message(question_id: str, student_answer: str,
                        mark_points: list[dict]) -> dict:
    """Hand the examiner the answer and the exact mark points to judge."""
    points_block = "\n".join(
        f'- mark_point_id="{mp["mark_point_id"]}" ({mp["marks_value"]} mark): {mp["point_text"]}'
        for mp in mark_points
    )
    content = (
        f"QUESTION ID: {question_id}\n\n"
        f"STUDENT ANSWER:\n{student_answer}\n\n"
        f"MARK POINTS (judge each independently, in order):\n{points_block}"
    )
    return {"role": "user", "content": content}


def grade_answer(db: sqlite3.Connection, question_id: str, student_answer: str,
                 messages: list[dict] | None = None, chat_fn=ollama_chat) -> dict:
    """Grade one answer against its mark scheme. Returns the full scored result.

    Result keys: objective_id, question_id, points, score_pct, awarded, total,
    missed_points. Returns {"error": "no_mark_scheme"} if the question has none.
    """
    mark_points = fetch_mark_points(db, question_id)
    if not mark_points:
        return {"error": "no_mark_scheme"}

    messages = list(messages or [])
    messages.append(_build_user_message(question_id, student_answer, mark_points))

    raw = chat_fn(messages, system=_load_examiner_prompt(), schema=GRADING_SCHEMA)
    grading = json.loads(raw)

    score = compute_score(grading)
    grading.update(score)

    # Attach each mark point's scheme text for display (read-only join on the
    # mark_points already read for this question, keyed by mark_point_id). A point
    # whose id has no matching row simply gets no point_text -- never raises.
    point_text_by_id = {mp["mark_point_id"]: mp["point_text"] for mp in mark_points}
    for point in grading.get("points", []):
        text = point_text_by_id.get(point.get("mark_point_id"))
        if text is not None:
            point["point_text"] = text

    return grading


def _fetch_objective(db: sqlite3.Connection, objective_id: str) -> dict | None:
    row = db.execute(
        "SELECT objective_id, content_stmt, skill_type, command_words "
        "FROM objectives WHERE objective_id = ?",
        (objective_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _build_syllabus_message(objective_id: str, objective: dict,
                            question_stem: str, student_answer: str) -> dict:
    """Hand the syllabus examiner the objective, command words, and the answer.

    command_words is stored as a JSON array string (e.g. '["Explain","Define"]');
    a missing/malformed value degrades to an empty list rather than raising.
    """
    try:
        command_words = json.loads(objective["command_words"]) if objective["command_words"] else []
    except (json.JSONDecodeError, TypeError):
        command_words = []
    cw = ", ".join(command_words) if command_words else "(none specified)"
    content = (
        f"OBJECTIVE ID: {objective_id}\n"
        f"CONTENT STATEMENT: {objective['content_stmt']}\n"
        f"COMMAND WORDS: {cw}\n"
        f"SKILL TYPE: {objective['skill_type'] or '(unspecified)'}\n\n"
        f"QUESTION:\n{question_stem}\n\n"
        f"STUDENT ANSWER:\n{student_answer}\n\n"
        f"List 3-6 expected points for THIS question, then judge each. Use "
        f'synthetic mark_point_id values "{objective_id}-syn-1", '
        f'"{objective_id}-syn-2", and so on, numbered in the order you list them.'
    )
    return {"role": "user", "content": content}


def grade_against_syllabus(db: sqlite3.Connection, objective_id: str,
                           question_stem: str, student_answer: str,
                           messages: list[dict] | None = None,
                           chat_fn=ollama_chat) -> dict:
    """Grade an answer with no fixed mark scheme, against the syllabus objective.

    Used when no mark_points exist for a question, OR when the question was
    generated from a syllabus objective (practice mode). The LLM derives 3-6
    expected points from the objective's content_stmt / command_words / skill_type
    and judges each as awarded/not -- the same GRADING_SCHEMA shape as
    grade_answer(), with synthetic mark_point_ids "{objective_id}-syn-{n}".

    Returns the same dict shape as grade_answer() (objective_id, question_id,
    points, score_pct, awarded, total, missed_points). Python still computes every
    number -- the model only produces booleans. Returns {"error": "unknown_objective"}
    if the objective does not exist.
    """
    objective = _fetch_objective(db, objective_id)
    if objective is None:
        return {"error": "unknown_objective"}

    messages = list(messages or [])
    messages.append(_build_syllabus_message(objective_id, objective,
                                            question_stem, student_answer))

    raw = chat_fn(messages, system=_load_syllabus_examiner_prompt(),
                  schema=SYLLABUS_GRADING_SCHEMA)
    grading = json.loads(raw)

    # Deterministic FK guarantee (CLAUDE.md Rule 1): the recorded objective is the
    # one we asked about, never whatever the model echoed back. Every graded answer
    # keeps a real objective_id for the weakness_log.
    grading["objective_id"] = objective_id

    score = compute_score(grading)
    grading.update(score)
    return grading
