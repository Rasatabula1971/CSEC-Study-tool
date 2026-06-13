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
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# backend/ on sys.path so the bare module imports below resolve whether the app
# is launched as `backend.app:app` or imported directly in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_health  # noqa: E402
from controller import handle_request  # noqa: E402
from schedule import get_due_objectives  # noqa: E402

logger = logging.getLogger("csec.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
CHAT_HTML = STATIC_DIR / "chat.html"
QUIZ_HTML = STATIC_DIR / "quiz.html"


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    ssd_root = os.getenv("SSD_ROOT")
    if ssd_root and not os.path.exists(ssd_root):
        sys.exit(f"ERROR: SSD not mounted at {ssd_root}. Plug in the drive and restart.")

    if not ollama_health():
        logger.warning("Ollama is not reachable at %s -- study mode will surface the "
                        "error. Starting the app anyway.", os.getenv("OLLAMA_BASE"))

    db_path = os.getenv("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")
    app.state.db = open_db(db_path)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(CHAT_HTML)


@app.get("/health")
def health(request: Request) -> dict:
    db_ok = getattr(request.app.state, "db", None) is not None
    return {"status": "ok", "ollama": ollama_health(), "db": db_ok}


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
        LEFT   JOIN chunks c ON c.chunk_id = mp.question_id || '-stem'
        WHERE  d.subject_id = ?
        GROUP  BY mp.question_id
        ORDER  BY d.year DESC, d.paper, mp.question_id
        """,
        (subject_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = f"{d['year']} · {d['paper']} · Q{d['question_num'] or ''}".strip()
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

    Filters chunks by subject, content_type='past_paper' and a present
    question_num, joined to documents for paper/year. `paper` and `year` are
    optional query params that narrow the list further. Each row carries the
    question stem (first 400 chars) and a marks_total = number of mark points
    keyed on that question. Returns [] when nothing matches -- never 404.
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
        "  AND  d.content_type = 'past_paper'",
        "  AND  c.question_num IS NOT NULL",
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
    /api/questions query can return (past-paper chunks with a question_num)
    appear, so a selected paper/year always yields questions. `papers` is
    sorted alphabetically, `years` descending. Returns empty lists when the
    subject has no questions -- never 404.
    """
    db = request.app.state.db
    papers = db.execute(
        """
        SELECT DISTINCT d.paper AS paper
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type = 'past_paper'
          AND  c.question_num IS NOT NULL
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
          AND  d.content_type = 'past_paper'
          AND  c.question_num IS NOT NULL
          AND  d.year IS NOT NULL
        ORDER  BY d.year DESC
        """,
        (subject_id,),
    ).fetchall()
    return {
        "papers": [r["paper"] for r in papers],
        "years": [r["year"] for r in years],
    }


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
