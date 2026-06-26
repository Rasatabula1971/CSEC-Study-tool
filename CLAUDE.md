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
│   ├── start.bat                   ← SSD check → Ollama → open browser → run FastAPI in FOREGROUND (close the window to stop)
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

## Subject ID Prefix Convention

Every subject's `objective_id` prefix is derived from
`backend/ingest_v2/subject_prefix.py`'s `prefix_for(subject_id: str) -> str`,
backed by the single source-of-truth dict `SUBJECT_PREFIX`:

```python
SUBJECT_PREFIX = {
    "Principles_of_Business": "POB",
    "Economics": "ECON",
    "Mathematics": "MATH",
    "English": "ENG",
    "Principles_of_Accounts": "POA",
    "Integrated_Science": "INTSCI",
    "Information_Technology": "IT",
}
```

**Never hardcode a prefix in extraction or conversion scripts.** Always call
`prefix_for(subject_id)` and let it raise `ValueError` on an unrecognized
subject rather than guessing. Note that `INTSCI` (not the more obvious
`ISCI`) is correct for Integrated_Science — `ISCI` is only the legacy
Bridge-filename form, which `test_generic_office.py` enforces gets
reconciled (rebound) to `INTSCI`, never used as the canonical prefix itself.
If a new extraction script's output uses the wrong prefix, fix the script —
do not change `SUBJECT_PREFIX` or the rebind test to match the script.

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

## Two-Pass Objective Matching (backend/ingest.py)

`best_objective()` (line ~181, via helper `_match_objective()`) binds a
chunk to an objective in two passes, never one:

- **Pass 1** — match against `content_stmt` only (`use_title=False`).
  This preserves every already-confident binding exactly as before.
- **Pass 2** — only for chunks that fell below threshold in Pass 1, retry
  against `content_stmt` + the chunk's section/topic title
  (`use_title=True`). This rescues real content whose vocabulary overlaps
  the topic title but not the (often terse) objective statement itself —
  e.g. a chunk about household cleaning agents that doesn't share words
  with "Examine the effects of..." but does share words with the topic
  title "HOUSEHOLD CHEMICALS."

`MIN_KEYWORD_OVERLAP = 2` (line ~52) is never lowered to "fix" a thin
review queue — a threshold of 1 binds nearly as much junk (metadata,
SBA-admin boilerplate) as real content. If the review queue is still
real-content-heavy after the two-pass enrichment, the fix is a richer
matchable corpus (titles, key terms), not a lower threshold.

This change is shared infrastructure (not subject-specific) and is
regression-checked against POB and Economics before being trusted on any
new subject — confirm identical match counts on the existing locked
subjects' corpora before relying on a change here for a new one.

---

## Lesson Retrieval: Additive, Not Conditional Fallback (backend/ingest_lessons.py)

`candidate_chunks()` (line ~370) builds the source set for lesson
composition. As of the Integrated_Science build, retrieval is **additive**:

- `NOTES_K = 15` — top-15 from `vec_notes`, always pulled first (primary,
  and what drives `local_confidence_floor`).
- `ADDITIVE_K = 5` — top-5 from `vec_past_papers` **and** top-5 from
  `vec_mark_schemes`, always appended after notes, regardless of how many
  notes chunks were found.
- `FALLBACK_K` / `MIN_NOTES_CHUNKS` are retained only for backwards
  compatibility / historical tests — they are no longer the active gate.

**Why the old design failed:** the previous gate only pulled mark-scheme/
past-paper content when `notes < 2` — i.e. it assumed "few notes results"
meant "thin content." But a noisy notes corpus (e.g. syllabus-heading
chunks, textbook index-line fragments, duplicate boilerplate) can return a
full 15 results that are all low-value, so the gate never fires even though
the actual teaching content sits, well-ranked, in `vec_mark_schemes`. This
surfaced on `INTSCI-3.3.7` (flotation): 59 chunks were correctly *bound* to
the objective, but the top-15 semantic match on the bare `content_stmt`
query was entirely headings and index lines — the real Archimedes'
principle definitions ranked outside the top 40 in `vec_notes` but inside
the top 5 in `vec_mark_schemes` for the identical query.

**Lesson for future subjects:** "few results" and "wrong results" are
distinct failure modes. A binding-coverage check (does the objective have
chunks bound at all?) is necessary but not sufficient — also check what the
*lesson composer's actual retrieval query* surfaces, especially for
objectives whose syllabus content_stmt is terse or whose corpus leans
toward textbook front-matter / indexes over teaching prose.

---

## Before Assuming "insufficient_source": Check for a Binding Gap First

If lesson generation reports `insufficient_source` for an objective despite
the subject's corpus plausibly containing relevant material, do not assume
the content doesn't exist. Check, in order:

1. Does the objective have any bound chunks at all? If yes, are they
   actually on-topic (read the chunk text), or did they bind on generic
   shared words in the content_stmt (e.g. "conditions," "determine")?
