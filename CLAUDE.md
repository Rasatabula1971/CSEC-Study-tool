# CSEC AI Study Partner — CLAUDE.md

This file is the authoritative project context for Claude Code.
Read it **fully** before touching any file. Update the Stage Tracker when a stage completes.

---

## Purpose

A fully offline CSEC exam preparation system for seven subjects.
It teaches, quizzes, and grades against the real CXC syllabus, logs weaknesses,
and produces spaced-repetition revision plans.

**Live system = exactly two processes: Ollama + FastAPI.**
Everything else lives inside one SQLite file or deterministic Python.

---

## Design Rules — Non-Negotiable

**Rule 1:** Every lesson, quiz, mark, and revision task must resolve to a real
`objectives.objective_id`. No `objective_id` → no response. No exceptions.

**Rule 2:** If an output must be correct, a deterministic function produces it — not the LLM.

| Concern | Owner |
|---|---|
| Scope check | SQLite `WHERE` + `syllabus_locked` flag |
| Grading arithmetic | Python `sum()` |
| Next review date | Leitner scheduler |
| Weakness record | Pydantic model → validated SQLite write |
| Revision plan | Deterministic query of `weakness_log` ordered by box + due date |

---

## Seven Subjects

```
Principles_of_Business
Economics
Mathematics
English
Principles_of_Accounts
Integrated_Science
Information_Technology
```

**Pilot subject:** `Principles_of_Business`.
Every other subject is blocked behind a `syllabus_locked` gate until manually signed off.

---

## Repo vs SSD Layout

Code lives in the Git repo on the laptop C: drive.
Data (models, database, documents, backups) lives on the external SSD.
**Never commit the SSD path or its contents to Git.**

```
C:\csec-study-partner\              ← This repo
├── CLAUDE.md
├── requirements.txt
├── .env.example
├── .env                            ← gitignored
├── backend/
│   ├── app.py                      ← FastAPI entry point
│   ├── controller.py               ← Workflow router
│   ├── ollama_client.py            ← httpx wrapper (never Ollama SDK)
│   ├── ingest.py                   ← PDF chunk → embed → FK-validate → index
│   ├── scope.py                    ← Deterministic scope check
│   ├── retrieval.py                ← Structured-first, semantic-fallback
│   ├── grade.py                    ← Point-matching grader
│   ├── schedule.py                 ← Leitner scheduler
│   ├── weakness.py                 ← Validated weakness log writer
│   ├── ram_check.py                ← RAM budget verification script
│   ├── static/
│   │   └── chat.html               ← Vanilla JS chat page (no npm/React)
│   └── db/
│       ├── schema.sql              ← All CREATE TABLE / CREATE VIRTUAL TABLE
│       ├── init_db.py              ← Runs schema.sql against the SSD DB
│       ├── syllabus_parser.py      ← CSV → DB loader
│       ├── export_for_review.py    ← Exports objectives to Excel for sign-off
│       └── lock_subject.py         ← Sets syllabus_locked = 1 after approval
├── prompts/
│   ├── archivist.txt
│   ├── tutor.txt
│   ├── examiner.txt
│   └── planner.txt
├── launch/
│   ├── start.bat                   ← SSD check → Ollama → FastAPI → health checks
│   └── backup.bat                  ← Copies csec.sqlite to 07_BACKUPS with date stamp
└── tests/
    ├── test_schema.py
    ├── test_syllabus.py
    ├── test_ollama_client.py
    ├── test_ingest.py
    ├── test_core.py
    ├── test_api.py
    └── test_pilot_pob.py

D:\CSEC_AI_STUDY_PARTNER\           ← External SSD (data only, never in repo)
├── 01_MODELS\Ollama\
├── 02_DATABASE\csec.sqlite
├── 03_KNOWLEDGE_BASE\
│   └── {Subject}\
│       ├── 00_SYLLABUS\
│       ├── 01_SPECIMEN_PAPERS\
│       ├── 02_PAST_PAPERS\
│       ├── 03_MARK_SCHEMES\
│       ├── 04_NOTES\
│       └── 05_STUDENT_WORK\
├── 04_REPORTS\
└── 07_BACKUPS\
```

---

## Environment (.env)

```
SSD_ROOT=D:\CSEC_AI_STUDY_PARTNER
DB_PATH=D:\CSEC_AI_STUDY_PARTNER\02_DATABASE\csec.sqlite
KB_ROOT=D:\CSEC_AI_STUDY_PARTNER\03_KNOWLEDGE_BASE
REPORTS_ROOT=D:\CSEC_AI_STUDY_PARTNER\04_REPORTS
OLLAMA_BASE=http://localhost:11434
MODEL_CHAT=llama3.2:3b
MODEL_EMBED=nomic-embed-text
EMBED_DIM=768
```

