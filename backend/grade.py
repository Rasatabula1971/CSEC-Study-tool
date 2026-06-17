# PHASE: runtime
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
from llm_router import chat_for_grading  # noqa: E402
from weakness import log_weakness  # noqa: E402

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


# Phrases that mean "the student DID cover this point". A 3B model sometimes writes
# one of these in the evidence while still marking awarded=False -- a self-contradiction
# we correct deterministically (no prompt/schema change). Lowercase for case-insensitive match.
POSITIVE_MATCH_PHRASES = (
    "student gave", "student mentioned", "student described",
    "student provided", "student said", "student stated",
    "student explained", "student identified",
    "answer contains", "answer mentions", "answer includes",
    "answer describes", "answer states", "answer explains",
    "present in the answer", "covered in the answer",
    "addressed in the answer",
)

EMPTY_EVIDENCE_FILL = "No relevant content found in answer."


def reconcile_grading(result: dict) -> dict:
    """Post-process LLM grading to fix known 3B-model failure modes.

    Called after grade_against_syllabus / grade_synthesis produce the raw judged
    points and BEFORE compute_score, so corrected awards flow into both the score
    and (for synthesis) the per-objective weakness_log.

    Fix 1 -- Evidence-based flip: a point marked awarded=False whose evidence names
    the student as having covered it (POSITIVE_MATCH_PHRASES) is flipped to True,
    with "[auto-corrected] " prepended to the evidence so the correction is auditable.

    Fix 2 -- Empty evidence fill: a point still awarded=False with empty/whitespace
    evidence gets a clear placeholder instead of a blank string.

    Mutates and returns `result`. A result without a "points" list is returned as-is.
    """
    for point in result.get("points", []):
        if point.get("awarded"):
            continue
        evidence = point.get("evidence") or ""
        evidence_lc = evidence.lower()
        if any(phrase in evidence_lc for phrase in POSITIVE_MATCH_PHRASES):
            point["awarded"] = True
            point["evidence"] = "[auto-corrected] " + evidence
        elif not evidence.strip():
            point["evidence"] = EMPTY_EVIDENCE_FILL
    return result


def _grading_provenance(db: sqlite3.Connection, question_id: str,
                        mark_points: list[dict]) -> tuple[str, bool]:
    """grading_basis + pending_review for a graded answer (PDR v3.1 VAL-10).

    grading_basis reflects the strongest source_type among the mark points used,
    in priority order: past_paper > recovered_extraction > syllabus_derived. The
    source_type column is added by a runtime migration; on a DB that predates it
    we degrade to 'past_paper' (the historical default) rather than raise.

    pending_review is True when any graded objective still has content awaiting
    sign-off in ingest_review_queue (e.g. build-time syllabus-derived points). The
    UI shows a verify-with-teacher badge until that queue entry is cleared.
    """
    try:
        rows = db.execute(
            "SELECT DISTINCT source_type FROM mark_points WHERE question_id = ?",
            (question_id,),
        ).fetchall()
        types = {r["source_type"] for r in rows}
    except sqlite3.OperationalError:
        types = set()  # pre-migration DB: no source_type column

    if "past_paper" in types or not types or types == {None}:
        basis = "past_paper"
    elif "recovered_extraction" in types:
        basis = "recovered"
    elif "syllabus_derived" in types:
        basis = "syllabus_derived"
    else:
        basis = "past_paper"

    pending = False
    for oid in {mp["objective_id"] for mp in mark_points}:
        if db.execute(
            "SELECT 1 FROM ingest_review_queue WHERE objective_id = ? LIMIT 1",
            (oid,),
        ).fetchone():
            pending = True
            break
    return basis, pending


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


def _load_synthesis_examiner_prompt() -> str:
    return (PROMPTS_DIR / "synthesis_examiner.txt").read_text(encoding="utf-8")


def _synthesis_schema(n: int) -> dict:
    """GRADING_SCHEMA pinned to EXACTLY n points -- one per objective (Option C).

    A small local model will otherwise emit too few/too many points; min==max==n
    forces one judged point per objective in the batch.
    """
    return {
        "type": "object",
        "required": ["objective_id", "question_id", "points"],
        "properties": {
            "objective_id": {"type": "string"},
            "question_id": {"type": "string"},
            "points": {
                "type": "array",
                "minItems": n,
                "maxItems": n,
                "items": GRADING_SCHEMA["properties"]["points"]["items"],
            },
        },
    }


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

    # Provenance for the UI's verify-with-teacher signal (PDR v3.1 VAL-10): which
    # kind of mark scheme graded this, and whether those points are still pending
    # human review in ingest_review_queue.
    basis, pending = _grading_provenance(db, question_id, mark_points)
    grading["grading_basis"] = basis
    grading["pending_review"] = pending

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
        f"Anchor every point to what THIS question asks (not everything in the "
        f"content statement). Aim for 3-4 distinct, non-overlapping points; use 5-6 "
        f"only if the question has that many separate sub-tasks. Then judge each. Use "
        f'synthetic mark_point_id values "{objective_id}-syn-1", '
        f'"{objective_id}-syn-2", and so on, numbered in the order you list them.'
    )
    return {"role": "user", "content": content}