2. Search `ingest_review_queue` for the objective's real subject-matter
   keywords (not the content_stmt's generic words). Relevant content often
   sits there, correctly un-bound by the keyword matcher but still present
   in the corpus.
3. Check neighboring objectives' bound chunks for misbinds — a chunk
   discussing the right topic can bind to the *wrong* objective if it
   happens to share more keywords with a neighbor's content_stmt. This is
   especially likely when one objective's syllabus heading literally lists
   sub-topics ("(a)... (b)... (c)...") that read like a different
   objective's content.

Two real cases (Integrated_Science): `INTSCI-3.2.3` (tides) had zero bound
chunks but 5 genuine matches sitting in the review queue. `INTSCI-3.3.7`
(flotation) had 20 bound chunks — all off-topic — while the real content
(4 distinct sources, including the literal syllabus heading for 3.3.7) was
misbound to two neighboring objectives. Both were fixed by manual rebind/
promote, not by sourcing new material. Only escalate to "we need new source
content" after confirming the existing corpus genuinely has nothing — same
standard as the Economics Gopie-textbook decision.

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

# Start the study system (the student-facing launcher)
#   Double-click launch/start.bat. It checks the SSD, starts Ollama (left running
#   in the background), opens the browser, then runs uvicorn in the FOREGROUND of
#   its console window. That window IS the running indicator: closing it stops the
#   server cleanly. There is no orphaned background process and no stop.bat
#   (removed) -- closing the window is the stop mechanism. The launcher uses NO
#   --reload on purpose: the reload watcher child can outlive its parent and
#   orphan the server, which is exactly what the foreground launcher fixes.
launch\start.bat

# Dev server (active backend work only -- NOT the student launcher)
#   --reload picks up code changes live. Run it yourself from a separate dev
#   shell; do not bake --reload back into start.bat.
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

## Syllabus Extraction Pattern (proven on Economics + Integrated_Science)

When onboarding a new subject's syllabus from a PDF, follow this sequence —
it was hardened against real failures (a truncated objective, two fake
"objectives" from booklet boilerplate, and unexplained numbering gaps that
all slipped through a first-pass extraction).

1. **Identify the canonical source via content, not filename.** Filename
   keyword matching can miss real matches — e.g. `\bsyllabus\b` fails to
   match `syllabus_effectiveforexamsfrom2027.pdf` because `_` is a word
   character. Always run a content-keyword scan (CXC, CSEC, syllabus, SYLL)
   across candidate files in addition to filename matching.

2. **If multiple candidate editions exist, diff them section-by-section
   before trusting either.** Use a section-aware diff (split on detected
   headers, then `difflib` per section) and explicitly flag any
   objective-like statement present in one edition but not the other.
   Don't assume the newer-looking filename is the correct edition — verify.

3. **Extract with per-page column-boundary detection, not a fixed threshold.**
   CXC syllabi often place the Specific Objectives / Explanatory Notes
   column boundary at different x-coordinates on different pages or modules.
   See `tools/extract_isci_objectives.py`'s `_objective_col_boundary()` /
   `DEFAULT_OBJ_COL_BOUNDARY` for the working pattern — compute the boundary
   per page from a marker regex (e.g. `EXPL_MARKER_RE` matching `(a)`/`(i)`
   sub-markers) rather than hardcoding one x-value for the whole document.

4. **Run a gap-resolution pass — every numbering gap explicitly resolved,
   never left ambiguous.** After extraction, list the full objective-number
   sequence per topic/section. For any gap (e.g. `4.1, 4.3` with no `4.2`),
   re-scan that page range and report whether the number genuinely doesn't
   exist in the source or was missed by the parser. Do not ship an
   extraction with unexplained gaps.

5. **De-hyphenate conservatively — never blanket-join.** PDF line-wraps can
   split words (`inter- conversion` → `interconversion`) but legitimate
   compounds must survive (`non-soapy` stays hyphenated). Use a curated,
   reviewed rule (see `dehyphenate()` / `SOLID_WRAP_PREFIXES` /
   `_HYPHEN_WRAP_RE` in `extract_isci_objectives.py`) and print a full
   before/after diff for spot-check — don't apply a general "join short
   fragments" heuristic.

6. **Verify exam_weight against the syllabus's own "Format of the
   Examination" section — never inherit it from another subject's
   pattern.** Economics and Integrated_Science both ended up `Both` for
   every objective, but that was *verified independently* against each
   subject's actual assessment-grid text, not assumed because the other
   subject happened to use `Both`. A different subject's syllabus could
   genuinely split objectives across papers — check the source, every time.

7. **Extend the skill_type verb list per-subject as new command words
   appear — and ground each addition in CXC's own skill bands, not in
   what another subject happened to need.** Integrated_Science required
   adding examine/investigate/appraise/recommend/determine (→ Application)
   and relate (→ Understanding). Mathematics will likely need its own set
   (solve/derive/prove/simplify) — expect this, don't be surprised by it.

---

## Build Stage Tracker

Update this section when a stage is complete.
The current stage is the first unchecked box.

**A subject is not "done" until it clears three separate gates, in order —
do not conflate any of them when reporting status:**

1. **Syllabus locked** — `syllabus_locked = 1`, all objectives `verified = 1`,
   `exam_weight` populated (not `TBD`) and checked against the syllabus's
   own assessment section.
2. **Ingested** — source documents chunked, embedded, and bound to
   objectives (`chunks` + `vec_*` tables populated). Check *coverage*
   (every objective has ≥1 bound chunk), not just record counts — a subject
   can show thousands of indexed chunks and still have real per-objective
   gaps.
3. **Lessons generated** — `objective_lessons` has a row for every
   objective. "Ingested" does NOT mean "ready to study" — composing the
   canonical lesson is a distinct, separate step (Claude Sonnet, build-time)
   that happens after ingestion, not as part of it.

When asked "is subject X ready," report all three gates' status
individually, not a single yes/no.

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
  - IN PROGRESS (2026-06-22). **ingest_v2 framework built** — a source-family-aware
    adapter pipeline (CaribbeanAIAdapter, MoESLMSAdapter, KerwinMCQAdapter,
    GenericPDFAdapter, GenericOfficeAdapter) driven by per-subject YAML manifests with a
    `wire_adapters`/orchestrator dispatch, replacing v1's single-path `ingest.py` for new
    subjects. POB stays on the v1 path byte-for-byte; the manual `test_pob_parity` gate
    guards this (re-run this session: mismatches=0).
  - **Economics is the first subject through the lock gate**: syllabus CSV converted
    (77 objectives, after recovering 3 tail-truncated ones from 74), MCQ topic map
    resolved (203/203, 0 review), `syllabus_locked=1`, and canonical lessons generated
    via Claude Sonnet (build-time composition only; runtime grading stays Ollama).
  - **Integrated_Science is the SECOND subject through the lock gate (2026-06-22,
    `syllabus_locked=1`).** Built fresh from the canonical 2027 PDF (CXC 23/G/SYLL 23,
    191pg) rather than the flawed prior extraction. New build-time tooling under
    `tools/`: `sort_gpt_folder.py` (read-only intake triage of the GPT-folder dump),
    `inspect_syllabus_candidates.py`, `diff_syllabus_editions.py` (confirmed the
    2027-effective edition is objective-identical to the amended-Oct-2025 print — only
    SBA-admin/whitespace differs), and `extract_isci_objectives.py` (PyMuPDF block/line
    extraction: module from the running `MODULE N:` header, objective from left-column
    `N.M` lines, per-page EXPLANATORY-column boundary to kill cross-column bleed,
    conservative de-hyphenation). **114 objectives** (vs the old 105 that had a truncated
    6.7, two booklet-instruction false rows, and intra-topic gaps), 0 duplicates.
    objective_id = `{module}.{topic}.{objprinted}` → e.g. `INTSCI-2.1.3`; the framework
    `INTSCI` prefix (subject_prefix.prefix_for, NOT the ISCI Bridge-filename form) is
    derived, not hard-coded — test_generic_office.py's ISCI→INTSCI rebind still passes.
    `build_syllabus_csv.py` widened: `SECTION_RE` `(\d+)`→`([\d.]+)` for dotted
    module.topic sections (POB/Econ regression-checked identical); skill-verb sets
    extended (examine/investigate/appraise/recommend/determine→Application,
    relate→Understanding) clearing all 36 UNCLASSIFIED→0; `clean_content_stmt` gained
    curated word-split rejoin (`infectio us`→`infectious`), typo fix
    (`conditios`→`conditions`), trailing `; and,` connector + leaked-section-number
    stripping. `exam_weight=Both` for all 114, **verified against the syllabus PDF's
    FORMAT OF THE EXAMINATIONS + Assessment Grid B** (assessment is by profile dimension
    KC/UK/XS across Paper 01 + Paper 02, both sampling all three modules; no per-objective
    paper mapping) — not inherited from Economics. Loaded via syllabus_parser.py (subject
    row + 19 sections + 114 objectives), all `verified=1`, then locked. CSV at
    `E:\...\Integrated_Science\00_SYLLABUS\integrated_science_syllabus_raw.csv`. Next:
    ingestion (notes/past papers/mark schemes) + canonical-lesson generation via the
    ingest_v2 path. exam_weight P1/P2 split remains the only deferred refinement (Both is
    correct per the PDF; a finer per-objective split is not supported by the syllabus text).
  - **Three independent PRs merged to `main` 2026-06-22** (commits 5e17b88 / 77e8c9e /
    df34995; branches deleted):
      * **#20** — Gemini SDK migration `google.generativeai` → `google.genai` (build-time
        classification; removes the EOL-SDK FutureWarning).
      * **#21** — GenericOfficeAdapter + opt-in OCR (shared `backend/ocr_utils.py`,
        `OCR_TRIGGER_THRESHOLD=30` consolidating three call sites) + `extra_source_roots`:
        lets subjects 3-7 ingest their purpose-built Bridge/Supplemental `.docx` notes
        from `App_Upload_Staging` at first ingestion. Prefix-agnostic (handles
        ISCI→INTSCI / ENGA→ENG). POB parity re-verified mismatches=0.
      * **#22** — `NOTES_K` 5→15 for build-time lesson composition (fixed retrieval
        ranking/cutoff misses; 21/26 Economics `insufficient_source` cleared, ~$0.86).
        Runtime `retrieval.py` k=5 unaffected.
  - Onboarding is documented end-to-end: `backend/ingest_v2/PLAYBOOK.md` (ingest) +
    `LESSON_PLAYBOOK.md` (lesson generation). Phase 0 of both now carries the
    Bridge/Supplemental check (per-subject counts: Integrated_Science 99, Mathematics 85,
    Principles_of_Accounts 50, English 2, Information_Technology 0) and the ISCI/INTSCI &
    ENGA/ENG prefix-reconciliation note.
  - **Information_Technology is the FOURTH subject through all three gates
    (2026-06-25).** Syllabus locked (63 objectives across 8 sections, exam_weight=Both
    verified). INGESTED via the ingest_v2 markdown-adapter path (107 .md notes; manifest
    `enable_markdown_adapter: true`, `enable_office_adapter: false`, no extra_source_roots
    — IT had 0 Bridge/Supplemental .docx). All 63 objectives bound to ≥1 chunk after one
    binding-gap fix: **IT-5.9** ("manipulate one or more worksheets") had 0 bound chunks —
    the dedicated CSEC Hub note "Working with Multiple Worksheets" (9 chunks: create/
    rename/delete/group/move/copy sheets, 3D formulas) had been misbound by the keyword
    matcher across IT-1.1/4.11/4.8/4.1/1.15/1.11 on incidental shared words; rebound all 9
    to IT-5.9 (donors retained ample coverage). LESSONS GENERATED: 63/63 canonical lessons
    via Claude Sonnet, all conf 90 (62 first pass + IT-7.7 on one retry after a
    quality_check_failed recall-question — a non-deterministic model output, not a content
    gap). MCQ topic map resolves 104 (topic,subtopic) pairs from the 119-question Kerwin
    bank. First real student session is the remaining step.
  - **Locked + FULLY BUILT (all three gates): Principles_of_Business, Economics,
    Integrated_Science, Information_Technology.**
    **Remaining (still syllabus-gated): Mathematics, Principles_of_Accounts, English.**
    NOTE: Integrated_Science build applied two real binding-gap fixes — INTSCI-3.2.3
    (tides) and INTSCI-3.3.7 (flotation); see "Before Assuming 'insufficient_source'"
    above (the IT-5.9 fix is the same misbind pattern).
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
  - **Follow-up — auto-accept (source authority), branch `upload-auto-accept`, 2026-06-18:**
    the review gate assumes a subject-expert builder; this user is a parent building for
    his daughter from official CSEC POB sources, so source authority replaces subject
    expertise. `POST /api/staging/{subject}/auto-accept-and-ingest`
    (`{min_folder_confidence: 70}`) bulk-sets every eligible-but-unreviewed
    classification (not skipped, conf ≥ threshold) to review_decision='accepted',
    review_notes='auto_accepted_source_authority', then triggers ingest_all_accepted —
    returns {auto_accepted, skipped_low_confidence, already_decided, queued_for_ingestion}.
    `POST /api/backup {label}` exposes Stage-14 backup_database to the UI. upload.html
    gains a distinct primary `Auto-accept and ingest all` button + confirm modal stating
    the source-authority assumption; on confirm it POSTs /api/backup
    (label=pre_auto_accept_ingest) then auto-accept-and-ingest. **Bugfix:**
    `ingest_all_accepted` filtered `ingestion_status='not_started'` but both ingest-all
    paths pre-set rows to 'queued' first → the worker found nothing; widened to
    `IN ('not_started','queued')`. tests/test_auto_accept.py (3); suite 359.
    LIVE RUN (2026-06-18): auto-accepted 75, ingested **75/75 (0 failed)**, 2592 new
    chunks (file 56 = a 500k-char/669-chunk past-paper compilation), 158 POB docs /
    4587 chunks, all 75 staged files moved into KB, all **20 canonical lessons flagged
    is_stale=1** for user-triggered regeneration. CAVEAT learned: the
    /api/staging/{s}/ingestion-status poll reads the SHARED app.state.db connection, so
    during a large file's long uncommitted transaction it returns a STALE snapshot
    (looked frozen at 45/75 for ~25 min) even though the background worker kept
    committing — verify bulk-ingest completion from a SEPARATE connection, not the poll.
    Per-chunk embedding is slow (ollama_embed keep_alive=0 reloads nomic-embed-text each
    call): ~75 large OCR'd files took ~80 min. The 30 skipped files correctly stay
    not_started.

