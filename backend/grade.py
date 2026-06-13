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
