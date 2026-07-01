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
    "required": ["objective_id", "question_id", "points", "confidence"],
    "properties": {
        "objective_id": {"type": "string"},
        "question_id": {"type": "string"},
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                # Stage 10: confidence (0-100) per point lets the UI show a
                # verify-with-teacher badge when the examiner is unsure.
                "required": ["mark_point_id", "awarded", "evidence", "confidence"],
                "properties": {
                    "mark_point_id": {"type": "string"},
                    "awarded": {"type": "boolean"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                },
            },
        },
        # Overall confidence in the whole grading judgement (0-100).
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
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


def compute_score(grading: dict, mark_points_db: list) -> dict:
    """Deterministic WEIGHTED scoring (Stage 10). Never delegated to the model.

    Each point's weight is its `marks_value` read from the DB row (never from the
    model's output -- the model only supplies the awarded boolean). A CSEC question
    worth [1, 2, 1] marks that misses only the 2-mark point scores 50%, not 67%.
    Points the model returns that have no matching DB mark point are ignored.
    """
    mp_by_id = {mp["mark_point_id"]: mp for mp in mark_points_db}
    awarded_marks = 0
    total_marks = 0
    missed = []
    for p in grading["points"]:
        mp = mp_by_id.get(p["mark_point_id"])
        if not mp:
            continue
        weight = mp["marks_value"] if mp["marks_value"] else 1
        total_marks += weight
        if p["awarded"]:
            awarded_marks += weight
        else:
            missed.append(p["mark_point_id"])
    pct = round(100 * awarded_marks / total_marks) if total_marks else 0
    return {
        "score_pct": pct,
        "awarded": awarded_marks,
        "total": total_marks,
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


# Explanation connectors (Stage 10). Evidence that merely echoes the student's
# words verbatim, with none of these, is a sign the examiner rubber-stamped the
# point rather than judging it -- flagged for review (not auto-downgraded).
EXPLANATION_CONNECTORS = (
    "because", "so", "which means", "therefore",
    "this causes", "as a result", "since", "thus",
)


def evidence_post_check(points: list[dict], student_answer: str) -> list[str]:
    """Sanity-check the examiner's evidence against rubber-stamping (Stage 10).

    Mutates each awarded point in place and returns the list of mark_point_ids
    flagged for review. Three gates run on awarded points only:

    Gate 1 (auto-downgrade) -- evidence under 20 chars is too thin to justify a
    mark, so the point is flipped to awarded=False and tagged in its evidence.

    Gate 2 (flag only) -- evidence that is a verbatim substring of the student's
    answer AND contains no explanation connector suggests the examiner echoed the
    text without judging it. The mark_point_id is flagged; the award STANDS (the
    model's call is trusted, the human is merely alerted).

    Gate 3 (flag only, roadmap #1) -- evidence that does NOT appear in the student
    answer at all suggests the model paraphrased loosely or fabricated the quote.
    Flagged, not downgraded: a paraphrase of a longer passage can be legitimate, so
    the human verifies rather than the system overruling the award.
    """
    review_flags: list[str] = []
    for p in points:
        if not p.get("awarded"):
            continue
        evidence = p.get("evidence", "")
        # Gate 1: evidence too thin -> downgrade.
        if len(evidence.strip()) < 20:
            p["awarded"] = False
            p["evidence"] = evidence + " [auto-downgraded: evidence too thin]"
        # Gate 2: verbatim echo without an explanation connector -> flag only.
        elif evidence.strip() in student_answer:
            if not any(c in evidence.lower() for c in EXPLANATION_CONNECTORS):
                review_flags.append(p["mark_point_id"])
        # Roadmap point #1 — evidence must appear in the student answer. Runs after
        # the gates above (re-reads awarded, so a Gate-1 downgrade is skipped here).
        if p.get("awarded"):
            evidence = p.get("evidence", "").strip()
            if evidence and evidence not in student_answer:
                if p["mark_point_id"] not in review_flags:
                    review_flags.append(p["mark_point_id"])
    return review_flags


def overall_confidence(grading: dict, default: int = 50) -> int:
    """Lowest per-point confidence, else the top-level confidence, else `default`.

    The grade is only as trustworthy as its weakest judged point, so the floor
    (min) drives the verify-with-teacher badge. Models/stubs that omit per-point
    confidence fall back to the overall value, then to `default` (Stage 10)."""
    per_point = [
        p["confidence"] for p in grading.get("points", [])
        if isinstance(p.get("confidence"), int)
    ]
    if per_point:
        return min(per_point)
    if isinstance(grading.get("confidence"), int):
        return grading["confidence"]
    return default


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


# Stage 13 (roadmap #3): plain-language labels for each source rank. Rank 5 is the
# runtime "unreviewed" overlay -- it must never appear in a successful grade.
SOURCE_RANK_LABELS = {
    1: "Official CXC syllabus",
    2: "Official specimen / mark scheme",
    3: "Official past paper mark scheme",
    4: "Generated, queued for review",
    5: "Generated, unreviewed (hidden at runtime)",
}


def source_rank_info(db: sqlite3.Connection, question_id: str,
                     mark_points: list[dict]) -> tuple[int | None, str | None, str | None]:
    """Resolve (rank, label, blocked_objective_id) for a question's mark points.

    rank = MIN(source_rank) over the question's points (best source wins). label is
    the plain-language string for that rank. blocked_objective_id is non-None only
    when a RANK-5 point is present: a generated point (source_rank >= 4) whose
    objective still has an unreviewed ingest_review_queue row. Such content is too
    unreliable to grade against, so the caller refuses.

    A real past-paper point (rank <= 3) whose objective merely has an incidental
    queue entry is NOT rank 5 -- it grades normally and surfaces pending_review as a
    softer banner instead. On a DB predating the source_rank column, returns
    (None, None, None) so grading proceeds unchanged (no rank shown).
    """
    try:
        rows = db.execute(
            "SELECT objective_id, source_rank FROM mark_points WHERE question_id = ?",
            (question_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None, None, None  # pre-migration DB: no source_rank column
    if not rows:
        return None, None, None

    # Rank-5 overlay: any generated (rank >= 4) point still pending review -> block.
    for r in rows:
        sr = r["source_rank"]
        if sr is not None and sr >= 4:
            queued = db.execute(
                "SELECT 1 FROM ingest_review_queue WHERE objective_id = ? LIMIT 1",
                (r["objective_id"],),
            ).fetchone()
            if queued:
                return 5, SOURCE_RANK_LABELS[5], r["objective_id"]

    ranks = [r["source_rank"] for r in rows if r["source_rank"] is not None]
    if not ranks:
        return None, None, None
    rank = min(ranks)
    return rank, SOURCE_RANK_LABELS.get(rank), None


def fetch_mark_points(db: sqlite3.Connection, question_id: str) -> list[dict]:
    """Fetch mark points for a question, deduplicated by point_group_id.

    Fanned-out rows (one per objective from a multi-objective CSV row) all share
    one point_group_id.  For scoring purposes only ONE representative row per group
    is kept — the first by point_order — so compute_score counts the point once
    and the LLM is asked to judge it once.  The full sibling list (all objective_ids
    sharing the group) is attached as "sibling_objective_ids" on the representative
    row so grade_answer can fan the awarded boolean out to log_weakness for every
    objective simultaneously.

    Rows with point_group_id = NULL are legacy rows (pre-m020); each is treated as
    its own unique group (no deduplication, behaviour unchanged).
    """
    rows = db.execute(
        """
        SELECT mark_point_id, objective_id, point_text, marks_value, point_order,
               point_group_id
        FROM   mark_points
        WHERE  question_id = ?
        ORDER  BY point_order
        """,
        (question_id,),
    ).fetchall()
    all_rows = [dict(r) for r in rows]

    # Collect sibling objective_ids per group (NULL groups are singletons)
    group_siblings: dict[str, list[str]] = {}
    for r in all_rows:
        pgid = r["point_group_id"]
        if pgid is None:
            continue
        group_siblings.setdefault(pgid, []).append(r["objective_id"])

    # Deduplicate: keep first representative per group
    seen_groups: set[str] = set()
    deduplicated: list[dict] = []
    for r in all_rows:
        pgid = r["point_group_id"]
        if pgid is None:
            # Legacy row — always include as-is
            r["sibling_objective_ids"] = [r["objective_id"]]
            deduplicated.append(r)
        elif pgid not in seen_groups:
            seen_groups.add(pgid)
            r["sibling_objective_ids"] = group_siblings[pgid]
            deduplicated.append(r)
        # else: fanned-out sibling — skip; representative already queued

    return deduplicated


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
                 messages: list[dict] | None = None, chat_fn=ollama_chat,
                 is_retry: bool = False) -> dict:
    """Grade one answer against its mark scheme. Returns the full scored result.

    Result keys: objective_id, question_id, points, score_pct, awarded, total,
    missed_points, is_retry. Returns {"error": "no_mark_scheme"} if the question
    has none.

    is_retry: marks this attempt as a re-attempt of a recall/quiz question. This
    function does NOT itself persist to study_sessions -- that write (shared by the
    mark-scheme and syllabus-fallback grade paths) lives in controller._handle_grade,
    which reads is_retry from the request and flags the study_sessions row. The flag
    is echoed into the result here so callers and the UI can observe it; a retry
    overwrites the visible result and the Leitner decision (weakness_log upserts by
    objective_id), while the original attempt remains in study_sessions history.
    """
    mark_points = fetch_mark_points(db, question_id)
    if not mark_points:
        return {"error": "no_mark_scheme"}

    # Roadmap #3 rank-5 gate: if the question rests on generated, still-unreviewed
    # mark points, refuse BEFORE spending the LLM call. The UI tells the student the
    # objective is still being prepared. (rank/label are reused below on success.)
    source_rank, source_rank_label, blocked_oid = source_rank_info(db, question_id, mark_points)
    if blocked_oid is not None:
        return {
            "error": "mark_points pending review",
            "objective_id": blocked_oid,
            "source_rank": 5,
            "source_rank_label": SOURCE_RANK_LABELS[5],
        }

    messages = list(messages or [])
    messages.append(_build_user_message(question_id, student_answer, mark_points))

    raw = chat_fn(messages, system=_load_examiner_prompt(), schema=GRADING_SCHEMA)
    grading = json.loads(raw)

    # Stage 10 evidence quality post-check: thin evidence is auto-downgraded (so it
    # flows into missed_points below); verbatim-echo evidence is flagged for review
    # without changing the award. Runs BEFORE compute_score so downgrades count.
    review_flags = evidence_post_check(grading.get("points", []), student_answer)

    # Weighted score: marks_value comes from the DB rows, never the model output.
    score = compute_score(grading, mark_points)
    grading.update(score)
    grading["overall_confidence"] = overall_confidence(grading)
    grading["review_flags"] = review_flags

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

    # Roadmap #3: the source rank + label resolved above (1-4; rank 5 already
    # refused). None on a pre-migration DB, in which case the UI omits the line.
    grading["source_rank"] = source_rank
    grading["source_rank_label"] = source_rank_label

    # Echo the retry flag (UI overhaul session 1). The actual study_sessions write
    # happens in controller._handle_grade; this lets callers/tests see it on the result.
    grading["is_retry"] = bool(is_retry)

    # Multi-objective fanout: build a map of {objective_id: awarded} for every
    # sibling objective referenced by fanned-out mark points. The controller uses
    # this to call log_weakness for each sibling objective, not just the primary one.
    # "awarded" for a sibling mirrors the representative row's awarded boolean
    # (they share the same point_group_id, so the same judgement applies to all).
    awarded_by_mpid = {p["mark_point_id"]: p.get("awarded", False)
                       for p in grading.get("points", [])}
    fanout: dict[str, bool] = {}  # objective_id -> awarded (True = at least one awarded point)
    for mp in mark_points:
        siblings = mp.get("sibling_objective_ids", [mp["objective_id"]])
        if len(siblings) <= 1:
            continue  # single-objective row — controller handles this via the primary path
        awarded = awarded_by_mpid.get(mp["mark_point_id"], False)
        for oid in siblings:
            # Use OR: if the sibling already has a True from a prior point, keep it
            fanout[oid] = fanout.get(oid, False) or awarded
    # Remove the primary objective_id — controller already logs that one
    fanout.pop(grading.get("objective_id"), None)
    if fanout:
        grading["fanned_objective_ids"] = fanout  # {oid: awarded_bool}

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

    # No DB mark scheme here -- the model GENERATED these points, so each is worth
    # one mark. A synthetic weight-1 list lets the weighted compute_score (Stage 10)
    # produce the same /N score this fallback grader always returned.
    synthetic_mps = [
        {"mark_point_id": p.get("mark_point_id"), "marks_value": 1}
        for p in grading.get("points", [])
    ]
    score = compute_score(grading, synthetic_mps)
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

    # One generated point per objective, each worth one mark -> synthetic weight-1
    # list for the weighted compute_score (Stage 10), preserving the /N synthesis score.
    synthetic_mps = [
        {"mark_point_id": p.get("mark_point_id"), "marks_value": 1}
        for p in grading.get("points", [])
    ]
    score = compute_score(grading, synthetic_mps)
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