---

## State as of PDR v3.2 (18 June 2026)

Build status: structurally complete for pilot subject (POB).

- v3.1 playbook Stages 1-13 complete (Stage 13 reverted to v1 UI;
  panel shell preserved at chat_panel_shell.html.bak)
- Stage 14 backup hardening complete
- Upload feature sessions 1-4 complete + auto-accept-and-ingest
  endpoint (PR #14)
- 359 tests passing
- 16 schema migrations applied (m001-m016)
- 20 canonical lessons written, 96 queued (corpus-bound, not
  architecture-bound; upload feature exists to close this gap)
- 105 POB files staged, 75 Gemini-classified, 0 yet ingested

Per PDR v3.2 Section 8: no further architectural change until real
student use generates real feedback. The next legitimate input is
reality, not engineering review.

Next actions in order:
  1. Run POST /api/staging/Principles_of_Business/auto-accept-and-ingest
  2. Regenerate stale lessons (if any) via /api/lessons/regenerate-stale
  3. Re-run python backend/ingest_lessons.py --subject Principles_of_Business
     (96 queued objectives may now produce lessons against enlarged corpus)
  4. First real session with Rylee on three POB objectives

---

## Lesson quality fix (18 June 2026)

A POB-1.11 "lesson" showed hallucinated "Section N" citations + chat
boilerplate. Root cause was the runtime teach FALLBACK, not a stored lesson:
controller._handle_teach generated freeform prose via tutor.txt when no canonical
lesson existed, and study_plan.html scraped fake recall_questions from that prose.

Fixes (all on `main`):
  - controller._handle_teach NO LONGER generates lessons at runtime. With no
    canonical lesson it returns an honest placeholder (syllabus statement quoted),
    lesson_source='placeholder', recall_questions=[], full response shape kept
    (source_file=None, page=None, context_source='syllabus' — VAL-08), and queues
    reason='served_placeholder'. Runtime/build separation now enforced for teach.
  - study_plan.html: removed the extractQuestionFromLesson regex; empty
    recall_questions renders "No recall questions available for this lesson."
  - ingest_lessons._validate_lesson_quality gates every INSERT (rejects 'According
    to Section', chat boilerplate, answer leakage '(Answer: …)', count!=3,
    too-short, junk non-question/non-command; accepts '?' OR a CSEC command-word
    start). Failures queue reason='quality_check_failed: <why>'.
  - The new validator caught 2 pre-existing polluted stored lessons the Stage-11
    confidence floor missed: **POB-3.6** (junk schema-field echoes) and
    **POB-10.13** (answer leakage). Both deleted + re-queued
    (reason='quality_check_failed: pre-existing'). 18 of 20 stored lessons remain.
  - 7 teach tests updated to the placeholder contract; 9 new validator/controller
    tests. Suite 370.
  - Follow-up: 'name' + 'give' added to the validator's CSEC command-word set (real
    'Name three…' / 'Give two…' stems). Suite 372.