def grade_against_syllabus(db: sqlite3.Connection, objective_id: str,
                           question_stem: str, student_answer: str,
                           messages: list[dict] | None = None,
                           chat_fn=None) -> dict:
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

    # Default to the grading router (Gemini-preferred, Ollama fallback). Resolved
    # at call time -- not as a def-time default arg -- so it stays mockable and
    # honours an injected chat_fn (the controller and tests pass one explicitly).
    if chat_fn is None:
        chat_fn = chat_for_grading

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

    # Fix self-contradictory / blank-evidence 3B output before scoring.
    grading = reconcile_grading(grading)

    score = compute_score(grading)
    grading.update(score)
    return grading


def _build_synthesis_message(batch_id: int, objectives: list[dict],
                             student_answer: str) -> dict:
    """Hand the synthesis examiner every objective in the batch and the answer.

    Each objective's command_words is a JSON array string; a missing/malformed
    value degrades to an empty list rather than raising.
    """
    lines = []
    for i, obj in enumerate(objectives, 1):
        try:
            command_words = json.loads(obj["command_words"]) if obj["command_words"] else []
        except (json.JSONDecodeError, TypeError):
            command_words = []
        cw = ", ".join(command_words) if command_words else "(none specified)"
        lines.append(
            f"{i}. OBJECTIVE ID: {obj['objective_id']}\n"
            f"   CONTENT STATEMENT: {obj['content_stmt']}\n"
            f"   COMMAND WORDS: {cw}"
        )
    objectives_block = "\n".join(lines)
    content = (
        f"BATCH ID: {batch_id}\n\n"
        f"OBJECTIVES ({len(objectives)} in this batch):\n{objectives_block}\n\n"
        f"STUDENT ANSWER:\n{student_answer}\n\n"
        f"Produce EXACTLY {len(objectives)} points, one per objective, in the order "
        f'listed. Each mark_point_id MUST be "{batch_id}-syn-<OBJECTIVE ID>" '
        f'(e.g. "{batch_id}-syn-{objectives[0]["objective_id"]}").'
    )
    return {"role": "user", "content": content}


def grade_synthesis(db: sqlite3.Connection, batch_id: int, student_answer: str,
                    messages: list[dict] | None = None,
                    chat_fn=None) -> dict:
    """Grade a synthesis answer against a batch's objectives (Option C).

    Loads the batch's objective_ids, derives ONE expected point per objective via
    the synthesis examiner, and lets Python compute the /N score with
    compute_score(). For each objective, log_weakness is called independently with
    100 (point awarded) or 0 (missed) so each objective's Leitner box and weakness
    record update on its own -- exactly once per objective in the batch.

    Returns the GRADING_SCHEMA-shaped result (objective_id, question_id, points,
    score_pct, awarded, total, missed_points). Errors:
      {"error": "unknown_batch"}     -- no such batch_id.
      {"error": "unknown_objective"} -- a batch objective_id is missing.
    """
    batch = db.execute(
        "SELECT subject_id, objective_ids FROM study_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return {"error": "unknown_batch"}

    objective_ids = json.loads(batch["objective_ids"])
    subject_id = batch["subject_id"]

    objectives = []
    for oid in objective_ids:
        obj = _fetch_objective(db, oid)
        if obj is None:
            return {"error": "unknown_objective"}
        objectives.append(obj)

    # Default to the grading router (Gemini-preferred); resolved at call time so it
    # stays mockable and honours an injected chat_fn (see grade_against_syllabus).
    if chat_fn is None:
        chat_fn = chat_for_grading

    n = len(objectives)
    messages = list(messages or [])
    messages.append(_build_synthesis_message(batch_id, objectives, student_answer))

    raw = chat_fn(messages, system=_load_synthesis_examiner_prompt(),
                  schema=_synthesis_schema(n))
    grading = json.loads(raw)

    # Deterministic identity (Rule 1 / Rule 2): never trust the model for these.
    grading["objective_id"] = f"batch-{batch_id}"
    grading["question_id"] = f"synthesis-{batch_id}"
    grading["batch_id"] = batch_id

    # Fix self-contradictory / blank-evidence 3B output before scoring AND before
    # the per-objective weakness loop below, so corrected awards flow into both.
    grading = reconcile_grading(grading)

    score = compute_score(grading)
    grading.update(score)

    # Update each objective's weakness_log independently. Match points back to
    # objectives by the synthetic mark_point_id; a missing/unmatched point counts
    # as not awarded so a real objective_id is still recorded (Rule 1).
    awarded_by_id = {
        p.get("mark_point_id"): bool(p.get("awarded"))
        for p in grading.get("points", [])
    }
    for oid in objective_ids:
        awarded = awarded_by_id.get(f"{batch_id}-syn-{oid}", False)
        log_weakness(
            db,
            {"objective_id": oid, "subject_id": subject_id,
             "score_pct": 100 if awarded else 0},
            session_id=0,
        )

    return grading
