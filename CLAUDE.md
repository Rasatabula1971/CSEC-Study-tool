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

## Offline-First: What It Means and What It Does Not Mean

PDR v3.1 distinguishes two phases of the system.

**Build-time** — the builder runs ingestion scripts (`ingest.py`,
`ingest_lessons.py`, `recover_mark_points.py`, `derive_syllabus_mark_points.py`)
to populate the database. This is not student-facing. Cloud APIs (Gemini)
may be used here when `CLOUD_MODE=1` to fill gaps where the local model
cannot produce adequate content. All cloud-generated content is stored
in SQLite, flagged with `source_model='gemini'`, and queued in
`ingest_review_queue` for review before going live.

**Runtime** — the student's live session. Lessons, grading, revision plans,
feedback. Ollama only. SQLite only. `CLOUD_MODE` has no effect on runtime
paths regardless of value. The acceptance test (VAL-01) is: with Wi-Fi
off, every student-facing endpoint returns a valid response.

**Module phase markers** — every `backend/*.py` file carries a phase tag at
the top:

```
# PHASE: runtime — called during a student session
# PHASE: build   — called only during ingestion scripts
# PHASE: dual    — used by both, with internal phase gating
```

`llm_router.py` is the only `PHASE: dual` module. Its `CLOUD_MODE` check
ensures Gemini is never reached at runtime, only at build time.
`tests/test_pdr_v3_1_compliance.py` enforces this.

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

**Stage 11 note:** `prompts/tutor.txt` is now **follow-up Q&A only** — answering a
student's question about a lesson they have already been shown. Initial lesson
generation no longer happens at runtime: canonical lessons are pre-generated
offline by `backend/ingest_lessons.py` (its composition prompt lives inline in the
script) and served deterministically from `objective_lessons`. The runtime teach
route only falls back to live tutor generation when no canonical lesson exists, and
queues that objective for the next `ingest_lessons.py` pass.

---

## Optional Cloud Mode

When `CLOUD_MODE=1` and `GEMINI_API_KEY` is set, the grading path uses
Gemini for point-matching judgements. When `CLOUD_MODE=0` (the default),
all inference is Ollama-only. The system is fully functional in either
mode. `CLOUD_MODE` must never silently fall back — if it is `1` and Gemini
is unreachable, the request fails with a clear error, not a silent
retry to Ollama.

`CLOUD_MODE` affects build-time ingestion scripts only. It has no effect
on runtime paths. See PDR v3.1 Section 2.5.

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

