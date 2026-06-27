# PHASE: build
"""
backend/ingest_lessons.py
=========================
Stage 11 (Build Playbook v3.1) -- Canonical Lessons.

Pre-generate ONE canonical lesson per syllabus objective at build time, store it
in objective_lessons, and let the runtime teach route serve it deterministically
(no Ollama call on a student request). This eliminates topical drift and removes
the regex-based active-recall extraction the UI used to do client-side.

Non-negotiable constraints (CLAUDE.md + the v3.1 non-expert-builder anchor):
  * Build-time only (PHASE: build). Composition routes through
    llm_router.chat_for_lesson_composition -> Claude Sonnet via the Anthropic API
    on the BUILDER's machine (PDR v3.2 cost-separation decision). It falls back to
    Ollama only when no ANTHROPIC_API_KEY is present. The student's machine never
    runs this script; runtime stays Ollama-only and offline.
  * The model REWRITES the supplied SOURCE MATERIAL for a Form 5 student. It never
    invents concepts, examples, or terminology absent from the source chunks; when
    the source is too thin it returns status='insufficient_source' instead.
  * No source, no lesson. An objective with zero source chunks is queued in
    lesson_generation_queue (reason='insufficient_sources'), never written blind.
  * Confidence is floored locally -- the model's self-reported confidence is
    capped by a floor derived from how much real source material was available.
  * Idempotent. A lesson already present is skipped unless --regenerate is set,
    which DELETEs the existing row before writing the new one.

Run:
    python backend/ingest_lessons.py --subject Principles_of_Business
    python backend/ingest_lessons.py --subject Principles_of_Business --regenerate
    python backend/ingest_lessons.py --subject Principles_of_Business --confidence-floor 30
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# backend/ on sys.path so the bare module imports resolve whether this is run as
# `python backend/ingest_lessons.py` or imported in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from retrieval import serialize_vec  # noqa: E402
from db.backup import backup_first  # noqa: E402
from llm_router import chat_for_lesson_composition  # noqa: E402

# The Lesson Structurer system prompt lives in prompts/lesson_structurer.txt (PDR
# v3.2): it is the authored, version-controlled prompt for Claude Sonnet, not inline
# Python. Loaded once at import.
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def _load_structurer_prompt() -> str:
    return (PROMPTS_DIR / "lesson_structurer.txt").read_text(encoding="utf-8")


LESSON_STRUCTURER_PROMPT = _load_structurer_prompt()

# The seven canonical subject_id strings the Lesson Structurer prompt branches on.
CANONICAL_SUBJECTS = {
    "Principles_of_Business", "Economics", "Mathematics", "English",
    "Principles_of_Accounts", "Integrated_Science", "Information_Technology",
}


def _normalize_subject_id(subject: str) -> str:
    """Normalise a subject input to one of the seven canonical subject_id strings.

    The Lesson Structurer prompt branches on `subject`, and the DB stores objectives
    under the canonical id, so the value must resolve exactly. Accepts minor variants
    (case, spaces instead of underscores) and raises ValueError on anything that does
    not resolve -- a guard so a typo never silently composes an off-syllabus lesson
    or queries the wrong subject.
    """
    if not subject or not isinstance(subject, str):
        raise ValueError(f"unknown subject: {subject!r}")
    cleaned = subject.strip().replace(" ", "_")
    for canon in CANONICAL_SUBJECTS:
        if cleaned.lower() == canon.lower():
            return canon
    raise ValueError(f"unknown subject: {subject!r}")

# Notes are the primary source, but the mark-scheme/past-paper pull is ADDITIVE:
# it runs for every objective, not only when notes are thin. A noisy or
# heading-only notes top-k (syllabus headings, index lines, duplicate source-card
# URL fragments) can crowd out the real teaching content, which for some objectives
# lives in answer keys and past papers (e.g. flotation definitions). Notes still
# come first and still drive local_confidence_floor; ADDITIVE_K is kept small so the
# merged set stays bounded.
NOTES_TABLE = "vec_notes"
PAST_PAPERS_TABLE = "vec_past_papers"
MARK_SCHEMES_TABLE = "vec_mark_schemes"
NOTES_K = 15
ADDITIVE_K = 5      # top-K pulled from EACH of past_papers + mark_schemes, always
FALLBACK_K = 3      # retained for backwards-compat; superseded by ADDITIVE_K
MIN_NOTES_CHUNKS = 2

# Short table -> display source name, used in the summary "sources" column.
SOURCE_NAMES = {
    NOTES_TABLE: "notes",
    PAST_PAPERS_TABLE: "papers",
    MARK_SCHEMES_TABLE: "schemes",
}

DEFAULT_CONFIDENCE_FLOOR = 30
QUEUE_REASON = "insufficient_sources"


# A recall prompt is valid if it ENDS in '?' or BEGINS with one of these CSEC command
# words (imperative prompts like "Identify one example..." are real recall questions
# that do not end in '?'). Junk array elements ('multiple-choice', a leaked answer in
# parentheses) match neither and are rejected.
_RECALL_COMMAND_WORDS = (
    "define", "state", "explain", "identify", "describe", "discuss",
    "distinguish", "outline", "list", "calculate", "compare", "contrast",
    "name", "give",
)


# The word floor depends on the objective's command word: a pure "Define" lesson is
# honestly short, while a "Discuss" lesson needs the most room. These tiers mirror the
# prompt's own COMMAND-WORD REGISTER section (harder demand -> more room) so the gate
# never false-rejects an accurate short-answer lesson the way a flat 300 floor did.
COMMAND_WORD_FLOOR_TIERS = {
    # Knowledge-tier: precise, brief, memorisable. A pure definition objective is
    # honestly short -- don't force padding.
    'define': 180, 'state': 180, 'list': 180,
    # Understanding-tier: cause-and-effect or step-by-step reasoning.
    'explain': 300, 'describe': 300,
    # Application-tier: method + worked example.
    'calculate': 300, 'solve': 300, 'apply': 300, 'use': 300,
    'construct': 300,
    # Discuss-tier: multiple sides or factors weighed -- needs the most room.
    'discuss': 350, 'analyse': 350, 'analyze': 350, 'compare': 350,
    # Figure-description tier.
    'draw': 250, 'sketch': 250, 'illustrate': 250,
}
DEFAULT_WORD_FLOOR = 300  # fallback for command words not listed above


def _word_floor_for_objective(command_words) -> int:
    """Pick the word floor from the HIGHEST-demand command word present.

    Matches the prompt's own command-word register logic (harder demand = more room
    needed). command_words may be a JSON string or an already-parsed list. An empty /
    absent value, or words not in the tier table, fall back to DEFAULT_WORD_FLOOR.
    """
    if isinstance(command_words, str):
        try:
            command_words = json.loads(command_words)
        except (TypeError, ValueError):
            command_words = []
    if not command_words:
        return DEFAULT_WORD_FLOOR

    floors = [
        COMMAND_WORD_FLOOR_TIERS.get(str(w).strip().lower(), DEFAULT_WORD_FLOOR)
        for w in command_words
    ]
    # "Highest demand" = the largest floor value, since the tiers are ordered by
    # cognitive demand (Define < Explain < Discuss in required depth and word count).
    return max(floors)


# Chat-boilerplate detection. The pollution we guard against is the model breaking
# character to ADDRESS THE READER conversationally (usually a closing line) -- not the
# mere appearance of words like "feel free" or "clarification", which are legitimate
# TEACHING CONTENT in a communication lesson (the POB-2.13 false positive). So we match
# phrase-level, reader-addressed patterns, not bare keywords. Note the "feel free to
# ask" pattern excludes a third-party object ("ask the customer/your manager") so
# genuine communication advice passes while "feel free to ask (me/if you...)" is caught.
CONVERSATIONAL_BREAK_PATTERNS = [
    r'\blet me know if\b',
    r'\bfeel free to ask\b(?!\s+(?:the|your|a|an|our|their|his|her|each)\b)',
    r'\bif you (?:need|want|have)\b.{0,20}\b(?:clarification|examples|help)\b',
    r'\bdo you (?:have|need) any questions\b',
    r'\bI hope this helps\b',
    r"\bI'm happy to\b",
]
_CONVERSATIONAL_BREAK_RE = [re.compile(p, re.IGNORECASE) for p in CONVERSATIONAL_BREAK_PATTERNS]


def _has_conversational_break(text) -> bool:
    """True if the text addresses the reader conversationally (assistant-voice break).

    Phrase-level, not bare-keyword: 'feel free to ask the customer for clarification'
    (teaching content) passes; 'Let me know if you'd like more clarification' (the
    model chatting) is caught.
    """
    if not text:
        return False
    return any(rx.search(text) for rx in _CONVERSATIONAL_BREAK_RE)


def _validate_lesson_quality(lesson_text, recall_questions, command_words=None):
    """Reject semantically-broken lessons the JSON schema can't catch (chat
    boilerplate, hallucinated 'Section N' citations, junk recall questions).

    Returns (ok, reason): (True, None) when the lesson is clean, else (False, reason).
    Applied before INSERT so a syntactically-valid-but-broken lesson is queued for a
    re-attempt instead of being served to a student.

    A recall question is accepted when it ends in '?' OR opens with a CSEC command
    word -- valid imperative prompts ("Identify two roles of...") do not end in '?',
    so requiring a literal '?' would wrongly reject good questions while still letting
    junk through. Everything else (boilerplate, too-short, non-string, leaked answers,
    bare labels like 'multiple-choice') is rejected.

    The lesson format is one question per lesson (PDR v3.2): recall_questions must hold
    exactly 1 item. lesson_text must also clear a word floor that is TIERED by the
    objective's command word (_word_floor_for_objective): a Define/State/List lesson is
    honestly short, a Discuss/Analyse/Compare lesson needs the most room. command_words
    may be a JSON string or a parsed list; None falls back to DEFAULT_WORD_FLOOR.
    """
    lower = (lesson_text or "").lower()
    if 'according to section' in lower:
        return False, 'lesson_text cites chunk section'
    # Contextual boilerplate check (phrase-level, reader-addressed) -- NOT bare
    # keywords, so a communication lesson using "feel free"/"clarification" as content
    # is not false-flagged (POB-2.13).
    if _has_conversational_break(lesson_text):
        return False, 'lesson_text contains chat boilerplate'
    # Word floor (checked AFTER the lesson_text content checks so a short, boilerplate
    # lesson still reports the more specific reason). The floor is tiered by command
    # word so a Define lesson is not held to a Discuss lesson's length.
    word_count = len((lesson_text or "").split())
    floor = _word_floor_for_objective(command_words)
    if word_count < floor:
        return False, f'lesson too short ({word_count} words; min {floor} for this command word)'
    if not isinstance(recall_questions, list) or len(recall_questions) != 1:
        got = len(recall_questions) if isinstance(recall_questions, list) else 'non-list'
        return False, f'recall_questions count != 1 (got {got})'
    for q in recall_questions:
        if not isinstance(q, str):
            return False, 'non-string in recall_questions'
        qs = q.strip()
        if len(qs) < 15:
            return False, 'recall_question too short'
        ql = qs.lower()
        if _has_conversational_break(qs):
            return False, 'recall_question contains chat boilerplate'
        # Answer leakage: the model appended the answer in parentheses, e.g.
        # "What is the term...? (Answer: Transportation)" (the POB-10.13 pattern).
        if '(answer:' in ql:
            return False, 'recall_question leaks the answer'
        is_question = qs.endswith('?')
        is_command = any(ql.startswith(cw + ' ') for cw in _RECALL_COMMAND_WORDS)
        # Scenario-first prompts: "Revenue is $500. Calculate the profit margin."
        # The command word appears after a sentence boundary, not at the start.
        is_mid_command = any(f'. {cw} ' in ql for cw in _RECALL_COMMAND_WORDS)
        if not (is_question or is_command or is_mid_command):
            return False, 'recall_question is not a question or command prompt'
    return True, None


def ensure_lesson_tables(db: sqlite3.Connection) -> None:
    """Create objective_lessons + lesson_generation_queue if absent.

    Mirrors app.apply_runtime_migrations so the script (and tests) work against a
    DB the FastAPI app has not opened yet. CREATE TABLE IF NOT EXISTS is a no-op
    when the table already exists.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS objective_lessons (
            lesson_id          TEXT PRIMARY KEY,
            objective_id       TEXT NOT NULL UNIQUE REFERENCES objectives(objective_id),
            subject_id         TEXT NOT NULL REFERENCES subjects(subject_id),
            lesson_text        TEXT NOT NULL,
            worked_examples    TEXT,
            key_terms          TEXT,
            common_mistakes    TEXT,
            recall_questions   TEXT NOT NULL,
            source_chunk_ids   TEXT NOT NULL,
            confidence         INTEGER NOT NULL,
            generated_at       TEXT DEFAULT (datetime('now')),
            reviewed           INTEGER DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_generation_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id  TEXT NOT NULL,
            reason        TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        )
        """
    )
    # UNIQUE(objective_id, reason) backs the idempotent upsert in
    # _queue_insufficient. Try/except: if the live DB still holds duplicate pairs
    # the CREATE fails -- the one-off cleanup dedupes first, then this succeeds.
    try:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_lgq_objective_reason "
            "ON lesson_generation_queue(objective_id, reason)"
        )
    except sqlite3.OperationalError:
        pass
    db.commit()


def locked_subject_objectives(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    """Every objective in a LOCKED subject, ordered by id.

    Carries the full set of fields the Lesson Structurer prompt expects as input
    (objective_num, exam_weight, and the parent section_title), so _compose_lesson
    can build the input JSON without further queries.
    """
    rows = db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.command_words, o.skill_type,
               o.objective_num, o.exam_weight, sec.title AS section_title
        FROM   objectives o
        JOIN   subjects subj ON subj.subject_id = o.subject_id
        LEFT   JOIN syllabus_sections sec ON sec.section_id = o.section_id
        WHERE  o.subject_id = ?
          AND  subj.syllabus_locked = 1
        ORDER  BY o.objective_id
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _lesson_exists(db: sqlite3.Connection, objective_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM objective_lessons WHERE objective_id = ? LIMIT 1",
        (objective_id,),
    ).fetchone()
    return row is not None


def _vec_search(db: sqlite3.Connection, table: str, query_vec: bytes,
                subject_id: str, k: int) -> list[dict]:
    """Top-k subject-filtered neighbours from a vec_* table, joined back to chunks.

    Each returned chunk is tagged with its source vec table so the caller can count
    notes vs papers vs schemes for the confidence floor. NOTE the `AND k = ?` form:
    sqlite-vec kNN with a JOIN needs the k constraint, not LIMIT (v0.1.9+).
    """
    rows = db.execute(
        f"""
        SELECT c.id, c.chunk_id, c.chunk_text, c.doc_id, c.page, d.source_file
        FROM   {table} v
        JOIN   chunks c    ON c.id = v.rowid
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  v.embedding MATCH ?
          AND  k = ?
          AND  v.rowid IN (SELECT id FROM chunks WHERE subject_id = ?)
        ORDER  BY v.distance
        """,
        (query_vec, k, subject_id),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["vec_table"] = table
        out.append(d)
    return out


def candidate_chunks(db: sqlite3.Connection, subject_id: str, objective: dict,
                     embed_fn=ollama_embed) -> list[dict]:
    """Source chunks to ground the lesson in, ordered notes-first.

    Top-NOTES_K from vec_notes on the objective's content_stmt, ALWAYS followed by
    top-ADDITIVE_K from vec_past_papers AND vec_mark_schemes (same query), de-duplicated
    by chunk.id and appended after the notes so notes stay primary. The mark-scheme/
    past-paper pull is additive (runs for every objective, not just when notes < 2):
    a noisy/heading-only notes top-k can crowd out the genuine teaching content that
    lives in answer keys and past papers. Notes remain first and still drive
    local_confidence_floor. Returns [] when the objective has no content_stmt.
    """
    query = objective.get("content_stmt")
    if not query:
        return []

    query_vec = serialize_vec(embed_fn(query))
    chunks = _vec_search(db, NOTES_TABLE, query_vec, subject_id, NOTES_K)

    seen = {c["id"] for c in chunks}
    for table in (PAST_PAPERS_TABLE, MARK_SCHEMES_TABLE):
        for c in _vec_search(db, table, query_vec, subject_id, ADDITIVE_K):
            if c["id"] not in seen:
                chunks.append(c)
                seen.add(c["id"])
    return chunks


def _command_words_array(command_words) -> list:
    """objectives.command_words (a JSON array string) -> Python list ([] if absent)."""
    if not command_words:
        return []
    try:
        parsed = json.loads(command_words)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        return []


def local_confidence_floor(chunks: list[dict]) -> int:
    """Cap derived from how much REAL source material backed the lesson.

    More notes -> a higher floor. With no notes and only mark schemes used, drop
    20 (a mark scheme is the weakest base for a lesson). Never below 30, so the
    floor only ever LOWERS an over-confident model, never raises a poor lesson.
    """
    notes_used = sum(1 for c in chunks if c["vec_table"] == NOTES_TABLE)
    papers_used = sum(1 for c in chunks if c["vec_table"] == PAST_PAPERS_TABLE)
    schemes_used = sum(1 for c in chunks if c["vec_table"] == MARK_SCHEMES_TABLE)

    if notes_used >= 3:
        base = 90
    elif notes_used == 2:
        base = 70
    elif notes_used == 1:
        base = 50
    else:
        base = 30

    if notes_used == 0 and papers_used == 0 and schemes_used > 0:
        base -= 20

    return max(base, 30)


def _build_lesson_input(subject_id: str, objective: dict, chunks: list[dict]) -> dict:
    """Assemble the JSON input the Lesson Structurer prompt documents."""
    return {
        "subject": _normalize_subject_id(subject_id),
        "section_title": objective.get("section_title") or "",
        "objective_id": objective.get("objective_id") or "",
        "objective_num": objective.get("objective_num") or "",
        "content_stmt": objective.get("content_stmt") or "",
        "skill_type": objective.get("skill_type") or "",
        "command_words": _command_words_array(objective.get("command_words")),
        "exam_weight": objective.get("exam_weight") or "",
        "source_excerpts": [
            {
                "text": c.get("chunk_text", ""),
                "source_file": c.get("source_file") or "",
                "page": c.get("page"),
            }
            for c in chunks
        ],
    }


def _parse_lesson_json(raw: str) -> dict | None:
    """Parse the model's JSON reply, tolerating ```json fences / surrounding prose.

    Returns the dict, or None if no JSON object can be recovered.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        # Last resort: slice between the first '{' and the last '}'.
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j <= i:
            return None
        try:
            data = json.loads(s[i:j + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


# Tool-use schema for lesson composition. Passing this (rather than schema=None)
# routes anthropic_chat through Anthropic's tool-use path, which returns
# json.dumps(block.input) -- structurally valid JSON the SDK serialises, so literal
# quotes/newlines in lesson_text can no longer break parsing (the POB-6.6 failure
# class). A single object covers BOTH output shapes: 'ok' fills lesson_text /
# active_recall_question / sources_used; 'insufficient_source' fills reason. Only the
# three always-present fields are required so the model can omit the rest per status.
LESSON_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "insufficient_source"]},
        "subject": {"type": "string"},
        "objective_ref": {"type": "string"},
        "lesson_text": {"type": "string"},
        "active_recall_question": {"type": "string"},
        "sources_used": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reason": {"type": "string"},
    },
    "required": ["status", "subject", "objective_ref"],
}