`MODEL_CHAT` is used for **all roles** (Archivist, Tutor, Examiner).
Roles differ by system prompt only — never by loading a different model.

---

## Database Schema (schema.sql)

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS subjects (
    subject_id      TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    syllabus_locked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS syllabus_sections (
    section_id  TEXT PRIMARY KEY,
    subject_id  TEXT NOT NULL REFERENCES subjects(subject_id),
    title       TEXT NOT NULL,
    section_num TEXT
);

CREATE TABLE IF NOT EXISTS objectives (
    objective_id  TEXT PRIMARY KEY,        -- e.g. "POB-1.2"
    section_id    TEXT NOT NULL REFERENCES syllabus_sections(section_id),
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_num TEXT NOT NULL,
    content_stmt  TEXT NOT NULL,
    skill_type    TEXT,                    -- Knowledge | Understanding | Application
    command_words TEXT,                    -- JSON array e.g. '["Explain","Define"]'
    exam_weight   TEXT,                    -- P1 | P2 | Both
    verified      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    content_type  TEXT NOT NULL,           -- syllabus|specimen|past_paper|mark_scheme|notes
    paper         TEXT,
    year          INTEGER,
    source_file   TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
    objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    chunk_text    TEXT NOT NULL,
    page          INTEGER,
    question_num  TEXT,
    chunk_id      TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS mark_points (
    mark_point_id TEXT PRIMARY KEY,        -- e.g. "POB-1.2-q2b-mp1"
    objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
    question_id   TEXT,
    doc_id        TEXT REFERENCES documents(doc_id),
    point_text    TEXT NOT NULL,
    marks_value   INTEGER NOT NULL DEFAULT 1,
    point_order   INTEGER
);

CREATE TABLE IF NOT EXISTS study_sessions (
    session_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id   TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
    mode         TEXT NOT NULL,            -- teach|quiz|grade
    outcome      TEXT,                     -- pass|fail|partial
    score_pct    INTEGER,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS weakness_log (
    weakness_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
    subject_id   TEXT NOT NULL REFERENCES subjects(subject_id),
    score_pct    INTEGER NOT NULL,
    reason       TEXT,
    leitner_box  INTEGER NOT NULL DEFAULT 1,
    next_review  TEXT NOT NULL,
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS revision_schedule (
    task_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
    due_date     TEXT NOT NULL,
    task_type    TEXT NOT NULL,            -- review|quiz|practice
    completed    INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ingest_review_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    chunk_text  TEXT NOT NULL,
    reason      TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- sqlite-vec virtual tables  (EMBED_DIM = 768 for nomic-embed-text)
-- rowid matches chunks.id in every vec table
CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes
    USING vec0(embedding float[768]);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_past_papers
    USING vec0(embedding float[768]);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_mark_schemes
    USING vec0(embedding float[768]);
```

---

## sqlite-vec API (Python)

```python
import sqlite3, sqlite_vec, struct, os
from dotenv import load_dotenv

load_dotenv()

def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(os.getenv("DB_PATH"))
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db

def serialize_vec(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)

def index_chunk(db: sqlite3.Connection, chunk_rowid: int,
                embedding: list[float], table: str) -> None:
    db.execute(
        f"INSERT OR REPLACE INTO {table}(rowid, embedding) VALUES (?, ?)",
        (chunk_rowid, serialize_vec(embedding))
    )

def semantic_search(db: sqlite3.Connection, query_vec: list[float],
                    table: str, subject_id: str, k: int = 5) -> list:
    return db.execute(f"""
        SELECT v.rowid, v.distance,
               c.chunk_text, c.objective_id, c.chunk_id, c.page
        FROM   {table} v
        JOIN   chunks c ON c.id = v.rowid
        WHERE  v.embedding MATCH ?
          AND  v.rowid IN (SELECT id FROM chunks WHERE subject_id = ?)
        ORDER  BY v.distance
        LIMIT  ?
    """, (serialize_vec(query_vec), subject_id, k)).fetchall()
```

---

## Ollama API (always httpx — never the Ollama Python SDK)

```python
import httpx, os

OLLAMA     = os.getenv("OLLAMA_BASE", "http://localhost:11434")
MODEL_CHAT = os.getenv("MODEL_CHAT",  "llama3.2:3b")
MODEL_EMBED = os.getenv("MODEL_EMBED", "nomic-embed-text")

def ollama_embed(text: str) -> list[float]:
    """keep_alive=0 evicts the embedding model immediately after the call."""
    r = httpx.post(f"{OLLAMA}/api/embeddings", json={
        "model":      MODEL_EMBED,
        "prompt":     text,
        "keep_alive": 0
    }, timeout=30)
    r.raise_for_status()
    return r.json()["embedding"]

def ollama_chat(messages: list[dict], system: str,
                schema: dict | None = None) -> str:
    """schema = JSON Schema dict forces the model to output conforming JSON."""
    payload = {
        "model":    MODEL_CHAT,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream":   False,
    }
    if schema:
        payload["format"] = schema
    r = httpx.post(f"{OLLAMA}/api/chat", json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["message"]["content"]

def ollama_health() -> bool:
    try:
        return httpx.get(f"{OLLAMA}/api/tags", timeout=3).status_code == 200
    except Exception:
        return False
```

---

## Grading Contract

The Examiner produces **one** schema-constrained JSON object.
Python computes every number. Never ask the model to add, calculate a percentage, or choose a date.

```python
GRADING_SCHEMA = {
    "type": "object",
    "required": ["objective_id", "question_id", "points"],
    "properties": {
        "objective_id": {"type": "string"},
        "question_id":  {"type": "string"},
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["mark_point_id", "awarded", "evidence"],
                "properties": {
                    "mark_point_id": {"type": "string"},
                    "awarded":       {"type": "boolean"},
                    "evidence":      {"type": "string"}
                }
            }
        }
    }
}

def compute_score(grading: dict) -> dict:
    pts     = grading["points"]
    awarded = sum(1 for p in pts if p["awarded"])
    total   = len(pts)
    pct     = round(100 * awarded / total) if total else 0
    missed  = [p["mark_point_id"] for p in pts if not p["awarded"]]
    return {
        "score_pct":     pct,
        "awarded":       awarded,
        "total":         total,
        "missed_points": missed
    }
```

---

## Leitner Scheduler

Five boxes. Intervals: `{1: 1 day, 2: 2 days, 3: 4 days, 4: 7 days, 5: 15 days}`.
Pass (score ≥ 70 %) → move up one box (max 5).
Fail (score < 70 %) → reset to box 1.
New objectives → box 1, `next_review` = today.

```python
from datetime import date, timedelta

LEITNER_INTERVALS = {1: 1, 2: 2, 3: 4, 4: 7, 5: 15}
PASS_THRESHOLD    = 70

def update_leitner(current_box: int, score_pct: int) -> tuple[int, str]:
    passed  = score_pct >= PASS_THRESHOLD
    new_box = min(current_box + 1, 5) if passed else 1
    days    = LEITNER_INTERVALS[new_box]
    return new_box, (date.today() + timedelta(days=days)).isoformat()
```

---

## Scope Check (gate before every request)

```python
def is_in_scope(db: sqlite3.Connection,
                subject_id: str,
                objective_id: str) -> bool:
    row = db.execute("""
        SELECT 1 FROM objectives o
        JOIN   subjects s ON s.subject_id = o.subject_id
        WHERE  o.objective_id = ?
          AND  o.subject_id   = ?
          AND  s.syllabus_locked = 1
    """, (objective_id, subject_id)).fetchone()
    return row is not None
```

`is_in_scope` returning `False` → controller returns a polite redirect.
**No LLM call. No embedding call.** Just a deterministic "not in scope" response.

---

## Deterministic vs LLM — Never Cross This Line

| Concern | Owner | Notes |
|---|---|---|
| Is subject locked / in scope? | Deterministic (SQLite) | Boolean result |
| Exact lookup by paper/year/question | Deterministic (SQLite) | Covers most grading requests |
| Explain a syllabus objective | LLM — Tutor prompt | Pure language |
| One targeted follow-up question | LLM — Tutor prompt | Low-risk generation |
| Did the answer match mark point X? | LLM — boolean, schema-constrained | One per point |
| Score arithmetic | Python `sum()` | Never ask the model |
| Write weakness record | Pydantic → SQLite | Never parse from free text |
| Next review date | Leitner Python function | Never ask the model |
| Build revision plan | Deterministic query | Order by box and due_date |
| Source / traceability | SQLite FK join | chunk → objective |

---

## Retrieval Order (retrieval.py)

```
1. Structured lookup first.
   If request contains (subject, paper, year, question_num) or objective_id
   → SQLite WHERE on chunks. If found, skip all embedding calls.

2. Semantic fallback.
   Only when the structured key is unknown (free-text question).
   → embed the query with keep_alive=0
   → search the correct vec_* table filtered by subject_id
   → join back to chunks to get objective_id

3. Always return objective_id + source_file + page with every result.
   This is what makes VAL-08 traceability real.
```

---

## SSD Safety Rules

Always check at startup:

```python
import os, sys

SSD_ROOT = os.getenv("SSD_ROOT", r"D:\CSEC_AI_STUDY_PARTNER")
if not os.path.exists(SSD_ROOT):
    sys.exit(f"ERROR: SSD not mounted at {SSD_ROOT}. Plug in the drive and restart.")
```

- **Never hardcode `D:\`** — always read from `os.getenv("SSD_ROOT")`.
- The drive letter can differ on different machines.

---

## Key Commands

```bash
# Initialise the database (Stage 1)
python backend/db/init_db.py

# Run all tests
pytest tests/ -v

# Start the system (dev mode with reload)
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload

# Ingest a subject folder
python backend/ingest.py --subject Principles_of_Business

# Export weak topics to Excel
python backend/export_excel.py --subject Principles_of_Business

# Lock a subject after manual syllabus sign-off
python backend/db/lock_subject.py --subject Principles_of_Business

# Verify RAM budget
python backend/ram_check.py
```

---

## Python Dependencies (requirements.txt)

```
fastapi>=0.111
uvicorn[standard]>=0.29
httpx>=0.27
sqlite-vec>=0.1.6
pydantic>=2.7
python-dotenv>=1.0
python-multipart>=0.0.9 # FastAPI form handling (Stage 6)
pymupdf>=1.24          # PDF chunking (import as fitz)
openpyxl>=3.1          # Excel export
psutil>=5.9            # RAM measurement
pytest>=8.2
```

**No Qdrant. No CrewAI. No LangChain. No OpenAI SDK. No Ollama Python SDK.**
If a dependency is not in this list, ask before adding it.

---

## Build Stage Tracker

Update this section when a stage is complete.
The current stage is the first unchecked box.

- [x] **Stage 1** — Storage & Schema: folder structure, schema.sql, init_db.py, backup.bat ✓ 2026-06-12 (SSD root D:\CSEC_AI_STUDY_PARTNER; init_db OK, backup OK, 8/8 tests pass)
- [x] **Stage 2** — Syllabus Lock: syllabus_parser.py, export_for_review.py, lock_subject.py ✓ 2026-06-12 (POB SYLL 17 extracted → 10 sections/116 objectives loaded into E: DB, all verified=1, review xlsx exported, syllabus_locked=1; 32/32 tests pass; other 6 subjects still gated)
- [x] **Stage 3** — Minimal Engine: ollama_client.py, ram_check.py, model pull verification ✓ 2026-06-12 (httpx Ollama client + verify_models; ram_check.py now advisory-only — tiered WARN, never FAILs/blocks; real RAM test is a session that runs without freezing)
- [x] **Stage 4** — Ingestion: ingest.py (PDF chunk → embed → FK-validate → sqlite-vec index) ✓ 2026-06-13 (chunk→keyword-match→FK-validate→route to vec_notes/vec_past_papers/vec_mark_schemes; unmatched→ingest_review_queue, never indexed unmapped; mark-point parser; --review-queue interactive assign; embed_fn injectable; 8 ingest tests + 51/51 suite pass. NOTE: live end-to-end run on real PDFs still pending Ollama install + nomic-embed-text pull)
- [x] **Stage 5** — Deterministic Core: scope.py, retrieval.py, grade.py, schedule.py, weakness.py, controller.py + four prompt files + full test suite ✓ 2026-06-13 (scope gate + subject_is_locked; structured-first/semantic-fallback retrieval w/ injectable embed_fn; GRADING_SCHEMA+compute_score grader w/ injectable chat_fn; Leitner + get_due_objectives; Pydantic weakness upsert (raises ValueError, never silent); controller teach/grade/plan — subject-lock gate BEFORE any embedding, plan fully deterministic/no-LLM; archivist/tutor/examiner/planner prompts; test_core.py 16 tests, suite 67/67. NOTE: manual controller smoke test still pending Ollama + ingested data; playbook's smoke snippet calls init_db.open_db() w/o its required db_path arg)
- [x] **Stage 6** — FastAPI + UI + Launcher: app.py, routes, chat.html, start.bat ✓ 2026-06-13 (app.py: lifespan SSD-check/ollama_health-warn/open_db→app.state.db; StaticFiles /static; GET / serves finalized backend/static/chat.html (provided, unmodified); POST /api/chat maps message→query/student_answer, 422 on empty/missing message or subject_id; GET /api/subjects locked-only; GET /api/due/{id}; GET /health. app.py adds presentation-only UI shim: plan tasks→objectives alias, grade weakness.leitner_box/next_review lifted to top level. start.bat per spec w/ Ollama hard-gate + advisory ram_check note. python-multipart added. test_api.py 8 tests, suite 75/75. NOTE: live browser run still pending Ollama + ingested data; server not started per build instruction)
- [ ] **Stage 7** — Pilot: end-to-end POB integration tests + manual validation session
- [ ] **Stage 8** — Rollout: remaining six subjects through the lock gate
- [ ] **Stage 9** — Optional: Open WebUI front-end (v3.1); CrewAI orchestration (v3.2) — never Phase 1
