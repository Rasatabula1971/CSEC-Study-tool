"""
backend/app.py
==============
Stage 6 FastAPI entry point. Wraps the deterministic controller in JSON endpoints
and serves the single-page chat UI (backend/static/chat.html). The live system is
exactly this app + Ollama (CLAUDE.md).

Lifespan startup:
  1. Verify the SSD is mounted (sys.exit with a clear message if not).
  2. ollama_health() -- log a warning if down, but keep running (the UI still
     loads; chat calls surface the Ollama error).
  3. Open the SSD DB once and stash it on app.state.db.

Run (dev):
    python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
"""

import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# backend/ on sys.path so the bare module imports below resolve whether the app
# is launched as `backend.app:app` or imported directly in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_health, ollama_chat, ollama_embed  # noqa: E402
from gemini_client import is_gemini_available, gemini_key_valid  # noqa: E402
from controller import handle_request  # noqa: E402
from schedule import get_due_objectives  # noqa: E402
from study_plan import get_plan_progress  # noqa: E402
from export_progress import export_progress, fetch_progress  # noqa: E402
from notes import classify_notes, save_notes  # noqa: E402
from extract import detect_mime_type, extract_text  # noqa: E402

logger = logging.getLogger("csec.app")
# Ensure our startup INFO lines (Ollama / Gemini status) actually surface: the
# logger otherwise has no handler and defaults to WARNING, so INFO is dropped.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

STATIC_DIR = Path(__file__).resolve().parent / "static"
WELCOME_HTML = STATIC_DIR / "welcome.html"
CHAT_HTML = STATIC_DIR / "chat.html"
QUIZ_HTML = STATIC_DIR / "quiz.html"
PLAN_HTML = STATIC_DIR / "study_plan.html"