CREATE TABLE IF NOT EXISTS practice_questions (
    question_id   TEXT PRIMARY KEY,
    objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    stem          TEXT NOT NULL,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS study_plan (
    plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
    status        TEXT NOT NULL DEFAULT 'unmet',
        -- unmet | in_progress | met_once | mastered
    met_count     INTEGER NOT NULL DEFAULT 0,
    last_met_at   TEXT,
    created_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(subject_id, objective_id)
);

CREATE TABLE IF NOT EXISTS study_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_ids   TEXT NOT NULL,   -- JSON array of objective_ids
    synthesis_qid   TEXT,            -- question_id of the synthesis question
    status          TEXT NOT NULL DEFAULT 'active',
        -- active | completed | abandoned
    created_at      TEXT DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS ingest_review_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file  TEXT NOT NULL,
    chunk_text   TEXT NOT NULL,
    reason       TEXT,
    objective_id TEXT,                          -- known at queue time (prose rows)
    doc_id       TEXT REFERENCES documents(doc_id),
    created_at   TEXT DEFAULT (datetime('now'))
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

# Ingest a subject folder (notes / past papers / mark schemes -> vec index)
python backend/ingest.py --subject Principles_of_Business

# Ingest Paper 2 worked-solution PDFs -> mark_points (the gradeable "answer bank").
# Deterministic parse, offline by default; pass --embed to also index stems.
python backend/ingest_solutions.py --subject Principles_of_Business --src "<folder of *.pdf>"

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
- [x] **Stage 6** — FastAPI + UI + Launcher: app.py, routes, lightweight chat page, start.bat
- [x] **Stage 7** — Pilot: end-to-end Principles_of_Business test suite, manual validation ✓ 2026-06-13 (quiz Submit-Answer perceived-latency polish — disable+"Grading…", faint status line cycling the grading steps every 3s, 30s reassurance, plain text no spinners; perf: ollama_chat keep_alive=30m holds the 3B chat model resident + lifespan pre-warm after Ollama health check so first Submit skips cold-load (embed still keep_alive=0); syllabus-fallback grader grade_against_syllabus() judges against objective content_stmt when no mark_points exist (same GRADING_SCHEMA, Python scores); objective practice mode — practice_questions table via idempotent runtime migration, route="practice", /api/sections endpoint; quiz /api/questions+/api/filters restricted to solution-derived '-stem' chunks; 115/115 tests pass. Full-loop integration class TestPOBStudyLoop added (teach / grading 2-of-3→67% Leitner box-1 / scope gate / revision-plan box-order / VAL-08 traceability / weakness validation), real :memory: DB w/ apply_runtime_migrations, Ollama patched via unittest.mock — suite now 211/211. NOTE: live LLM grade still pending Ollama + nomic-embed-text)
**Build Playbook v3.1 (Stages 8–13) — harden POB before multi-subject rollout. Original Stage 8 → Stage 14, original Stage 9 → Stage 15.**

- [x] **Stage 8** — Mark Point Recovery: second-pass LLM-assisted extractor to close the 13-objective POB mark-point gap ✓ 2026-06-16 (mark_points widened with source_type/source_chunk_id/extraction_confidence via app.apply_runtime_migrations, each ALTER try/except sqlite3.OperationalError — applied to live E: DB, 2447 existing rows defaulted source_type='past_paper'; backend/recover_mark_points.py — offline ollama_chat only (never Gemini/cloud), per zero-mark-point objective gathers mark_scheme chunks (direct tag + vec_mark_schemes k=5 semantic on content_stmt), inline 4-field JSON schema (point_text/marks_value/confidence/evidence_quote), conf≥min→mark_points source_type='recovered_extraction', conf<min→ingest_review_queue reason='low_confidence_extraction', idempotent on (source_chunk_id,point_text) + deterministic mark_point_id, --subject/--dry-run/--min-confidence, summary table; backend/review_queue.py — Y promote→mark_points / N delete / Q quit, format-only judgement (no subject expertise); tests/test_recover_mark_points.py 5 tests (high-conf write, low-conf queue, re-run no-dup, dedup guard on shared chunk, dry-run no-write); suite 229/229. NOTE: live extraction run still pending Ollama — schema migration done, 13 POB objectives still empty until recover_mark_points.py runs against Ollama (manual --dry-run-first verify step))
- [x] **Stage 9** — Syllabus-Derived Mark Points: derive fallback mark points from content_stmt + notes for objectives with no past-paper coverage ✓ 2026-06-16 (apply_runtime_migrations widened with `ALTER TABLE mark_points ADD COLUMN command_word TEXT` (try/except OperationalError) + `CREATE TABLE IF NOT EXISTS lesson_generation_queue` — applied to live E: DB; backend/derive_syllabus_mark_points.py — offline ollama_chat only, for each locked-subject objective with ZERO mark_points of any source_type: top-5 vec_notes on content_stmt, falls back to vec_past_papers when <2 notes chunks; constrained 3–5-point schema (point_text/marks_value/confidence/evidence_quote); writes source_type='syllabus_derived' + source_chunk_id(primary chunk)+extraction_confidence+command_word, and ALWAYS queues every point to ingest_review_queue reason='syllabus_derived_first_run' (confidence never skips the queue); idempotent on (objective_id, point_text); --subject/--dry-run + per-objective summary table; review_queue.py extended — _split_candidate handles both evidence markers, promote_row stamps source_type by reason (syllabus_derived_first_run→syllabus_derived, else recovered_extraction); tests/test_derive_syllabus_mark_points.py (8) + tests/test_review_queue.py (6); suite 243/243. NOTE: live --dry-run verified against Ollama — 13 POB objectives, 49 points would be written, but model confidence on derived points is very LOW (1–10/100) given the thin notes corpus; live apply (run without --dry-run) + human review pass deferred to a manual decision. grade.py source_type priority + grading_basis is NOT yet done — belongs with Stage 10 confidence-aware grading.)
- [x] **Stage 10** — Confidence-Aware Grading: weighted marks_value scoring, command-word gating, evidence post-check, verify-with-teacher UI badge ✓ 2026-06-16 (command_word ALTER already present from Stage 9 — added backfill in apply_runtime_migrations: each mark_point inherits its objective's single command word via json_extract '$[0]' when json_array_length==1, else NULL; applied to live E: DB, 2447/2447 rows backfilled; grade.py compute_score(grading, mark_points_db) now WEIGHTS by DB marks_value (marks_value [1,2,1] missing the 2 → 50% not 67%), weights read from DB never model output; GRADING_SCHEMA + per-point & overall confidence (both required); prompts/examiner.txt rewritten with ROLE/COMMAND WORD RULES/CONFIDENCE/OUTPUT FORMAT sections (Explain needs reasoning, Define needs a statement, State/List accept brief, etc.); evidence_post_check in grade_answer — <20-char evidence auto-downgraded to missed (note appended), verbatim-echo-without-connector flagged in review_flags but award stands; grade_answer now returns grading_basis + overall_confidence (min per-point, else top-level, else 50) + review_flags + pending_review; grade_against_syllabus/grade_synthesis pass synthetic weight-1 lists to preserve /N scoring; chat.html appendGradeCard — "X / Y marks (Z%)" slash line, per-point ✓/✗ breakdown with grey-italic downgrade notes, plain-language grading_basis label, amber verify-with-teacher badge (shows when confidence<70 OR basis≠past_paper OR review_flags OR pending_review), 👍👎🤔 feedback buttons POST /api/feedback feedback_type='grading' (best-effort; endpoint lands in Stage 12); tests A–F added; suite 252/252. NOTE: live LLM confidence output untested until Ollama run; /api/feedback endpoint is Stage 12.)
  - Note: chat.html feedback buttons (👍👎🤔) degraded silently until Stage 12 added POST /api/feedback (RESOLVED — Stage 12 endpoint live; buttons now persist end-to-end).
- [x] **Stage 11** — Canonical Lessons: pre-generate one lesson per objective at ingestion (objective_lessons), serve deterministically; tutor.txt becomes follow-up Q&A only ✓ 2026-06-16 (objective_lessons table added to app.apply_runtime_migrations in its own try/except sqlite3.OperationalError block — UNIQUE(objective_id) enforces one canonical lesson per objective; lesson_generation_queue reused from Stage 9, not recreated; backend/ingest_lessons.py — offline ollama_chat composer (PHASE: build), per locked-subject objective pulls top-5 vec_notes on content_stmt and, when <2 notes, also top-3 vec_past_papers + top-3 vec_mark_schemes; exact 6-field JSON schema (lesson_text/key_terms/worked_examples/common_mistakes/recall_questions[exactly 3]/confidence); source-anchored system prompt (rewrite-not-author, 200–350 words, define jargon once); local confidence floor (3+ notes→90, 2→70, 1→50, else 30; −20 when only mark schemes; never <30) capped against model self-report via min(); 'no source, no lesson' — zero-chunk objectives queue lesson_generation_queue reason='insufficient_sources' with NO model call; final<floor (default 30)→queue not write; lesson_id=sha256(objective_id|generated_at)[:16]; JSON-encoded array columns; source_chunk_ids=chunks.chunk_id list; --subject/--regenerate(DELETE-then-write)/--confidence-floor/--dry-run; per-objective + totals summary; controller teach route — _fetch_canonical_lesson serves stored lesson deterministically (no ollama_chat) returning lesson_text + recall_questions[list] + key_terms + worked_examples + common_mistakes + lesson_source='canonical' + confidence, checked after the scope gate for both explicit-objective and free-text paths; runtime fallback generates live, adds lesson_source='runtime', and _queue_lesson_generation INSERT-OR-IGNOREs (objective_id,'served_runtime'); both DB touches wrapped in try/except sqlite3.OperationalError so pre-Stage-11 test DBs degrade to runtime; study_plan.html — recall_questions rendered as tappable .recall-pill buttons (first auto-selected as the gradeable card), regex extractQuestionFromLesson kept as a SAFETY NET only, fired with console.warn when lesson_source='runtime' and no recall_questions; prompts/tutor.txt rewritten for follow-up Q&A on a stored lesson (NOT initial generation — that lives inline in ingest_lessons.py); tests/test_lessons.py 6 tests (write/queue-insufficient/regenerate-replaces/regenerate-default-skips/canonical-no-LLM/runtime-queues); test_teach_context updated to stub the two new helpers; suite 258/258. NOTE: live ingest run against Ollama performed for POB — see run log.)
  - Follow-up fix (2026-06-17): lesson_generation_queue was accumulating stale + duplicate rows (every failed/low-confidence run re-INSERTed). Added UNIQUE INDEX idx_lgq_objective_reason on (objective_id, reason) via app.apply_runtime_migrations + ensure_lesson_tables (try/except OperationalError — created only after dedup); _queue_insufficient now upserts ON CONFLICT(objective_id, reason) DO UPDATE created_at instead of stacking rows; a successful lesson write DELETEs that objective's queue rows in the SAME transaction as the insert; summary gained a 'cleared' column. One-off live cleanup: 187→96 rows (7 stale dropped, 84 duplicates collapsed) — queue now == 96 distinct objectives needing work, 0 stale, 0 dup; objective_lessons=20, 20+96=116. tests/test_lessons.py +2 (write-clears-queue, idempotent-requeue); suite 260/260.
- [x] **Stage 12** — Feedback Loop: per-message 👍/👎/🤔 user_feedback log + targeted teacher-review report ✓ 2026-06-17 (user_feedback table added to app.apply_runtime_migrations in its own try/except sqlite3.OperationalError block — feedback_type CHECK IN ('lesson','grading','recall_question'), sentiment CHECK IN ('positive','negative','confused'), FK objective_id→objectives + subject_id→subjects (Rule 1: every flag resolves to a real objective); idx_feedback_objective + idx_feedback_sentiment(sentiment,subject_id) back the report query; POST /api/feedback — FeedbackRequest Pydantic model (Literal enums → 422 on bad value before body runs; objective_id/subject_id/feedback_type/sentiment required, notes/context_json/session_id optional), INSERTs one row returning {ok:true, feedback_id}, FK violation→400 {ok:false,error:'unknown objective_id or subject_id'} via sqlite3.IntegrityError (caught BEFORE the generic sqlite3.Error→500 branch), status set via injected Response so the body stays {ok,...}; backend/feedback_report.py (PHASE: build) — top-20 objectives by (negative+confused) DESC then total_feedback DESC, HAVING (neg+confused)>0, CASE WHEN not FILTER for SQLite-version compat; openpyxl workbook sheet 'Top objectives for review', bold white header on #2E75B6 fill, freeze_panes A2, 8 cols A–H (Objective ID/Objective #/Content[:80]/Negative/Confused/Positive/Total/Last negative), autofit capped 60, written to REPORTS_ROOT, dir created if absent, zero-flag case prints 'No feedback recorded yet' + still writes header-only file; generate_report(db, subject, reports_root, today) returns (path, count) for testability; chat.html UNCHANGED — Stage 10 buttons already POST {objective_id, subject_id:state.subjectId, feedback_type:'grading', sentiment} matching FeedbackRequest, now succeed end-to-end; tests/test_feedback.py 6 tests (valid→200+row, bad sentiment→422, bad feedback_type→422, unknown objective→400, report grouping/ordering/format, empty→header-only) using a real in-memory DB with check_same_thread=False for the TestClient worker thread; suite 266/266. Live: server POST returned {ok:true,feedback_id:1}; report ran (0 flagged rows so far — the single live row is positive — header-only xlsx written to E:\...\04_REPORTS).)
- [x] **Stage 13** — Panel UX Shell: rebuild chat.html as Learn/Practice/Review/Progress/Library/Exam panels with deep-link URL state ✓ 2026-06-17 (roadmap pts 2/3/4 folded in; pt 1 shipped earlier in f952f54). source_rank INTEGER added to mark_points via app.apply_runtime_migrations (try/except OperationalError) + backfill CASE: past_paper+specimen→2, past_paper→3, recovered_extraction/syllabus_derived→4, null→NULL (content_type joined from documents); live E: DB 2447 POB rows → rank 3. grade.py: SOURCE_RANK_LABELS + source_rank_info(db,question_id,mark_points) returns (min_rank,label,blocked_oid); rank-5 = a generated point (rank≥4) whose objective still has an unreviewed ingest_review_queue row → grade_answer refuses BEFORE the LLM call with {error:'mark_points pending review',objective_id,source_rank:5}; real rank≤3 points whose objective has an incidental queue row still grade + surface pending_review banner; grade_answer returns source_rank + source_rank_label (None on pre-migration DB). Four GET endpoints in app.py: /api/syllabus/{subject} (section+objective tree with has_lesson/mark_point_count/best_source_rank, command_words JSON-decoded), /api/progress/{subject} (ALL objectives, leitner_box/latest_score_pct/last_studied/next_review/feedback_negative/feedback_confused, nulls where absent), /api/past-papers/{subject} (gradeable '-stem' docs grouped by doc_id incl doc_id+objectives_covered, year desc), /api/practice-question/{doc_id}/{question_num} (question_id+text+objective+marks_total+command_words, 404 if absent); added import json + _decode_command_words helper. GET / now serves the panel shell (was welcome.html → moved to /welcome; /chat also serves the shell). backend/static/chat.html rebuilt as a single self-contained vanilla-JS panel shell (old → chat_v1.html.bak): 6 panels (Learn/Practice/Review/Progress/Library/Exam), CSS grid (nav rail | chat | panel) → mobile bottom-nav + slide-in panel, all design tokens in :root with full dark-mode override (prefers-color-scheme) + prefers-reduced-motion, deep-link URL state (?subject=&panel=&objective= via replaceState/popstate; subject resolves by id or unique initials e.g. POB), four-tier confidence band on grading (85+/70+/50+/<50 verbatim copy), source-rank badge line, per-mark-point ✓/✗ + auto-downgrade notes + review-flags details + pending_review amber banner, 👍👎🤔 feedback after lessons & grading, Exam mode = timed (sessionStorage startedAt survives refresh), nav-locked to Practice+Exam mid-attempt, hidden chat, auto-submit on time-up, sequential /api/chat grade calls + aggregate result; empty/loading(skeleton)/error states throughout; all 30 required functions defined, no CDN/framework/module/TODO. NOTE on recall grading: free-text recall pills in Learn/Review are SELF-CHECK only (no fabricated marks) — /api/chat route='grade' needs a question_id with a real mark scheme, and the task forbade new backend routes, so automated marking lives in Practice + Exam where '-stem' mark schemes exist. tests/test_source_rank.py (3) + tests/test_panel_shell.py (7 structural: required-functions/tokens/dark-mode/reduced-motion/single-inline-script/no-TODO/panels+endpoints) + 5 new endpoint tests in test_api.py (real in-memory DB) + test_root repointed to panel shell & test_welcome added; suite 283/283. Manual verify: GET / serves shell; every panel endpoint returns correct live POB data (syllabus 10 sections, progress 116 objs, past-papers 31 gradeable, due 5). Stages 8–13 (POB hardening) complete; next is Stage 14 rollout.)
  - Panel UI reverted to v1 on 2026-06-17. The panel shell did not work for daily use, so GET / serves the original chat.html (v1 chat UI) again. Panel shell preserved at backend/static/chat_panel_shell.html.bak for a possible future revisit; tests/test_panel_shell.py repointed at that .bak path (structural guards still run). The four new API endpoints (/api/syllabus, /api/progress, /api/past-papers, /api/practice-question) remain live and useful regardless of UI. No backend logic changed.
**v3.1 roadmap renumbering (resolved fully in PDR v3.2): this Stage 14 is the roadmap's "backup hardening", NOT the original playbook's Stage 14. Subject rollout slips to Stage 16; the Optional front-end work to Stage 17. Stage 15 is reserved for a further pre-rollout hardening pass (TBD in v3.2).**

- [x] **Stage 14** — Backup Hardening + Version-Tracked Migrations: auto-backup before every destructive build script; schema_migrations ledger ✓ 2026-06-17 (backend/db/backup.py PHASE: build — backup_database(label) copies DB_PATH → {SSD_ROOT}/07_BACKUPS/csec_{YYYY-MM-DD_HHMMSS}_{label}.sqlite via shutil.copy2 (preserves mtime), raises RuntimeError on unmounted SSD / missing DB / failed copy, rolling prune keeps the 30 most-recent csec_*.sqlite by mtime; backup_first(label) decorator runs backup_database first and ABORTS the wrapped fn if the backup raises (functools.wraps). Decorated main() of every destructive build-time script: ingest.py='pre_ingest', ingest_lessons.py='pre_ingest_lessons', derive_syllabus_mark_points.py='pre_derive', recover_mark_points.py='pre_recover', ingest_worked_solutions.py='pre_ingest_solutions', db/syllabus_parser.py='pre_syllabus_parse', db/lock_subject.py='pre_lock_subject' (read-only scripts export_progress/feedback_report/ram_check/review_queue left undecorated). Import: backend/ scripts `from db.backup import backup_first`; db/ scripts add their own dir to sys.path then `from backup import backup_first`. app.py: schema_migrations(version PK, description, applied_at) ledger + _ensure_schema_migrations bootstrap (always-run, can't version-track its own creation) + _run_migration(db, version, description, sql) — skips if version recorded, else splits sql on ';' and runs each, records the row; an ALTER that hits 'duplicate column name' is recorded as '<desc> [pre-existing]' (so historical try/except ALTERs stop retrying), any other error rolls back + re-raises. apply_runtime_migrations refactored into two layers: (1) 11 version-tracked schema migrations m001_runtime_core_tables…m011_stage13_source_rank_column; (2) the three DATA backfills (command_word, source_rank, question_id→-stem) kept UNCONDITIONAL/idempotent on every call — tests insert rows then re-run migrations to backfill them, and later ingestion adds rows that still need normalising, so these must NOT be version-gated. Live E: DB backfilled: 11 rows in schema_migrations (m002-m006/m011 [pre-existing], the CREATEs applied). tests/test_backup.py (5: file-created, missing-SSD→RuntimeError, prune-keeps-30, decorator-backs-up-first, decorator-aborts-on-fail) + tests/test_schema_migrations.py (5: applies+records, no-op when applied, duplicate-column→[pre-existing], unexpected-error re-raises, apply_runtime_migrations twice = same row count); suite 293/293. Build-time mutations are now backup-guarded and migrations are version-idempotent — subject rollout (Stage 16) is safe.)
- [ ] **Stage 15** — (reserved — further pre-rollout hardening, TBD in PDR v3.2)
- [ ] **Stage 16** (was Stage 8) — Rollout: remaining six subjects through the lock gate
- [ ] **Stage 17** (was Stage 9) — Optional: Open WebUI front-end (v3.1); CrewAI orchestration (v3.2) — never Phase 1

---

## Upload Material feature (build-phase, 4 sessions)

A `PHASE: build` content-preparation flow: drop a PDF/Word file in the browser,
stage it on the SSD, and review its extracted text before anything is ingested.
Separate from the older `/api/notes/*` paste-and-ingest flow (that one chunks +
embeds immediately; this one stages for human review first).

- [x] **Session 1** — Staging + PDF/DOCX text extraction (preview only) ✓ 2026-06-17
  - `upload_staging` table via migration **m012** (`apply_runtime_migrations`,
    version-tracked Layer 1); `extract_status` state machine
    pending→extracting→ready|failed; `status` staged|ingested|rejected (only
    'staged' is written in session 1). `ensure_staging_dirs()` creates
    `{SSD_ROOT}/06_UPLOAD_STAGING/{subject_id}/` for each locked subject
    (best-effort; warns + skips if the SSD is unmounted).
  - `backend/uploads.py` (`PHASE: build`): `stage_file` (FK-/type-validated SSD
    write, filename sanitised against path traversal, returns staging_id),
    `extract_text` (PyMuPDF page-by-page with `[Page N]` / `[Page N - no text]`
    markers; python-docx paragraphs + `[Table]…[/Table]` with ` | ` cells; 500k
    char cap), `get_staging_list` (no full text), `get_staging_detail` (full text).
  - Endpoints in `app.py`: `POST /api/upload` (multipart; locked-subject + .pdf/
    .docx + 50 MB gates; `BackgroundTasks` runs extraction async; errors use the
    `{"ok": false, "error": …}` + injected-`Response`-status pattern, not
    HTTPException), `GET /api/staging/{subject}`, `GET /api/staging/{subject}/{id}`,
    `DELETE /api/staging/{subject}/{id}` (removes file + row), `GET /upload`.
  - `backend/static/upload.html` (vanilla JS, links `shared.css`): drag-and-drop,
    sequential per-file upload, status-badge list polling every 3s while any file
    is pending/extracting, monospace preview with 10k-char "Show all", subject
    picker from `/api/subjects`. Unsupported drops show "Skipped: PNG not supported
    until session 2." per file.
  - Tests: `tests/test_uploads.py` (8) + `tests/test_upload_api.py` (8); suite 309/309.
  - NOTE: m012 is applied to the live E: DB. Live in-browser smoke test deferred —
    an `ingest.py` run held the DB write-lock at build time.
- [x] **Session 2** — OCR fallback + chunked storage + image upload ✓ 2026-06-18
  - **m013** REBUILDS `upload_staging` (session-1's `CHECK (file_type IN ('pdf','docx'))`
    can't be ALTERed to add `'image'`, and SQLite can't drop a CHECK -> rebuild),
    folding in `ocr_used`, `ocr_pages_count`, `ocr_confidence_avg`, `total_pages`,
    `truncated`; preserves all rows. Adds `upload_staging_chunks` (FULL text in
    100k-char chunks for files past 500k, ON DELETE CASCADE).
  - `uploads.py`: `_extract_pdf` now does page-level OCR fallback (a page below
    `PAGE_TEXT_THRESHOLD=50` chars) and full-file OCR (file avg below
    `FILE_AVG_THRESHOLD=100` chars/page) — catches empty-string pages AND hidden
    scans (barcode/page-number-only). `_extract_image` (.png/.jpg/.jpeg) via
    Tesseract. `_finalize_extraction` applies a 5M hard cap, the truncated flag, and
    chunk slicing. Tesseract is located via `extract._configure_tesseract`
    (TESSERACT_CMD -> SSD-bundled `E:\...\Tesseract\tesseract.exe` -> PATH).
  - Endpoints: `POST /api/staging/{id}/reextract` (one file, 409 mid-extract),
    `POST /api/staging/{subject}/reextract-all` (bulk; `{only_low_quality:true}`
    selects only PDFs with `[Page ` markers averaging < `FILE_AVG_THRESHOLD` — DOCX
    have no markers and are correctly EXCLUDED). Detail adds ocr_*/total_pages/
    truncated/has_chunks/chunk_count; list adds ocr_used/ocr_confidence_avg/truncated.
  - `upload.html`: accepts images, OCR/low-quality/truncated badges, per-row
    Re-extract + a "Re-extract all session-1 files" button (confirm modal).
  - Tests: `test_uploads_ocr.py` (7) + `test_uploads_api_session_2.py` (6); the two
    session-1 test files updated (PDF fixtures now have >100 chars/page so they stay
    off the OCR path; the 500k test now asserts chunks instead of hard truncation).
    Suite 322/322. Live: staging_id 30 (P2 2025 JAN, 20 scanned pages) re-extracted
    411 chars of `[Page N - no text]` -> 27,423 chars of OCR text (conf 32, low-quality
    badge). reextract-all (after a docx-false-positive fix) targets the scanned PDFs.
  - Follow-up fix (2026-06-18): oversized pages. Two CXC scans (P1 2019 MJ, P2 2026 JAN)
    failed bulk OCR — rendering at OCR_DPI=300 exceeded Pillow's decompression-bomb guard
    (`PIL_PIXEL_LIMIT=178,956,970`). `_ocr_render_dpi(page)` now predicts the pixel count
    from `page.rect` and scales DPI down (sqrt of the area ratio, floored at 72) to stay
    under 90% of the limit; Pillow's guard is kept (we adjust our render setting, not the
    security feature). `_extract_pdf` reports `ocr_dpi_reduced`; **m014** adds the
    `upload_staging.ocr_dpi_reduced` column (surfaced in detail+list for a session-3
    "reduced resolution" badge). 3 tests; suite 325. Live: ids 12+27 re-extracted OK
    (ocr_dpi_reduced=1, conf 28/37 — low but extractable); all 105 files now ready.
- [x] **Session 3** — Gemini classification (subject + objective) at build time ✓ 2026-06-17
  - **m015** (`apply_runtime_migrations`, Layer 1 version-tracked) adds
    `skip_classification` / `skip_reason` / `classification_status`
    (unclassified→queued→classifying→classified|failed|skipped) to `upload_staging`,
    plus the `upload_classifications` table (one row per staged file, UNIQUE
    staging_id, ON DELETE CASCADE; CHECK on recommended_folder; objectives_json +
    rationale + model_used + raw_response audit + review_decision/review_folder/
    review_objectives_json). The auto-skip backfill lives in **Layer 2**
    (UNCONDITIONAL, idempotent — so the seed-then-migrate test pattern flags rows and
    new uploads get caught): low-OCR-confidence (<70), reduced-DPI, truncated,
    duplicate-content (same text length >1000, keep lowest staging_id), and format
    twins (PDF/DOCX same stem → DOCX preferred). A later `/unskip` survives until the
    next startup re-asserts a genuine quality skip (intentional — the signal is
    intrinsic). Live E: DB: 105 staged → 75 eligible, 30 skipped (18 format_twin,
    9 low_ocr_confidence, 3 duplicate_content).
  - `backend/classify_uploads.py` (`PHASE: build`): per eligible file (ready, not
    skipped, unclassified unless --force) builds a prompt = full 116-objective POB
    syllabus + the file's first 10000 chars + an inline JSON schema, routed through
    `llm_router.chat_for_classification`. Every objective_id Gemini returns is
    validated against the objectives table and silently dropped if invented (drop
    count noted in the rationale; Rule 1). `_extract_json` tolerates ```json fences /
    surrounding prose. Failures are recorded as a classification row (empty objectives
    + `ERROR:` rationale, status='failed') — retryable. A bulk run self-heals any row
    left stuck in 'classifying' from an interrupted prior run. `model_used` = 'gemini'
    when CLOUD_MODE=1 else 'ollama'. CLI: --subject/--staging-id/--force/--dry-run,
    `@backup_first('pre_classification')`.
  - `llm_router.chat_for_classification`: CLOUD_MODE=1 → Gemini (loud RuntimeError if
    unreachable — never a silent degrade); CLOUD_MODE=0 → warns then Ollama. Existing
    `chat_for_grading` untouched.
  - Endpoints in `app.py`: `POST /api/staging/{subject}/classify-all` (counts eligible
    synchronously → returns `{queued}`, runs `classify_uploads` in a BackgroundTask via
    a lazy import so the runtime server never loads the cloud client at startup),
    `POST /api/staging/{id}/classify` (single, force=True), `GET
    /api/staging/{subject}/classifications` (staged ⋈ classification, classified-first
    ordering — declared BEFORE the `/{staging_id}` detail route so the literal isn't
    swallowed), `POST /api/staging/{id}/review` (ReviewRequest: accepted|overridden|
    rejected + override_folder/override_objectives/notes), `POST /api/staging/{id}/unskip`.
  - `backend/static/upload.html`: a Classification & Review section — "Classify all"
    with progress polling, a filter (All/Pending/Accepted/Overridden/Rejected/Skipped),
    per-file cards (folder badge by token colour, four-tier confidence badge, rationale,
    top-3 objectives expandable, Accept/Override/Reject/View-text), an override panel
    (folder dropdown + searchable objective multi-select pre-filled from Gemini + notes),
    skip cards with Unskip, and a View-text modal reusing the staging-detail endpoint.
  - Tests: `tests/test_classify_uploads.py` (9) + `tests/test_classify_api.py` (5);
    suite 339/339.
  - Live: CLOUD_MODE=1, /api/status gemini_available=true. 3 samples classified —
    P2 2006 (pdf) → 02_PAST_PAPERS/100 (POB-2.4/2.2, span-of-control); lecture-8.docx →
    04_NOTES/95 (POB-10.x, MIS); P2 2008 (ocr'd) → 02_PAST_PAPERS/100 (15 objectives).
    Bulk classify-all returned queued=72.
  - Follow-up (2026-06-18): env-shadow fixed + bulk completed. The invalid machine-level
    `GEMINI_API_KEY=AIzaSy…` (it shadowed the .env `AQ.*` key — `load_dotenv` never
    overrides an existing OS var) was deleted from `HKCU\Environment` + broadcast; .env
    is now authoritative. Bulk run then hit a ~50% failure rate, root-caused to
    **gemini-flash-latest being a thinking model**: gemini_client capped
    `max_output_tokens=2048`, which counts thinking + output together, so heavy-thinking
    files (OCR'd MCQ papers) blew the budget on thoughts (≈6600 tok) and truncated the
    JSON mid-array (finish_reason=MAX_TOKENS). Also plain `response_mime_type` alone
    produced malformed JSON. **Fix in `gemini_client.gemini_chat`:** bump
    max_output_tokens→8192; pass a `response_schema` (via `_to_gemini_schema`, which
    reduces a JSON-Schema dict to Gemini's OpenAPI subset — strips minimum/maxItems/etc.)
    so output is structurally enforced; robust `_response_text` that concatenates parts
    when `.text` raises on a thought response. classify_uploads got a 3-attempt retry.
    Re-ran bulk: **all 105 resolved — 75 classified (0 failed, 0 invalid objective_ids,
    2–15 objectives/file avg 10.5), 30 skipped.** Folders: 46 past_papers, 28 notes,
    1 syllabus. All 75 unreviewed (the UI review pass is the user's next step). 3 new
    gemini tests; suite 342/342.
- [x] **Session 4** — Ingestion trigger + stale-lesson tracking (status→ingested/rejected) ✓ 2026-06-18
  - **m016** (`apply_runtime_migrations`, Layer 1 version-tracked) adds `ingested_at` /
    `ingestion_status` (not_started→queued→ingesting→ingested|failed) / `ingestion_error`
    / `ingested_doc_id`→documents to `upload_staging`; `is_stale` / `stale_reason` /
    `staled_at` to `objective_lessons`; and the `ingestion_log` audit table
    (staging_id, success, chunks_created, objectives_hit JSON, lessons_staled JSON).
    No backfill — column defaults are the right initial state.
  - `ingest.py` gained a single-file entry `ingest_document(db, *, path, subject_id,
    content_type, objectives, embed_fn, preferred_objectives, full_text, source_file)`
    (mints hash doc_id, inserts documents row, splits text into pages on `[Page N]`
    markers — so DOCX/image OCR text from sessions 1–2 ingests too — chunks/embeds/
    indexes, returns doc_id+chunks_created+objectives_hit). `best_objective` +
    `ingest_page` gained `preferred_objectives`: a chunk binds to one of the
    classification's objectives first, falling back to the full syllabus only if none
    clear the keyword threshold — so Gemini's session-3 binding actually steers
    ingestion. `ingest_lessons_for_subject` gained an `objective_ids` filter for
    single-lesson regeneration.
  - `backend/upload_ingest.py` (`PHASE: build`): `ingest_staged_file` validates the
    decision (accepted/overridden, not already ingested), copies the file from
    06_UPLOAD_STAGING into `{KB}/{subject}/{folder}` (override folder wins; collision →
    `_N`; non-ingestable folders 00_SYLLABUS/05/UNCERTAIN are archived, not chunked),
    runs `ingest_document` with the binding hint + the staged extracted text, stales
    every matching `objective_lessons` row, records ingestion_log + staging status, and
    removes the staged original ONLY on success (failure: rollback, remove the KB copy,
    keep the staged file, mark failed, log — never a half-move). `ingest_all_accepted`
    (dry_run reports `would_ingest` without touching anything), `get_stale_lessons`
    (joins ingestion_log.lessons_staled → caused_by_files), `regenerate_lessons`
    (delegates to ingest_lessons, clears is_stale on written objectives).
  - Endpoints in `app.py`: `POST /api/staging/{id}/ingest` (400 unless accepted/
    overridden & not ingested), `POST /api/staging/{subject}/ingest-all`
    (`{dry_run}`; returns queued), `GET /api/staging/{subject}/ingestion-status`
    (declared before `/{staging_id}`; totals + per-file latest ingestion_log data),
    `GET /api/lessons/stale/{subject}`, `POST /api/lessons/{objective}/regenerate`,
    `POST /api/lessons/regenerate-stale/{subject}` — all build work lazy-imports
    upload_ingest in the background task so the runtime server never loads it at startup.
  - `upload.html`: an Ingestion section (status summary "N classified · N accepted · …"
    + "N ingested · N ready · N failed", "Ingest all" disabled while any file is
    unreviewed, confirm modal, per-card ingestion badges + per-file Ingest button, 5s
    polling) and a Stale Lessons section (list with cause files, per-lesson + bulk
    Regenerate, confirm modals, empty state).
  - Tests: `tests/test_upload_ingest.py` (8) + `tests/test_upload_ingest_api.py` (6);
    suite 356/356. No lesson auto-regenerates and no file auto-ingests — both are
    user-triggered. Live (build-only, dry-run): m016 applied to E: DB; ingestion-status
    105 not_started; ingest-all dry_run eligible=0 (all 75 still unreviewed — acceptance
    is the user's next step); stale lessons empty. Full Upload Material feature
    (sessions 1–4) complete: upload → extract → classify (Gemini) → review (human) →
    ingest → regenerate stale lessons (human-triggered).