## Lesson-status page (19 June 2026)

Read-only live view of lesson generation (no new state — pure DB read of
objective_lessons + lesson_generation_queue):
  - GET /api/lessons/status/{subject_id} → {total_objectives, lessons_written,
    lessons_stale, lessons_queued, queue_by_reason (grouped on the reason PREFIX
    before ':'), recent_activity (lesson_written/staled/queued union, newest-first,
    ≤10)}.
  - GET /lessons/status → backend/static/lesson_status.html, auto-refreshes every 5s,
    updates in place. Discreet "Lesson Status →" link added to welcome.html topbar
    (ungated — it's just numbers). 5 tests; suite 377.

## Confidence=0 no-signal fix (19 June 2026)

ingest_lessons.py discarded good lessons when the model self-reported
confidence=0. llama3.2:3b often returns 0 even after composing a complete,
valid lesson, and the old code computed `final_conf = min(model_conf, floor)`
→ `min(0, 90) = 0` → below the floor → queued as insufficient_sources. Verified
live against POB-3.13 (1119-char lesson + 3 clean recall questions, conf=0,
discarded).

Fix (ingest_lessons.py, section (e)): treat `model_conf <= 0` as "no signal"
and fall back to the local source-quality floor; a genuine non-zero
self-report still gets the `min()` cap as before. The floor still rejects
truly weak material (0 notes → floor 30 max). 1 new test
(test_zero_model_confidence_uses_source_floor); suite 378.

Re-ran `ingest_lessons.py --subject Principles_of_Business --regenerate` (full
116-objective pass, not just the stale set): written 46 · queued 61 · errored 9
· cleared 46. objective_lessons went ~18 → 53 (47 clean, 6 still stale). Stored
confidence distribution: 43×90, 7×80, 1×75, 2×60 (no low-conf garbage written).
The 61 queued are genuine low non-zero model self-reports (conf 1–15, correctly
capped); the 9 errored are model-call exceptions (retryable). On --regenerate an
existing lesson is deleted only once a passing replacement is in hand, so the 6
objectives that errored/queued this run kept their prior (stale) lesson.

## Confidence floor-only fix (19 June 2026) — supersedes the no-signal fix above

Same root cause, second iteration. The no-signal fix only rescued
`model_conf == 0`; non-zero low self-reports were still capped by
`min(model_conf, floor)` and queued. Live diagnostic on POB-1.11: 5 notes
chunks (floor 90), a coherent 1829-char lesson + 3 valid recall questions, but
`confidence=5` → `min(5, 90) = 5` → below the 30 threshold → queued. llama3.2:3b
returns 0/5/10 even for good lessons — its self-confidence is uncalibrated noise
on this task.

Fix (ingest_lessons.py, section (e)): drop the model self-report from the
final_conf decision entirely — `final_conf = local_confidence_floor(chunks)`.
The remaining safety nets are unchanged: `confidence_floor=30` still rejects
zero-notes objectives (floor 30 max), the `_validate_lesson_quality` gate still
queues malformed lessons (boilerplate / section citations / answer leakage /
bad recall questions), and a model/JSON failure still errors→retryable. 1 new
test (test_low_model_confidence_still_uses_source_floor); one legacy assertion
updated (test_ingest_writes_lesson_when_sources_sufficient now expects the floor
90, not min(85,90)=85); suite 379.

Re-ran the full `--regenerate` pass: written 112 · queued 4 · skipped 0 ·
errored 0 · cleared 73. DB after: objective_lessons = 114, all clean, 0 stale,
all conf=90 → **98.3% lesson coverage (114/116)**. POB-1.11 now WRITTEN at
conf=90. The only 4 not written are quality_check_failed (answer-leak / bad
recall question) — the validator gate doing its job, not a confidence problem;
2 of those kept a prior clean lesson, 2 have none.

## Lesson prompt v2 — Sonnet for build (19 June 2026, PDR v3.2)

Architectural cost-separation decision (by environment + who pays):
  - **Lesson composition** (build-time, once per objective) → Claude **Sonnet**
    via the Anthropic API on the BUILDER's machine. Builder pays Anthropic
    (~$20 one-time for all seven subjects' lessons).
  - **Classification** of student-uploaded files → Gemini **free tier**
    (student-side, no API key for the student to manage). Unchanged.
  - **Grading** (runtime, every answer) → **Ollama** only. Offline guarantee
    intact and unchanged.

Implementation (branch `lesson-prompt-v2-sonnet`):
  - `backend/anthropic_client.py` (PHASE: build) mirrors gemini_client:
    `anthropic_chat(messages, system, schema)` (schema → Anthropic tool-use,
    returns the tool input as a JSON string), `is_anthropic_available()`. Reads
    `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`).
    **Both `.strip()`ed** — a pasted key with a trailing newline made the HTTP
    Authorization header illegal (httpx LocalProtocolError); caught by a 1-call
    preflight before the paid batch.
  - `llm_router.chat_for_lesson_composition` → Anthropic when a key is set, else
    Ollama (loud warning). **Never Gemini.** anthropic is imported LAZILY inside
    the function, so no PHASE: runtime module imports a cloud client (VAL-01 holds;
    llm_router stays PHASE: dual). requirements: `anthropic>=0.39.0`.
  - `prompts/lesson_structurer.txt` — the authored, version-controlled Lesson
    Structurer prompt (per-subject rules + per-subject word bands inlined). New
    lesson format: `lesson_text` ends with a single `Q: ` line; the model returns
    `status: ok|insufficient_source`, `active_recall_question` (ONE, was 3),
    `sources_used`. Word bands: narrative (POB/Econ/English/IT) 350–650, calc
    (Maths/POA) 400–800, Integrated_Science 400–700; **hard floor 300**.
  - `backend/ingest_lessons.py`: default `chat_fn=chat_for_lesson_composition`;
    `_normalize_subject_id` guard (canonical id, raises on unknown);
    `_build_lesson_input` (JSON the prompt documents, incl. section_title /
    objective_num / exam_weight / source_excerpts) + `_parse_lesson_json`
    (fence-tolerant); `status='insufficient_source'` → queued, never written;
    `active_recall_question` wrapped as a 1-element `recall_questions` list;
    worked_examples/key_terms/common_mistakes no longer separate (embedded in
    lesson_text, stored empty). `_validate_lesson_quality`: exactly **1** recall
    question (was 3) + **300-word floor**. New `--objectives POB-1.11,POB-3.1,…`
    flag for targeted regeneration. The old inline LESSON_SYSTEM/LESSON_SCHEMA
    removed.
  - `backend/static/study_plan.html`: `renderRecallPills` renders a single recall
    question directly (no "pick one" header); legacy 3-question lessons keep the
    tap-to-choose header. Both shapes handled during the transition.
  - Tests: `tests/test_lesson_prompt_v2.py` (9: subject-guard ×3, routing ×3 —
    Anthropic/Ollama-fallback/not-Gemini, validator ×3 — 1-question + 300 floor);
    `tests/test_lessons.py` updated to the v2 format (1 question, ≥300-word fakes).
    Suite **392** (no regressions; VAL-01 still passes).

Cost-bounded test batch (NOT a full regen — pending user approval after review):
  - Backup `csec_…_pre_sonnet_test_batch.sqlite` taken first.
  - `--regenerate --objectives` on 10 POB objectives: **written 9, queued 1,
    errored 0**. POB-3.1's Sonnet lesson came back 265 words (< 300 floor) →
    quality_check_failed → queued; its prior lesson is untouched (regenerate only
    deletes once a passing replacement is in hand). POB-1.11 (the original bug
    case) is now a clean 417-word legal+ethical lesson. Samples written to
    `pob_sample_lessons.txt` for review. **Full --regenerate deferred** to user
    sign-off after reading the samples (~$20 for all 116 / all subjects).

  - **Tiered word floor (follow-up):** the flat 300-word floor wrongly queued
    POB-3.1 ("Define the term entrepreneur") — an honest Define lesson runs ~265–300
    words, and the prompt forbids padding, so it oscillated at the boundary and
    failed ~half its runs. Fixed by tiering the floor by command word in
    `_word_floor_for_objective`, mirroring the prompt's COMMAND-WORD REGISTER:
    Define/State/List → 180, Draw/Sketch/Illustrate → 250, Explain/Describe +
    Calculate/Solve/Apply/Use/Construct → 300, Discuss/Analyse/Compare → 350,
    unrecognised → 300 default; with multiple command words the HIGHEST-demand floor
    wins. `_validate_lesson_quality` now takes `command_words` (the orchestrator
    passes `obj["command_words"]`). This matters for the full run and every subject:
    all seven have short-answer objectives that would false-reject under a flat floor.
    POB-3.1 then wrote a clean 279-word Define lesson (conf 90). +6 tests; suite 398.

  - **Robustness fixes (follow-up, branch `lesson-robustness-fixes`):** the full POB
    regen surfaced two failure classes that would recur across the 7-subject rollout.
    (1) **Tool-use JSON.** `_compose_lesson` now passes `LESSON_OUTPUT_SCHEMA` instead
    of `schema=None`, so `anthropic_chat` uses Anthropic's tool-use path and the SDK
    returns structurally valid JSON (`json.dumps(block.input)`). A lesson that
    legitimately quotes a phrase (POB-6.6: `"two for the price of one."`) no longer
    breaks `json.loads` on unescaped quotes — the entire failure class is gone, not
    just the instance. The schema is one object covering BOTH shapes (status enum;
    only status/subject/objective_ref required, so 'insufficient_source' omits the
    rest). (2) **Contextual boilerplate filter.** Bare-substring checks
    (`'clarification' in lower`) false-flagged a communication lesson (POB-2.13). New
    `_has_conversational_break` matches phrase-level, reader-addressed regex
    (`CONVERSATIONAL_BREAK_PATTERNS`: `let me know if`, `feel free to ask` [excluding a
    third-party object so "ask the customer" passes], `I hope this helps`, …) — so
    domain vocabulary passes and only assistant-voice breaks are caught. Re-ran the 4
    stuck objectives: **POB-2.13, POB-1.7, POB-6.11 wrote** (442/452/422 words, conf
    90); only **POB-1.14** stayed `insufficient_source` (careers content genuinely
    absent). insufficient_source is a non-deterministic model judgment — POB-1.7/6.11
    declined on the full run, composed cleanly now. **POB coverage: 115/116 fresh v2.**
    +6 tests; suite **404**.

## /plan jump-to-objective + batch navigation (19 June 2026) — UX only

The /plan page only served objectives in fixed batches starting from the lowest
objective_id; there was no way to jump to a specific topic (blocking both the
builder testing lesson-quality fixes on one objective and a student re-studying a
topic before a test). Two small additions, no design overhaul:

  - **GET /api/objective/{objective_id}** (app.py): looks up the objective's
    subject_id, then routes through `controller.handle_request(route='teach')` —
    the SAME path the batch loader uses — so a stored canonical lesson is served
    deterministically and an objective with none returns the existing placeholder
    contract (lesson_source='placeholder', recall_questions=[], source_file=None,
    page=None, context_source='syllabus'). 404 (HTTPException) when the id is
    unknown. No new controller logic.
  - **study_plan.html "Jump to objective"** input above the batch area:
    normalizeObjectiveId accepts "POB-3.1" / "pob-3.1" / "POB 3.1" / bare "3.1"
    (bare number gets the subject's objective prefix, learned from
    /api/objectives). Valid id → single-objective view (lesson + recall questions,
    display-only) with a "← Back to batch" link that RESUMES an in-progress batch
    where it left off; invalid/unknown → "Objective not found".
  - **study_plan.html Previous/Next within a batch**: a "◄ Previous | Question N of
    M | Next ►" footer on each step; the last step shows "Finish batch". Navigation
    does NOT submit — it just moves between objectives, and per-step answers are
    preserved (state.plan.answers) so you can skim, answer some, and come back. The
    post-grade "Next objective" action now routes through the same navStep().
  - 4 new tests in tests/test_study_plan.py (real in-memory DB + TestClient:
    /plan→200, /api/objective returns canonical lesson+recall, unknown→404,
    no-lesson→placeholder contract). Suite 383. Live-DB verify: POB-3.1 → canonical
    (3 recall qs), POB-99.99 → 404. NOTE: the dev server runs without --reload, so
    restart it to pick up the new /api/objective route (static /plan reflects edits
    immediately).

## /plan jump-to-objective missing answer submission (19 June 2026) — bug fix

The jump-to-objective view loaded the lesson + recall question but had NO answer
textarea or Submit button — it could be read, never answered or graded. Root cause
was a SEPARATE, incomplete template: `renderSingleObjective` called `showPlanQA(false)`
(hiding the shared answer section) and rendered the recall questions as inert
`cursor:default` text, by design ("Display-only — grading lives in the batch flow").
The batch view, by contrast, drives the one shared answer block (`#answerTextarea` +
`#submitBtn`, toggled by `showPlanQA`) via `loadLesson` → recall pills →
`setActiveRecallQuestion`/`showQuestion` → `submitBatchAnswer` → `POST
/api/plan/grade_batch`.

Fix (frontend only — `backend/static/study_plan.html`):
  - `renderSingleObjective` is now async and reuses the EXACT SAME renderer the batch
    view uses: `showPlanQA(true)` + a `#planLessonHost` div + `await loadLesson(objective)`
    (objective built as `{objective_id, objective_num}` from the id, no extra round-trip).
    loadLesson draws the lesson, tappable recall pills, auto-selects the first as the
    gradeable card and enables the shared textarea + Submit. The "← Back to batch" link
    stays on top; Previous/Next footer stays hidden (batch-only). A placeholder /
    no-recall objective falls back to `showPlanQA(false)` (lesson shown, nothing to grade).
  - Grading reuses `submitBatchAnswer` unchanged except a jump branch: `/api/plan/grade_batch`
    needs a `batch_id`, but a per-objective grade is scored by `grade_against_syllabus`
    independent of the batch's objective list, so the batch is only a subject/scope carrier.
    New `ensureJumpBatch()` reuses an in-progress batch's id, else opens ONE lightweight
    context batch (`/api/plan/start_batch`) and caches it in `state.jumpBatchId`. Post-grade
    in jump mode renders the grade + teach-the-missed (`showMissed`) but skips the batch
    summary/step-advance; jump mode is detected via `state.plan.phase==='jump'`. No backend
    change.
  - 2 structural tests in tests/test_study_plan.py (served /plan reuses the shared flow:
    async renderSingleObjective + `await loadLesson(objective)` + ensureJumpBatch + the
    grade call + textarea/submit present; old `Display-only`/`cursor:default` markers gone).
    Suite 406. Live verify: GET /plan serves the new wiring (no old markers);
    /api/objective/POB-3.2 → canonical lesson + recall question; the jump grade path
    (start_batch → grade_batch with objective_id+question_text) returned score 100%, 3/3,
    3 mark points — identical shape to the batch view's renderGrade.

## Tutor Chat teach render — field-name fix (19 June 2026) — bug fix

A targeted UI smoke test (every clickable control on every served page, checked
against its live backend response) found ONE confirmed broken button across all six
pages: **chat.html Teach**. The /api/chat teach response returns the lesson under
`lesson_text` (the v2 canonical-lesson field; `_shape_for_ui` does NOT alias it), but
chat.html read `data.lesson || data.text || data.response || JSON.stringify(data)` —
all three aliases were undefined, so the teach branch fell through to
`JSON.stringify(data)` and dumped the raw response object into the chat bubble.
study_plan.html already read `data.lesson_text` correctly (loadLesson); chat.html was
never updated to match.

Fix (`backend/static/chat.html` ~line 964, one line): read `data.lesson_text` first,
keeping the legacy aliases + JSON.stringify as defensive fallbacks. No other field
mismatch in the same render block (grade branch reads `data.points`/`objective_id`
correctly). Rendering parity needed no further work: `.message.ai .message-bubble` is
already `white-space: pre-wrap`, so the lesson's `\n\n` paragraph breaks render; the
v2 `lesson_text` already ends with the recall question as a trailing `Q:` line, so it
shows read-only (matching chat.html's bubble UX — its grade mode uses a separate
question-picker, not the recall question). `**bold**` stays literal in BOTH pages
(neither renders markdown — that IS the parity; adding markdown would diverge).

Tests: `tests/test_chat.py` (4 structural HTML guards — reads `lesson_text` first, old
raw-dump-first chain gone, `lesson_text` precedes the JSON.stringify fallback, bubble
is pre-wrap), matching the read-the-served-markup convention in test_panel_shell.py.
Suite 410. Live verify: GET /chat serves the fix (old buggy line gone); teach payload
for POB-1.6 (the bug-report objective) renders as a formatted canonical lesson with
paragraph breaks + the trailing recall Q, not a raw JSON dump. Smoke-test recon also
re-confirmed full lesson coverage: 116/116 objective_lessons (POB-1.14 a genuine
1635-char lesson, conf 90).

## UI overhaul — session 1 of 3: backend foundations (19 June 2026, branch `ui-overhaul-backend`)

First of three sessions building the UI overhaul the student requested after her
first real use of the system (sticky subject across pages, a one-time first-launch
welcome message, retry-rescore for recall questions). Session 1 is backend-only —
no frontend changes; sessions 2 (Welcome + upload + first-launch) and 3 (Study +
Quiz + Builder) build on these endpoints. Branch left open, NOT merged.

  - **m017** (`apply_runtime_migrations`, Layer 1 version-tracked) adds the generic
    `app_state` key-value table (single-student, no-accounts app: keys
    `current_subject_id` + `welcome_message_seen`) and `study_sessions.is_retry`
    (1 = re-attempt, 0 = first try). Both are ALSO added to `schema.sql` (the
    source of truth for fresh test DBs) so the controller's `is_retry` INSERT works
    on a schema-built DB without a migration call; on the live E: DB (predates both)
    m017 creates them, and on a fresh schema DB the bundled ALTER raises duplicate-
    column → recorded `[pre-existing]`. Applied to live E: DB. (Lesson: inline `;`
    in a `schema.sql` comment breaks the tests' naive `split(";")` — keep comments
    semicolon-free.)
  - `backend/app_state.py` (PHASE: runtime): `get_state`/`set_state` (upsert),
    `get_current_subject` (defaults to the first `syllabus_locked=1` subject
    alphabetically when unset), `set_current_subject` (raises ValueError unless the
    subject is locked — never strands the student on a gated subject),
    `has_seen_welcome_message`/`mark_welcome_message_seen`.
  - **Retry-rescore.** `grade.grade_answer` gained `is_retry=False` and echoes it
    into the result, but does NOT itself persist — the `study_sessions` write (shared
    by the mark-scheme AND syllabus-fallback grade paths) lives in
    `controller._handle_grade`, which now reads `is_retry` from the request, passes it
    to `grade_answer`, and flags the `study_sessions` row (`is_retry` column).
    `ChatRequest` gained `is_retry: bool = False` so the /api/chat grade turn carries
    it. Confirmed this session: `weakness.log_weakness` already UPSERTS by
    objective_id (SELECT existing → UPDATE score/box/next_review, else INSERT — one
    row per objective), so a retry overwrites the visible result + the Leitner
    decision while the original attempt stays in `study_sessions` history. NO change
    to weakness.py. (Caveat for a later session: the retry's new box is
    `update_leitner(box_left_by_first_attempt, retry_score)`, so a fail-then-pass
    retry recomputes from box 1, not the pre-attempt box — a value upsert, not a stack.)
  - New endpoints in `app.py`: `GET/POST /api/state/subject` (POST validates via
    `set_current_subject` → 400 `{ok:false,error}` on unlocked/unknown using the
    injected-`Response`-status pattern), `GET/POST /api/state/welcome-seen` (POST is a
    one-way flag, no body).
  - Tests: `tests/test_app_state.py` (4), `tests/test_grade_retry.py` (3, via the
    controller grade route with a stubbed examiner — asserts is_retry=0/1 rows both
    present + weakness reflects the retry score), `tests/test_app_state_api.py` (3).
    Suite **420**.

## UI overhaul — session 2 of 3: Welcome page, first-launch, student upload (19 June 2026, branch `ui-overhaul-backend`)

Second of three UI overhaul sessions, on the same branch as session 1 (still NOT
merged — session 3 builds on it). Builds on session 1's app_state endpoints.

  - **First-launch message** (`backend/static/first_launch.html`): a full-screen,
    centered, dark (#13151a) one-time message from the builder to his daughter
    ("…the next best thing — made with love, for you. — Dad"). `GET /` now branches
    SERVER-SIDE on `app_state.has_seen_welcome_message` (no client flash): unseen →
    first_launch.html, seen → the Welcome page. Continue POSTs
    `/api/state/welcome-seen` then navigates to `/`. Shown exactly once, ever; no UI
    reset (a builder DB edit if ever needed). `GET /` previously served chat.html —
    chat UI is still at `/chat`.
  - **welcome.html fully rebuilt** (old design removed; was the greeting/add-notes/
    nav page). Self-contained vanilla JS + the shared dark/blue CSS custom-property
    palette (defined at the top of the file; sessions 2-3 reuse the SAME tokens):
    header subject dropdown (persists via session 1's `/api/state/subject`, reloads
    only the status section on change), a single hardcoded quote (rotation
    intentionally deferred, noted in a comment), a live status row (`X of Y mastered
    · N due today` + circular % badge — reuses the EXACT `/api/plan/progress/{subject}`
    + `/api/due/{subject}` endpoints the /plan page uses, not a recompute), three
    actions (Continue studying → `/plan`, Browse all topics → `/plan#topics` placeholder
    for session 3's objective map, Practice → `/quiz`), a drag-and-drop upload box, and
    a discreet PIN-gated Builder link.
  - **`POST /api/student-upload`** (student-facing, DELIBERATELY separate from the
    builder's `/api/upload` staging workflow): synchronous single file →
    `uploads.stage_file` → `uploads.extract_text` → `classify_uploads.single_file_classify`
    (new minimal refactor: a thin wrapper over the existing single-file `classify_uploads`
    path, used by both the CLI and this endpoint; never touches other files' queue
    state) → decide. `folder_confidence >= 85` AND exactly one objective at
    confidence >= 85 → auto-accept (`review_notes='auto_accepted_student_upload'`) +
    `upload_ingest.ingest_staged_file` → `{outcome:'added', section}`. Otherwise leave
    it staged + unreviewed (`review_notes='pending_student_upload_review'`) for the
    builder's existing `/upload` queue → `{outcome:'needs_review'}` (no confidence/
    technical detail shown to the student). Hard failures (bad type/empty/too large/
    extraction-failed/classification-failed) → `{outcome:'error', message:<friendly>}`
    with the real error logged server-side, never returned. Always HTTP 200 so the
    front end branches on `{ok, outcome}` only. Design note: step 5/6 of the spec
    overlap on "classification failed"; resolved as — parsed-but-low-confidence →
    needs_review, hard classification failure (e.g. Gemini unreachable, recorded as
    status='failed') → error but the file is kept for the builder.
  - **Builder PIN**: none existed before — built fresh. `POST /api/builder/verify-pin`
    checks SERVER-SIDE against `BUILDER_PIN` in .env (default '1971'; added to
    .env.example) so the value never ships to the browser. welcome.html's modal counts
    its own three wrong attempts then hides until refresh. `GET /builder` is a
    placeholder (serves the Upload Material page) until session 3 builds the console.
  - Tests: `tests/test_first_launch.py` (3 — unseen→first-launch, seen→welcome,
    transition), `tests/test_student_upload.py` (4 — added+ingested, needs_review-not-
    ingested, clean error with no stack-trace leak, distinct-from-builder-batch). Two
    existing test_api.py markers updated for the new `GET /` + welcome content. Suite
    **427**.
  - Live verify (server on :8001 — a stale dev server held :8000): `GET /` served
    first_launch ("next best thing") while unseen → POST welcome-seen → `GET /` served
    Welcome ("Continue studying"); `/api/state/subject` = POB; PIN 0000→false, 1971→true;
    `/welcome` has the dropdown + dropzone; `/builder` → 200; status numbers
    mastered 1 / 116 / due 2 match `/plan`. Backup `csec_…_pre_ui_session_2_test.sqlite`
    taken first; welcome flag left reset to '0' so Rylee's real first launch still shows.

## UI overhaul — session 3 of 3: Study rebuild, Quiz restyle, Builder console (19 June 2026, branch `ui-overhaul-backend`)

Final UI-overhaul session (PR opened for user review — NOT merged). Built from
Rylee's real first-use feedback: objective map, collapsed lessons, question-after-
reading, retry-with-missed-points, and a real Builder console.

  - **Task 1 — shared palette.** The dark/blue tokens (`--bg-page`, `--accent-blue`,
    `--text-body`, …) moved from welcome/first_launch into shared.css §2b (one
    definition). welcome.html + first_launch.html now `<link>` shared.css and dropped
    their local `:root`. study_plan.html + quiz.html adopt the palette by re-pointing
    the legacy surface/text/accent tokens (`--ink`/`--booklet`/`--paper`…) onto the
    blue ones at page level, so existing shared.css component rules render blue
    without a rewrite.
  - **Task 2 — objective map.** New `GET /api/objectives/{subject}/map`: objectives
    grouped by section, each `status` = mastered (study_plan.status='mastered', the
    SAME model as get_plan_progress — counting map 'mastered' == progress mastered) /
    attempted (has a study_sessions row) / not_started, plus `is_next_due`
    (get_due_objectives). study_plan.html renders it as an in-page collapsible section
    (sections collapsed except the one holding the next-due objective; status dots
    green/amber(due)/blue(attempted)/grey-ring; row click → the unified renderer). The
    progress header numbers are derived FROM the map data so header + map can't disagree.
  - **Task 3 — unified lesson flow.** ONE renderer `renderObjectiveLesson(objective,
    {origin})` reached by all three entry points (batch step, jump, map click) —
    collapses the divergent-render-path bug class. Lesson renders COLLAPSED (2-sentence
    preview + "Read full lesson ▾", `formatLesson` does **bold** + preserves `\n\n`);
    the recall question is hidden behind a "Ready for the question →" gate; on submit
    the shared missed-points feedback shows; **one** retry ("Try again" → is_retry=true
    → then "Next"; unlimited retries deliberately NOT built — noted per task). A slim
    back/objectives/next footer is consistent across views; small sticky subject
    dropdown in the header (POSTs /api/state/subject). To make Study grading record
    (so the map's "attempted" reflects Study, and retries flag history),
    `/api/plan/grade_batch` + `_handle_grade_batch_question` gained `is_retry` and now
    INSERT a per-objective study_sessions row (session 1's parameter, extended to the
    batch path — synthesis unchanged).
  - **Task 4 — Quiz restyle (visual only).** Segmented mode toggle, header sticky
    subject dropdown, question card/answer box styled identically to Study, and the
    grade card replaced by the SHARED feedback component. New `backend/static/feedback.js`
    (`renderMissedFeedback`) is `<script src>`-included by BOTH Study and Quiz — a
    genuine shared partial, not copy-paste. Load Question / Submit logic untouched.
  - **Task 5 — Builder console.** `backend/static/builder.html` (replaces the
    placeholder): links to /upload + /lessons/status + a "Reset welcome message"
    action (`POST /api/state/welcome-reset` → flag '0'). Client-side PIN gate reuses
    session 2's `/api/builder/verify-pin` (blurred until unlocked; sessionStorage
    'builder_ok' set by the Welcome modal so it doesn't double-prompt). BUILDER_PIN
    default 1971.
  - Tests: test_objective_map.py (3), test_lesson_flow_unified.py (3: shared lesson
    path + is_retry 0→1 sequence), test_quiz_restyle.py (4), test_builder_console.py
    (3), test_shared_tokens.py (4); two existing markers in test_study_plan.py updated
    to the unified renderer (openSingleObjective / renderObjectiveLesson), two existing
    test_api markers already updated in session 2. Suite **443**.
  - Live verified on :8000 (port freed by killing a zombie multiprocessing-fork child
    holding the inherited socket): / served first-launch while unseen → after
    welcome-seen served Welcome; /plan has the map + ready gate + unified renderer +
    feedback.js; map endpoint returned 10 sections / 116 objectives with statuses +
    is_next_due; /quiz has the segmented toggle + shared feedback + header dropdown;
    PIN 0000→false / 1971→true; /builder serves the gated console; welcome-reset
    flipped the flag and **welcome_message_seen left at 0** (Rylee's first launch
    untouched). Backup csec_…_pre_ui_session_3_test.sqlite taken first. NOTE: live LLM
    grading not re-run here (covered by tests); the grade endpoints were verified to
    serve, the retry is_retry wiring by integration test.

## Launcher: retire the bypassing .vbs + harden Continue (20 June 2026)

Three live symptoms (stale "open Task Manager / end python.exe" dialog, a silent
~20s startup that caused repeat double-clicks, and a Continue button that hung)
all traced to ONE thing the prior foreground-`start.bat` fix never touched: the
**desktop shortcut ran `C:\Users\ricky\Desktop\CSEC Study Partner.vbs`**, a
separate launcher created at setup that bypassed `start.bat` entirely — it ran
`shell.Run "...uvicorn...", 0, False` (hidden + detached, so the orphan problem
persisted and there was no window to close) and showed the Task Manager MsgBox.
`start.bat` was correct; she just never ran it.

Fixes:
  - **Desktop shortcut repointed** to `launch\start.bat` (Normal window, Start-in
    the repo). `.vbs` renamed → `.vbs.old` → deleted after live verify; a second
    shortcut under `OneDrive\Videos\Desktop` that also pointed at the `.vbs` was
    repointed to `start.bat` too. (A pre-existing `CSEC Study Partner.vbs.bak` on
    the Desktop and a stale repo copy at
    `OneDrive\Dokumente\Workflow\CSEC-Study-tool.inspect` were left for manual
    cleanup — not in the run path.)
  - **`start.bat` now self-loads `SSD_ROOT` from `.env`** when it isn't already in
    the environment (the `.vbs` read `.env` via Python and never needed the shell
    var; a plain double-click otherwise died at the SSD gate since `SSD_ROOT` is set
    nowhere on this machine). `.env` stays the single source of truth.
  - **`start.bat` prints an explicit "starting up — ~20 seconds, the page will look
    blank, that's normal, just wait" message** during the cold-start window that was
    previously silent. The ~20s is FastAPI lifespan's pre-warm `ollama_chat` loading
    `llama3.2:3b` (measured: ~19.6s cold / ~2-3s warm); the delay is unavoidable, the
    invisibility was the bug.
  - **`first_launch.html` Continue button hardened**: the `welcome-seen` POST is now
    time-boxed by an `AbortController` (5s) and navigation lives in a `finally`, so a
    POST that hangs during cold start (a plain `fetch` only rejects on a connection
    error, not a slow response) can never trap her on the message screen again. Worst
    case on a true failure: the flag isn't set and she sees the message once more.
  - tests/test_first_launch.py +2 (served-markup guards: timeout/finally present, old
    no-timeout pattern gone). Suite **445**. Live: launched via the shortcut → visible
    console + startup message + uvicorn as the console's child; closing the window
    stopped the server (uvicorn gone, :8000 free, Ollama left running); `GET /` served
    first_launch with the hardened handler.

## UI overhaul MERGED + confirmed in real student use (20 June 2026)

PR #17 (`ui-overhaul-backend` → `main`, merge commit 4d0749e) is merged and the
branch deleted. The three-session UI rebuild is now live on `main`: Welcome page,
one-time first-launch message, Study with the objective map + collapsed lessons +
retry-with-missed-points, Quiz restyle (shared feedback partial), Builder console,
plus the launcher foreground-console + Continue-button hardening.

**Confirmed working end-to-end through Rylee's actual sessions** (server logs
reviewed directly, not just tests): she loads /plan, sees her real progress on the
objective map, studies objectives, submits answers, is graded, and the
retry-with-missed-points flow works. The first-launch message was seen and the
Continue button (post-timeout-fix) correctly advances. The launcher console shows
startup status and stops the server on close. Live DB on `main` post-merge:
`welcome_message_seen='1'` (she dismissed it), **116/116 POB lessons** written,
suite **445/445** with 0 failures.

**Small known items — logged backlog, NOT active work:**
  - Jump-to-objective input: grey placeholder text can be mistaken for an
    already-entered value, causing an empty/invalid search ("Objective not found"
    when nothing was actually typed).
  - One observed instance of two browser windows from what felt like one click —
    not reproduced on clean runs; likely a leftover desktop-icon mixup from the
    .vbs→.lnk transition, not a script bug. Watch for recurrence only; do not invest
    further debugging unless it recurs.
  - `google.generativeai` deprecation warning (pre-existing, harmless; migrate to
    `google.genai` eventually).

**No further UI/launcher work should be started** unless one of the above recurs or
Rylee reports something new from actual use. Per PDR v3.2: the next legitimate input
is reality, not engineering review.
