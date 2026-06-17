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
  - Note: chat.html feedback buttons (👍👎🤔) degrade silently until Stage 12 adds POST /api/feedback.
- [x] **Stage 11** — Canonical Lessons: pre-generate one lesson per objective at ingestion (objective_lessons), serve deterministically; tutor.txt becomes follow-up Q&A only ✓ 2026-06-16 (objective_lessons table added to app.apply_runtime_migrations in its own try/except sqlite3.OperationalError block — UNIQUE(objective_id) enforces one canonical lesson per objective; lesson_generation_queue reused from Stage 9, not recreated; backend/ingest_lessons.py — offline ollama_chat composer (PHASE: build), per locked-subject objective pulls top-5 vec_notes on content_stmt and, when <2 notes, also top-3 vec_past_papers + top-3 vec_mark_schemes; exact 6-field JSON schema (lesson_text/key_terms/worked_examples/common_mistakes/recall_questions[exactly 3]/confidence); source-anchored system prompt (rewrite-not-author, 200–350 words, define jargon once); local confidence floor (3+ notes→90, 2→70, 1→50, else 30; −20 when only mark schemes; never <30) capped against model self-report via min(); 'no source, no lesson' — zero-chunk objectives queue lesson_generation_queue reason='insufficient_sources' with NO model call; final<floor (default 30)→queue not write; lesson_id=sha256(objective_id|generated_at)[:16]; JSON-encoded array columns; source_chunk_ids=chunks.chunk_id list; --subject/--regenerate(DELETE-then-write)/--confidence-floor/--dry-run; per-objective + totals summary; controller teach route — _fetch_canonical_lesson serves stored lesson deterministically (no ollama_chat) returning lesson_text + recall_questions[list] + key_terms + worked_examples + common_mistakes + lesson_source='canonical' + confidence, checked after the scope gate for both explicit-objective and free-text paths; runtime fallback generates live, adds lesson_source='runtime', and _queue_lesson_generation INSERT-OR-IGNOREs (objective_id,'served_runtime'); both DB touches wrapped in try/except sqlite3.OperationalError so pre-Stage-11 test DBs degrade to runtime; study_plan.html — recall_questions rendered as tappable .recall-pill buttons (first auto-selected as the gradeable card), regex extractQuestionFromLesson kept as a SAFETY NET only, fired with console.warn when lesson_source='runtime' and no recall_questions; prompts/tutor.txt rewritten for follow-up Q&A on a stored lesson (NOT initial generation — that lives inline in ingest_lessons.py); tests/test_lessons.py 6 tests (write/queue-insufficient/regenerate-replaces/regenerate-default-skips/canonical-no-LLM/runtime-queues); test_teach_context updated to stub the two new helpers; suite 258/258. NOTE: live ingest run against Ollama performed for POB — see run log.)
  - Follow-up fix (2026-06-17): lesson_generation_queue was accumulating stale + duplicate rows (every failed/low-confidence run re-INSERTed). Added UNIQUE INDEX idx_lgq_objective_reason on (objective_id, reason) via app.apply_runtime_migrations + ensure_lesson_tables (try/except OperationalError — created only after dedup); _queue_insufficient now upserts ON CONFLICT(objective_id, reason) DO UPDATE created_at instead of stacking rows; a successful lesson write DELETEs that objective's queue rows in the SAME transaction as the insert; summary gained a 'cleared' column. One-off live cleanup: 187→96 rows (7 stale dropped, 84 duplicates collapsed) — queue now == 96 distinct objectives needing work, 0 stale, 0 dup; objective_lessons=20, 20+96=116. tests/test_lessons.py +2 (write-clears-queue, idempotent-requeue); suite 260/260.
- [ ] **Stage 12** — Feedback Loop: per-message 👍/👎/🤔 user_feedback log + targeted teacher-review report
- [ ] **Stage 13** — Panel UX Shell: rebuild chat.html as Learn/Practice/Review/Progress/Library panels with deep-link URL state
- [ ] **Stage 14** (was Stage 8) — Rollout: remaining six subjects through the lock gate
- [ ] **Stage 15** (was Stage 9) — Optional: Open WebUI front-end (v3.1); CrewAI orchestration (v3.2) — never Phase 1