def _compose_lesson(subject_id: str, objective: dict, chunks: list[dict],
                    chat_fn) -> dict | None:
    """Ask Claude Sonnet (via chat_fn) to compose the lesson from the source chunks.

    The Lesson Structurer prompt is the system prompt; the user message is the input
    JSON the prompt documents. LESSON_OUTPUT_SCHEMA is passed so anthropic_chat uses
    Anthropic's TOOL-USE path -- the SDK then returns structurally valid JSON
    (json.dumps of the tool input), so a lesson that legitimately quotes a phrase
    (e.g. 'two for the price of one.') no longer breaks json.loads on unescaped
    quotes. _parse_lesson_json still runs (it cleanly parses the valid JSON, and the
    fence/brace-slice fallbacks stay as defence for the Ollama-fallback path).
    None on parse failure.
    """
    lesson_input = _build_lesson_input(subject_id, objective, chunks)
    raw = chat_fn([{"role": "user", "content": json.dumps(lesson_input)}],
                  system=LESSON_STRUCTURER_PROMPT, schema=LESSON_OUTPUT_SCHEMA)
    return _parse_lesson_json(raw)


def _queue_insufficient(db: sqlite3.Connection, objective_id: str,
                        dry_run: bool) -> None:
    """Idempotently flag an objective as needing sources.

    ON CONFLICT(objective_id, reason) means a re-run REFRESHES created_at on the
    existing row rather than stacking a duplicate -- so the queue stays one row per
    objective-per-reason of genuine outstanding work.
    """
    if not dry_run:
        db.execute(
            "INSERT INTO lesson_generation_queue (objective_id, reason) "
            "VALUES (?, ?) "
            "ON CONFLICT(objective_id, reason) DO UPDATE SET created_at = datetime('now')",
            (objective_id, QUEUE_REASON),
        )