# Idempotent runtime migration: ensures tables added after a DB was first created
# exist on the live SSD DB without a re-init. CREATE TABLE IF NOT EXISTS is a no-op
# when the table is already present (the canonical schema lives in db/schema.sql).
RUNTIME_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS practice_questions (
        question_id   TEXT PRIMARY KEY,
        objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
        subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
        stem          TEXT NOT NULL,
        created_at    TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_plan (
        plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
        objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
        status        TEXT NOT NULL DEFAULT 'unmet',
        met_count     INTEGER NOT NULL DEFAULT 0,
        last_met_at   TEXT,
        created_at    TEXT DEFAULT (datetime('now')),
        UNIQUE(subject_id, objective_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_batches (
        batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
        objective_ids   TEXT NOT NULL,
        synthesis_qid   TEXT,
        status          TEXT NOT NULL DEFAULT 'active',
        created_at      TEXT DEFAULT (datetime('now')),
        completed_at    TEXT
    )
    """,
)


def open_db(db_path: str) -> sqlite3.Connection:
    """Open the SSD DB with sqlite-vec loaded and FKs on (same pattern as init_db)."""
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("ERROR: sqlite-vec is not installed. Run: pip install sqlite-vec")
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def apply_runtime_migrations(db: sqlite3.Connection) -> None:
    """Run idempotent migrations against the live DB: schema (CREATE TABLE IF NOT
    EXISTS) plus a data-normalisation pass. Safe to run on every startup."""
    for stmt in RUNTIME_MIGRATIONS:
        db.execute(stmt)
    # Data migration: normalise question_id to the -stem convention used by
    # ingest_solutions.py. Old PDF-ingester rows stored question_id without
    # the suffix; this makes the grade-picker join work for all rows.
    db.execute(
        """
        UPDATE mark_points
        SET    question_id = question_id || '-stem'
        WHERE  question_id NOT LIKE '%-stem'
        """
    )
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ssd_root = os.getenv("SSD_ROOT")
    if ssd_root and not os.path.exists(ssd_root):
        sys.exit(f"ERROR: SSD not mounted at {ssd_root}. Plug in the drive and restart.")

    if ollama_health():
        # Pre-warm: one tiny chat call loads the 3B chat model into RAM now (held by
        # ollama_chat's keep_alive=30m), so the first Submit of the session doesn't
        # pay the cold model-load tax. Non-fatal -- a failure just warns.
        try:
            ollama_chat([{"role": "user", "content": "ready"}],
                        system="Respond with one word: ready.")
        except Exception as exc:
            logger.warning("Ollama pre-warm failed (%s) -- first response may be slow.", exc)
    else:
        logger.warning("Ollama is not reachable at %s -- study mode will surface the "
                        "error. Starting the app anyway.", os.getenv("OLLAMA_BASE"))

    # Validate the key once, here, with a single live ping -- not on every
    # /api/status request. The result drives the UI's grading indicator so it
    # reflects whether grading will REALLY reach Gemini, not just that a key exists.
    if is_gemini_available():
        if gemini_key_valid():
            app.state.gemini_ok = True
            logger.info("Gemini API key valid -- grading calls will use Gemini Flash "
                        "with Ollama fallback")
        else:
            app.state.gemini_ok = False
            logger.warning("Gemini API key present but REJECTED by Google (invalid or "
                           "expired) -- grading falls back to local Ollama")
    else:
        app.state.gemini_ok = False
        logger.info("No Gemini API key -- all calls use local Ollama")

    db_path = os.getenv("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")
    app.state.db = open_db(db_path)
    apply_runtime_migrations(app.state.db)
    try:
        yield
    finally:
        app.state.db.close()


app = FastAPI(title="CSEC AI Study Partner", lifespan=lifespan)

# Serve the static assets (the chat UI lives here).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    """A chat turn. `message` is the student's text; `route` selects the workflow.

    message and subject_id are required and non-empty (missing OR empty -> 422).
    Optional fields (question_id, objective_id, paper, year, question_num,
    content_type) pass through to the controller when present -- e.g. a grade
    turn supplies question_id.
    """
    message: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    route: str
    question_id: str | None = None
    objective_id: str | None = None
    paper: str | None = None
    year: int | None = None
    question_num: str | None = None
    content_type: str | None = None


class StartBatchRequest(BaseModel):
    """Open a new study-plan batch for a subject."""
    subject_id: str = Field(min_length=1)


class BatchQuestionRequest(BaseModel):
    """Ask for the question at one step of a batch. step = "1".."N" | "synthesis".

    lesson_context: the lesson text the student just read (already stripped of its
    own trailing question). When present, the generator is constrained to test what
    that lesson actually taught, so the question card stays aligned with the lesson.
    """
    batch_id: int
    step: str = Field(min_length=1)
    lesson_context: str | None = None


class GradeBatchRequest(BaseModel):
    """Grade one batch answer (per-objective or synthesis).

    Per-objective steps (single-call architecture) send objective_id + question_text:
    the question was extracted from the lesson client-side, so there is no stored
    question to resolve. Synthesis (and the no-lesson fallback) still send question_id.
    """
    batch_id: int
    question_id: str | None = None
    objective_id: str | None = None
    question_text: str | None = None
    answer: str = Field(min_length=1)


class MissedPoint(BaseModel):
    """One mark point the student missed, as returned in a grade result."""
    mark_point_id: str | None = None
    expected: str | None = None
    evidence: str | None = None


class ExplainMissedRequest(BaseModel):
    """Ask for a plain-language explanation of the points missed on one objective."""
    subject_id: str = Field(min_length=1)
    objective_id: str = Field(min_length=1)
    missed_points: list[MissedPoint] = []


class ClassifyNotesRequest(BaseModel):
    """Classify pasted/uploaded note text: which subject + objectives it belongs to."""
    text: str = Field(min_length=1, max_length=50000)
    available_subjects: list[str] = []


# ---------------------------------------------------------------------------
# UI compatibility shim
# ---------------------------------------------------------------------------
def _shape_for_ui(result: dict) -> dict:
    """Add aliases the chat UI expects, without touching the controller output.

    - plan: the UI reads `objectives`; the controller returns `tasks`.
    - grade: the UI reads top-level `leitner_box`/`next_review`; the controller
      nests them under `weakness`.
    These are additive (the original keys remain) and presentation-only.
    """
    if not isinstance(result, dict):
        return result
    if "tasks" in result and "objectives" not in result:
        result["objectives"] = result["tasks"]
    weakness = result.get("weakness")
    if isinstance(weakness, dict):
        result.setdefault("leitner_box", weakness.get("leitner_box"))
        result.setdefault("next_review", weakness.get("next_review"))
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/quiz")
def quiz_page() -> FileResponse:
    """Serve the dedicated full-page quiz experience (backend/static/quiz.html)."""
    return FileResponse(QUIZ_HTML)


@app.get("/plan")
def plan_page() -> FileResponse:
    """Serve the standalone Study Plan page (backend/static/study_plan.html)."""
    return FileResponse(PLAN_HTML)


@app.get("/")
def index() -> FileResponse:
    """The Welcome page is the app's front door (greeting, add-notes, navigation)."""
    return FileResponse(WELCOME_HTML)


@app.get("/chat")
def chat_page() -> FileResponse:
    """The tutor chat UI, moved here from / when the Welcome page took the root."""
    return FileResponse(CHAT_HTML)


@app.get("/health")
def health(request: Request) -> dict:
    db_ok = getattr(request.app.state, "db", None) is not None
    return {"status": "ok", "ollama": ollama_health(), "db": db_ok}


@app.get("/api/status")
def status(request: Request) -> dict:
    """Which engine grading will really use, for the UI's status indicator.

    `gemini` reflects key VALIDITY (verified once at startup, stored on
    app.state.gemini_ok), not mere presence -- so an invalid key honestly shows
    local grading instead of falsely claiming the cloud. grading_engine: 'gemini'
    if the key works, else 'ollama' if Ollama is up, else 'unavailable'.
    """
    ollama_up = ollama_health()
    gemini_ok = bool(getattr(request.app.state, "gemini_ok", False))
    if gemini_ok:
        engine = "gemini"
    elif ollama_up:
        engine = "ollama"
    else:
        engine = "unavailable"
    return {"ollama": ollama_up, "gemini": gemini_ok, "grading_engine": engine}


@app.get("/api/subjects")
def subjects(request: Request) -> list[dict]:
    """Locked subjects only -- the UI must not offer a subject that is out of scope."""
    rows = request.app.state.db.execute(
        "SELECT subject_id, display_name FROM subjects WHERE syllabus_locked = 1 "
        "ORDER BY display_name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/due/{subject_id}")
def due(subject_id: str, request: Request) -> list[dict]:
    return get_due_objectives(request.app.state.db, subject_id)


@app.get("/api/questions/{subject_id}")
def questions(subject_id: str, request: Request) -> list[dict]:
    """Gradeable questions for a subject -- those that have mark points.

    Powers the grade-mode question picker: each entry carries the question_id
    grade.fetch_mark_points keys on, the question prose (stem chunk), and a
    human label. Marks = number of mark points for that question.
    """
    rows = request.app.state.db.execute(
        """
        SELECT mp.question_id            AS question_id,
               mp.objective_id           AS objective_id,
               c.chunk_text              AS question_text,
               c.question_num            AS question_num,
               d.paper                   AS paper,
               d.year                    AS year,
               COUNT(mp.mark_point_id)   AS marks
        FROM   mark_points mp
        JOIN   documents d ON d.doc_id = mp.doc_id
        LEFT   JOIN chunks c ON c.chunk_id = mp.question_id
        WHERE  d.subject_id = ?
        GROUP  BY mp.question_id
        ORDER  BY d.year DESC, d.paper, mp.question_id
        """,
        (subject_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # paper already carries the year ("Paper 2 - January 2026"), so don't
        # prefix it again -- keep the label free of a duplicated year.
        d["label"] = f"{d['paper']} · Q{d['question_num'] or ''}".strip()
        out.append(d)
    return out


@app.get("/api/questions")
def questions_by_filter(
    request: Request,
    subject_id: str,
    paper: str | None = None,
    year: int | None = None,
) -> list[dict]:
    """Past-paper questions for the dedicated quiz page (/quiz).

    Filters chunks by subject, content_type in (past_paper, mark_scheme), a
    present question_num, and a solution-derived chunk_id ('...-stem'), joined to
    documents for paper/year. The '-stem' filter hides papers whose chunks came
    from ingest.py's MCQ chunker (garbled for older Paper 2 PDFs) until that
    chunker is rewritten -- only ingest_solutions.py questions appear. `paper`
    and `year` are optional query params that narrow the list further. Each row
    carries the question stem (first 400 chars) and a marks_total = number of
    mark points keyed on that question. Returns [] when nothing matches -- never
    404.
    """
    sql = [
        "SELECT c.chunk_id      AS question_id,",
        "       c.question_num  AS question_num,",
        "       d.paper         AS paper,",
        "       d.year          AS year,",
        "       SUBSTR(c.chunk_text, 1, 400) AS stem,",
        "       (SELECT COUNT(*) FROM mark_points mp",
        "          WHERE mp.question_id = c.chunk_id) AS marks_total",
        "FROM   chunks c",
        "JOIN   documents d ON d.doc_id = c.doc_id",
        "WHERE  c.subject_id = ?",
        "  AND  d.content_type IN ('past_paper', 'mark_scheme')",
        "  AND  c.question_num IS NOT NULL",
        # Only show solution-derived questions (chunk_id like 'POB-...-stem',
        # the ingest_solutions.py convention). ingest.py's MCQ chunker still
        # produces garbled chunks for older Paper 2 PDFs -- missing stems,
        # options split across chunks, OCR artifacts ("U nski lied") -- so its
        # auto-generated chunk_ids are excluded until the chunker is rewritten.
        "  AND  c.chunk_id LIKE '%-stem'",
    ]
    params: list = [subject_id]
    if paper:
        sql.append("  AND  d.paper = ?")
        params.append(paper)
    if year is not None:
        sql.append("  AND  d.year = ?")
        params.append(year)
    sql.append("ORDER BY d.year DESC, c.question_num ASC")
    rows = request.app.state.db.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/filters")
def filters(request: Request, subject_id: str) -> dict:
    """Distinct paper/year values that actually have questions for a subject.

    Powers the quiz-page Paper and Year dropdowns: only values that the
    /api/questions query can return (past_paper OR mark_scheme chunks with a
    question_num AND a solution-derived '-stem' chunk_id) appear, so a selected
    paper/year always yields well-formed questions. `papers` is sorted
    alphabetically, `years` descending. Returns empty lists when the subject has
    no questions -- never 404.
    """
    db = request.app.state.db
    papers = db.execute(
        """
        SELECT DISTINCT d.paper AS paper
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme')
          AND  c.question_num IS NOT NULL
          -- Only papers with solution-derived ('-stem') chunks; ingest.py's
          -- MCQ chunker yields garbled chunks for older Paper 2 PDFs, so its
          -- papers are hidden until that chunker is rewritten. Mirrors the
          -- /api/questions filter so a selected paper always yields questions.
          AND  c.chunk_id LIKE '%-stem'
          AND  d.paper IS NOT NULL
        ORDER  BY d.paper ASC
        """,
        (subject_id,),
    ).fetchall()
    years = db.execute(
        """
        SELECT DISTINCT d.year AS year
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme')
          AND  c.question_num IS NOT NULL
          -- Only years with solution-derived ('-stem') chunks; see papers query.
          AND  c.chunk_id LIKE '%-stem'
          AND  d.year IS NOT NULL
        ORDER  BY d.year DESC
        """,
        (subject_id,),
    ).fetchall()
    return {
        "papers": [r["paper"] for r in papers],
        "years": [r["year"] for r in years],
    }


@app.get("/api/sections")
def sections(request: Request, subject_id: str) -> list[dict]:
    """Syllabus sections (with their objectives) for the quiz-page Practice mode.

    Powers the SECTION and OBJECTIVE dropdowns: each section carries its objectives
    nested, so choosing a section populates the objective list with no extra round
    trip. Returns [] for an unknown subject -- never 404.
    """
    db = request.app.state.db
    secs = db.execute(
        "SELECT section_id, title, section_num FROM syllabus_sections "
        "WHERE subject_id = ? ORDER BY section_num",
        (subject_id,),
    ).fetchall()
    out = []
    for s in secs:
        objs = db.execute(
            "SELECT objective_id, objective_num, content_stmt FROM objectives "
            "WHERE section_id = ? ORDER BY objective_num",
            (s["section_id"],),
        ).fetchall()
        d = dict(s)
        d["objectives"] = [dict(o) for o in objs]
        out.append(d)
    return out


@app.get("/api/objectives/{subject_id}")
def objectives(subject_id: str, request: Request) -> list[dict]:
    """All objectives for a subject, in syllabus order (section_num, objective_num).

    Powers the Welcome-page manual-pick fallback (subject + objective dropdowns)
    when note classification can't determine the subject. Returns [] for an unknown
    subject -- never 404.
    """
    rows = request.app.state.db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.objective_num,
               s.title       AS section_title,
               s.section_num AS section_num
        FROM   objectives o
        JOIN   syllabus_sections s ON s.section_id = o.section_id
        WHERE  o.subject_id = ?
        ORDER  BY s.section_num, o.objective_num
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/chat")
def chat(body: ChatRequest, request: Request) -> dict:
    """Map a chat turn onto the controller's request shape and return its result."""
    req = body.model_dump(exclude_none=True)
    # The controller reads `query` (teach) and `student_answer` (grade); both come
    # from the single message box in the UI.
    req["query"] = body.message
    req["student_answer"] = body.message
    result = handle_request(request.app.state.db, req)
    return _shape_for_ui(result)


# ---------------------------------------------------------------------------
# Study Plan endpoints
# ---------------------------------------------------------------------------
@app.post("/api/plan/start_batch")
def plan_start_batch(body: StartBatchRequest, request: Request) -> dict:
    """Seed the plan (idempotent) and open the next batch of objectives."""
    req = {"route": "start_batch", "subject_id": body.subject_id}
    return handle_request(request.app.state.db, req)


@app.post("/api/plan/batch_question")
def plan_batch_question(body: BatchQuestionRequest, request: Request) -> dict:
    """Generate the question for one step of a batch (per-objective or synthesis)."""
    req = {"route": "batch_question", "batch_id": body.batch_id, "step": body.step}
    if body.lesson_context:
        req["lesson_context"] = body.lesson_context
    return handle_request(request.app.state.db, req)


@app.post("/api/plan/grade_batch")
def plan_grade_batch(body: GradeBatchRequest, request: Request) -> dict:
    """Grade one batch answer and return the result plus updated plan progress."""
    req = {
        "route": "grade_batch_question",
        "batch_id": body.batch_id,
        "answer": body.answer,
    }
    # Forward only what was supplied: question_id for synthesis/fallback, or
    # objective_id + question_text for the extracted per-objective question.
    if body.question_id:
        req["question_id"] = body.question_id
    if body.objective_id:
        req["objective_id"] = body.objective_id
    if body.question_text is not None:
        req["question_text"] = body.question_text
    result = handle_request(request.app.state.db, req)
    return _shape_for_ui(result)


@app.post("/api/plan/explain_missed")
def plan_explain_missed(body: ExplainMissedRequest, request: Request) -> dict:
    """Explain the concepts a student missed on one per-objective step.

    Returns {"feedback": "..."}; an empty missed_points list returns
    {"feedback": ""} without an LLM call (the controller short-circuits).
    """
    req = {
        "route": "explain_missed",
        "subject_id": body.subject_id,
        "objective_id": body.objective_id,
        "missed_points": [mp.model_dump() for mp in body.missed_points],
    }
    return handle_request(request.app.state.db, req)


@app.get("/api/plan/progress/{subject_id}")
def plan_progress(subject_id: str, request: Request) -> dict:
    """Mastery counts for a subject's study plan (deterministic aggregation)."""
    return get_plan_progress(request.app.state.db, subject_id)


@app.get("/api/export/progress/{subject_id}")
def export_progress_report(subject_id: str, request: Request) -> FileResponse:
    """Generate a colour-coded study-progress workbook and return it as a download.

    Same logic as backend/export_progress.py (the CLI). Lets the UI add a "Download
    Progress Report" button. Returns the .xlsx as an attachment; 404 if the subject
    has no objectives.
    """
    db = request.app.state.db
    reports_root = os.getenv("REPORTS_ROOT")
    if not reports_root:
        raise HTTPException(status_code=500, detail="REPORTS_ROOT not configured")
    if not fetch_progress(db, subject_id):
        raise HTTPException(status_code=404, detail=f"No objectives for subject '{subject_id}'")
    path = export_progress(db, subject_id, reports_root, date.today().isoformat())
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Add Study Notes (Welcome page)
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB (52428800 bytes)
MAX_NOTE_CHARS = 50_000


def _extract_upload_text(data: bytes, filename: str) -> str:
    """Extract plain text from uploaded file bytes (PDF/DOCX/TXT/JPG/PNG).

    Writes the bytes to a temp file (extract.extract_text and its libraries take a
    path), dispatches on the filename's mime type, then cleans up. Raises
    ValueError -- which the caller maps to a 400 -- for unsupported types or when
    the Tesseract OCR toolchain is missing for an image.
    """
    mime = detect_mime_type(filename)  # ValueError -> caller returns 400
    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return extract_text(str(tmp_path), mime)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/notes/classify")
def notes_classify(body: ClassifyNotesRequest, request: Request) -> dict:
    """Decide which subject + objectives a note excerpt belongs to.

    The LLM picks the subject (schema-constrained); a deterministic cosine pass
    ranks the subject's objectives. Returns subject_id (may be null), confidence,
    reasoning, and up to three suggested_objectives. If no subject is offered,
    falls back to the locked-subject list from the DB.
    """
    available = body.available_subjects
    if not available:
        rows = request.app.state.db.execute(
            "SELECT subject_id FROM subjects WHERE syllabus_locked = 1"
        ).fetchall()
        available = [r["subject_id"] for r in rows]
    return classify_notes(
        request.app.state.db, body.text, available,
        chat_fn=ollama_chat, embed_fn=ollama_embed,
    )


@app.post("/api/notes/classify_file")
async def notes_classify_file(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Extract text from an uploaded file, then classify it like /classify.

    Accepts a PDF, DOCX, TXT, JPG, or PNG. The full text is extracted server-side
    (images via Tesseract OCR); the first 2000 chars feed the same LLM-subject +
    deterministic-objective classifier as /api/notes/classify. Returns that result
    plus `extracted_text_length` so the UI can confirm how much text it read. The
    caller saves by re-sending the file to /api/notes/upload.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")
    try:
        full_text = _extract_upload_text(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    full_text = (full_text or "").strip()
    if not full_text:
        raise HTTPException(
            status_code=422,
            detail="No text could be extracted from the file.",
        )

    rows = request.app.state.db.execute(
        "SELECT subject_id FROM subjects WHERE syllabus_locked = 1"
    ).fetchall()
    available = [r["subject_id"] for r in rows]

    result = classify_notes(
        request.app.state.db, full_text[:2000], available,
        chat_fn=ollama_chat, embed_fn=ollama_embed,
    )
    result["extracted_text_length"] = len(full_text)
    return result


@app.post("/api/notes/upload")
async def notes_upload(request: Request) -> dict:
    """Chunk, embed, and index confirmed notes under a subject + objective.

    Handles two request shapes (the paste path sends JSON, the upload path sends
    multipart/form-data):
      * JSON      -> {subject_id, objective_id, text}
      * multipart -> subject_id, objective_id, and either a `text` field or a
        `file` field (PDF/DOCX/TXT/JPG/PNG, extracted server-side). `text` wins
        when both are present.
    Returns {doc_id, chunks_created, objective_id}.
    """
    content_type = request.headers.get("content-type", "")
    note_text = ""
    source_file = "pasted_notes"

    if content_type.startswith("application/json"):
        body = await request.json()
        subject_id = (body.get("subject_id") or "").strip()
        objective_id = (body.get("objective_id") or "").strip()
        note_text = (body.get("text") or "").strip()
    else:
        form = await request.form()
        subject_id = (form.get("subject_id") or "").strip()
        objective_id = (form.get("objective_id") or "").strip()
        note_text = (form.get("text") or "").strip()
        upload = form.get("file")
        if not note_text and upload is not None and hasattr(upload, "read"):
            data = await upload.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")
            try:
                note_text = _extract_upload_text(data, upload.filename or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            source_file = upload.filename or "uploaded_file"

    if not subject_id or not objective_id:
        raise HTTPException(status_code=422, detail="subject_id and objective_id are required.")

    note_text = note_text[:MAX_NOTE_CHARS].strip()
    if not note_text:
        raise HTTPException(status_code=400, detail="No note text was provided.")

    try:
        return save_notes(
            request.app.state.db, subject_id, objective_id, note_text,
            source_file=source_file, embed_fn=ollama_embed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
