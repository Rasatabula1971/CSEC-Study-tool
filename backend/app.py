"""
backend/app.py
==============
Stage 6 FastAPI entry point. Wraps the deterministic controller in JSON endpoints
and serves the single-page chat UI. The live system is exactly this app + Ollama
(CLAUDE.md).

Lifespan startup:
  1. Verify the SSD is mounted (sys.exit with a clear message if not).
  2. ollama_health() -- log a warning if down, but keep running (the UI still
     loads; chat calls will surface the Ollama error).
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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

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
    if ssd_root and not Path(ssd_root).exists():
        sys.exit(f"ERROR: SSD not mounted at {ssd_root}. Plug in the drive and restart.")

    if not ollama_health():
        logger.warning("Ollama is not reachable at %s -- chat calls will fail until "
                        "it is running. Starting the app anyway.", os.getenv("OLLAMA_BASE"))

    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")
    app.state.db = open_db(db_path)
    try:
        yield
    finally:
        app.state.db.close()


app = FastAPI(title="CSEC AI Study Partner", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    """A chat turn. `message` is the student's text; route selects the workflow.

    Optional fields (question_id, objective_id, paper, year, question_num,
    content_type) are passed through to the controller when present -- e.g. a
    grade turn supplies question_id.
    """
    message: str
    subject_id: str
    route: str
    question_id: str | None = None
    objective_id: str | None = None
    paper: str | None = None
    year: int | None = None
    question_num: str | None = None
    content_type: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    # The chat UI (static/chat.html) is deferred to a later step. Until it
    # exists, serve a small placeholder so the app still runs and the API
    # endpoints below are usable directly.
    if CHAT_HTML.exists():
        return FileResponse(CHAT_HTML)
    return JSONResponse({
        "status": "ok",
        "message": "API is running. The chat UI is not built yet.",
        "endpoints": ["/health", "/api/subjects", "/api/due/{subject_id}", "/api/chat"],
    })


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


@app.post("/api/chat")
def chat(body: ChatRequest, request: Request) -> dict:
    """Map a chat turn onto the controller's request shape and return its result."""
    req = body.model_dump(exclude_none=True)
    # The controller reads `query` (teach) and `student_answer` (grade); both come
    # from the single message box in the UI.
    req["query"] = body.message
    req["student_answer"] = body.message
    return handle_request(request.app.state.db, req)