def _queue_insufficient_source(db: sqlite3.Connection, objective_id: str,
                               reason: str, dry_run: bool) -> None:
    """Queue an objective whose composed lesson came back status='insufficient_source'.

    Distinct from the zero-chunks case (reason 'insufficient_sources'): here the model
    SAW source material but judged it too thin to teach honestly. The model's one-line
    reason is folded into the queue reason ('insufficient_source: <reason>').
    Idempotent upsert on (objective_id, reason).
    """
    if not dry_run:
        full_reason = "insufficient_source: " + (reason or "model judged source insufficient")
        db.execute(
            "INSERT INTO lesson_generation_queue (objective_id, reason) "
            "VALUES (?, ?) "
            "ON CONFLICT(objective_id, reason) DO UPDATE SET created_at = datetime('now')",
            (objective_id, full_reason),
        )


def _queue_quality_failed(db: sqlite3.Connection, objective_id: str,
                          reason: str, dry_run: bool) -> None:
    """Queue an objective whose composed lesson failed the quality gate, so it is
    re-attempted later instead of a broken lesson being written. The specific failure
    is folded into the reason ('quality_check_failed: <why>')."""
    if not dry_run:
        full_reason = "quality_check_failed: " + (reason or "unknown")
        db.execute(
            "INSERT INTO lesson_generation_queue (objective_id, reason) "
            "VALUES (?, ?) "
            "ON CONFLICT(objective_id, reason) DO UPDATE SET created_at = datetime('now')",
            (objective_id, full_reason),
        )


def ingest_lessons_for_subject(db: sqlite3.Connection, subject_id: str, *,
                               regenerate: bool = False,
                               confidence_floor: int = DEFAULT_CONFIDENCE_FLOOR,
                               chat_fn=None, embed_fn=ollama_embed,
                               dry_run: bool = False, verbose: bool = True,
                               objective_ids: list[str] | None = None) -> dict:
    """Compose canonical lessons for every objective in a locked subject.

    chat_fn defaults to local Ollama (offline build composition). Tests inject it.
    `objective_ids`, when given, restricts the run to those objectives (session 4:
    regenerate only the stale lessons the user asked for). Returns a summary dict.
    Side-effect free under dry_run.
    """
    # Canonical subject id for both the DB query and the prompt input (raises on a
    # bad subject -- fail fast rather than silently composing nothing).
    subject_id = _normalize_subject_id(subject_id)
    ensure_lesson_tables(db)
    if chat_fn is None:
        # Default: route to Claude Sonnet via the Anthropic API (PDR v3.2). Falls
        # back to Ollama inside the router when no ANTHROPIC_API_KEY is present.
        # Tests inject their own chat_fn, so this default never runs under test.
        chat_fn = chat_for_lesson_composition

    objectives = locked_subject_objectives(db, subject_id)
    if objective_ids is not None:
        wanted = set(objective_ids)
        objectives = [o for o in objectives if o["objective_id"] in wanted]
    summary = {
        "subject_id": subject_id,
        "regenerate": regenerate,
        "confidence_floor": confidence_floor,
        "objectives_total": len(objectives),
        "written": 0,
        "queued": 0,
        "skipped": 0,
        "errored": 0,
        "cleared": 0,  # stale queue rows deleted when a lesson was successfully written
        "rows": [],
    }

    if verbose:
        print(f"\nCanonical lessons -- {subject_id} "
              f"({len(objectives)} objective(s) in a locked subject)"
              f"{'  [DRY RUN]' if dry_run else ''}\n")
        print(f"  {'objective_id':<16}{'chunks':>7}  {'sources':<18}"
              f"{'conf':>4}{'cleared':>8}  status")
        print("  " + "-" * 68)

    for obj in objectives:
        oid = obj["objective_id"]
        # Commit this objective's net change before moving on (try/finally below), so
        # a later failure -- e.g. a single Ollama ReadTimeout deep in the subject --
        # never rolls back the lessons already written. A re-run skips what landed.
        try:
            # (a) An existing lesson is skipped unless --regenerate. The actual DELETE
            # is deferred to the write path (f) so a compose failure on a regenerate
            # cannot destroy a good lesson and leave nothing in its place.
            if _lesson_exists(db, oid) and not regenerate:
                _record(summary, oid, 0, "", None, "skipped_exists", verbose)
                summary["skipped"] += 1
                continue

            # (b) Retrieve source chunks (notes first, fallback to papers + schemes).
            try:
                chunks = candidate_chunks(db, subject_id, obj, embed_fn=embed_fn)
            except Exception:  # embedding/search failure for one objective is non-fatal
                _record(summary, oid, 0, "", None, "errored", verbose)
                summary["errored"] += 1
                continue

            sources = _source_label(chunks)

            # No source, no lesson (Stage 11 constraint): queue, do not compose blind.
            if not chunks:
                _queue_insufficient(db, oid, dry_run)
                _record(summary, oid, 0, sources, None, "queued", verbose)
                summary["queued"] += 1
                continue

            # (c)/(d) Compose from the source material via Claude Sonnet. A network/
            # timeout/parse error on ONE objective must not abort the whole subject
            # pass -- record it and move on.
            try:
                data = _compose_lesson(subject_id, obj, chunks, chat_fn)
            except Exception:
                data = None
            # Token usage for THIS objective's compose call (None,None on the
            # Ollama-fallback/test path). Captured once, reused on every _record below.
            tokens = _last_token_usage()
            if data is None:
                _record(summary, oid, len(chunks), sources, None, "errored", verbose,
                        tokens=tokens)
                summary["errored"] += 1
                continue

            # (d2) The model saw source but judged it too thin to teach honestly
            # (prompt rule 2). Queue with the model's reason; never write thin prose.
            if data.get("status") == "insufficient_source":
                reason = (data.get("reason") or "model judged source insufficient").strip()
                _queue_insufficient_source(db, oid, reason, dry_run)
                _record(summary, oid, len(chunks), sources, None, "queued", verbose,
                        tokens=tokens)
                summary["queued"] += 1
                if verbose:
                    print(f"    {oid}: insufficient_source -- {reason}")
                continue

            # New lesson format (PDR v3.2): lesson_text ends with a 'Q: ' line and the
            # single active-recall question is returned separately. Wrap it in a
            # one-element list for the existing recall_questions JSON column (the UI
            # handles both the new 1-question and legacy 3-question shapes).
            lesson_text = data.get("lesson_text", "") or ""
            recall_q = (data.get("active_recall_question") or "").strip()
            recall_questions = [recall_q] if recall_q else []

            # (e) Confidence: the source-quality floor (the model no longer self-reports
            # a confidence in this format). The floor only ever LOWERS, never inflates.
            floor = local_confidence_floor(chunks)
            final_conf = floor

            if final_conf < confidence_floor:
                _queue_insufficient(db, oid, dry_run)
                _record(summary, oid, len(chunks), sources, final_conf, "queued", verbose,
                        tokens=tokens)
                summary["queued"] += 1
                continue

            # (e2) Quality gate: reject semantically-broken output (chat boilerplate,
            # 'According to Section N' citations, a skeleton lesson under 300 words, a
            # bad/duplicated recall question). A failure is queued, never written.
            ok, why = _validate_lesson_quality(lesson_text, recall_questions,
                                               obj.get("command_words"))
            if not ok:
                _queue_quality_failed(db, oid, why, dry_run)
                _record(summary, oid, len(chunks), sources, final_conf, "queued", verbose,
                        tokens=tokens)
                summary["queued"] += 1
                if verbose:
                    print(f"    {oid}: quality_check_failed -- {why}")
                continue

            # (f) Write the lesson. lesson_id = sha256(objective_id|generated_at)[:16].
            # On --regenerate, delete the old row here -- only now that a good new
            # lesson is in hand -- then insert (UNIQUE(objective_id) is satisfied).
            # The matching queue rows are deleted in the SAME transaction as the
            # insert (the per-objective commit is in the finally below), so a failed
            # insert rolls the queue delete back too -- the objective stays flagged.
            cleared = 0
            if not dry_run:
                if regenerate:
                    db.execute("DELETE FROM objective_lessons WHERE objective_id = ?", (oid,))
                generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lesson_id = hashlib.sha256(
                    f"{oid}|{generated_at}".encode("utf-8")
                ).hexdigest()[:16]
                source_chunk_ids = [c["chunk_id"] for c in chunks]
                db.execute(
                    """
                    INSERT INTO objective_lessons
                        (lesson_id, objective_id, subject_id, lesson_text,
                         worked_examples, key_terms, common_mistakes,
                         recall_questions, source_chunk_ids, confidence, generated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lesson_id, oid, subject_id, lesson_text,
                        # worked_examples / key_terms / common_mistakes are not
                        # separate fields in the v2 format -- worked examples are
                        # embedded in lesson_text. Stored empty for schema compatibility.
                        json.dumps([]),
                        json.dumps([]),
                        "",
                        json.dumps(recall_questions),
                        json.dumps(source_chunk_ids),
                        final_conf, generated_at,
                    ),
                )
                # This objective is now covered -- drop any queue rows flagging it.
                cur = db.execute(
                    "DELETE FROM lesson_generation_queue WHERE objective_id = ?", (oid,)
                )
                cleared = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

            summary["cleared"] += cleared
            _record(summary, oid, len(chunks), sources, final_conf, "written", verbose,
                    cleared=cleared, tokens=tokens)
            summary["written"] += 1
        finally:
            if not dry_run:
                db.commit()

    if verbose:
        _print_totals(summary)
    return summary


def _last_token_usage():
    """(input_tokens, output_tokens) from the most recent Anthropic composition call.

    Reads anthropic_client.LAST_USAGE (set per call). Returns (None, None) when
    composition ran on the Ollama fallback or under test (anthropic never called),
    so the summary line just omits the token suffix.
    """
    try:
        import anthropic_client
        u = anthropic_client.LAST_USAGE
        return u.get("input_tokens"), u.get("output_tokens")
    except Exception:
        return None, None


def _source_label(chunks: list[dict]) -> str:
    """Comma-separated short source names in notes,papers,schemes order."""
    present = {c["vec_table"] for c in chunks}
    ordered = [SOURCE_NAMES[t] for t in
               (NOTES_TABLE, PAST_PAPERS_TABLE, MARK_SCHEMES_TABLE) if t in present]
    return ",".join(ordered)


def _record(summary: dict, oid: str, chunks_used: int, sources: str,
            confidence, status: str, verbose: bool, cleared: int = 0,
            tokens=None) -> None:
    in_tok, out_tok = (tokens or (None, None))
    summary["rows"].append({
        "objective_id": oid, "chunks_used": chunks_used, "sources": sources,
        "confidence": confidence, "status": status, "cleared": cleared,
        "input_tokens": in_tok, "output_tokens": out_tok,
    })
    if verbose:
        conf = "  --" if confidence is None else f"{confidence:>4}"
        tokstr = f"  tok={in_tok}/{out_tok}" if in_tok is not None else ""
        print(f"  {oid:<16}{chunks_used:>7}  {sources:<18}{conf}{cleared:>8}  {status}{tokstr}")


def _print_totals(summary: dict) -> None:
    print("  " + "-" * 68)
    print(f"  written: {summary['written']}   queued: {summary['queued']}   "
          f"skipped: {summary['skipped']}   errored: {summary['errored']}   "
          f"cleared: {summary['cleared']}")
    if summary["regenerate"]:
        print("  (--regenerate: existing lessons were replaced)")
    print()


def _open_live_db() -> sqlite3.Connection:
    """Open the SSD DB the same way the app does (sqlite-vec + FKs)."""
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("ERROR: sqlite-vec is not installed. Run: pip install sqlite-vec")
    db_path = os.getenv("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")
    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


@backup_first("pre_ingest_lessons")
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-generate one canonical lesson per objective (offline)."
    )
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Principles_of_Business")
    parser.add_argument("--regenerate", action="store_true",
                        help="Replace existing lessons instead of skipping them.")
    parser.add_argument("--confidence-floor", type=int,
                        default=DEFAULT_CONFIDENCE_FLOOR,
                        help="Lessons below this final confidence are queued, not written.")
    parser.add_argument("--objectives",
                        help="Comma-separated objective_ids to restrict the run to "
                             "(targeted regeneration), e.g. POB-1.11,POB-3.1. "
                             "Omit to run every objective in the subject.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen; change nothing.")
    args = parser.parse_args()

    objective_ids = None
    if args.objectives:
        objective_ids = [o.strip() for o in args.objectives.split(",") if o.strip()]

    db = _open_live_db()
    try:
        ingest_lessons_for_subject(
            db, args.subject,
            regenerate=args.regenerate,
            confidence_floor=args.confidence_floor,
            dry_run=args.dry_run,
            objective_ids=objective_ids,
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
