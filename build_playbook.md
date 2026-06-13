# CSEC AI Study Partner — Build Playbook

Stage-by-stage guide for building the project with Claude Code.
Each stage tells you:
- what to do **manually** before running Claude Code
- the **exact prompt** to give Claude Code
- what to **verify** once it's done

---

## Before You Start: Install Claude Code (one-time)

Claude Code is a CLI tool that reads CLAUDE.md, writes and runs code, and takes full tasks
rather than single answers. It requires a **Claude Pro subscription ($20/month)**.

**Install — run in Windows PowerShell 64-bit, NOT Command Prompt:**
```powershell
irm https://claude.ai/install.ps1 | iex
```

Close and reopen PowerShell, then verify:
```powershell
claude --version
claude doctor
```

If `claude` is not found after reinstalling, add its directory to your PATH:
```powershell
[Environment]::SetEnvironmentVariable("PATH", "$env:PATH;$env:USERPROFILE\.local\bin", [EnvironmentVariableTarget]::User)
```

**Authenticate:** Run `claude` from any folder. Your browser will open for login.

---

## Stage 0 — Repo Initialisation (manual, ~10 minutes)

Do this once before any Claude Code stage.

1. Create the repo folder:
   ```powershell
   mkdir C:\csec-study-partner
   cd C:\csec-study-partner
   git init
   ```

2. Copy `CLAUDE.md` into `C:\csec-study-partner\CLAUDE.md`.

3. Create `.gitignore`:
   ```
   .env
   __pycache__/
   *.pyc
   .pytest_cache/
   ```

4. Create `.env.example` (fill in your actual SSD drive letter):
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

5. Copy `.env.example` to `.env` and update `D:\` to your actual SSD drive letter.

6. Confirm Python 3.11+: `python --version`

7. Run `claude` from inside `C:\csec-study-partner`. You're ready.

---

## Stage 1 — Storage & Schema

**Goal:** Create the SSD folder structure, the SQLite schema (all tables + sqlite-vec
virtual tables), a Python init script, and a backup batch file.

### Manual first

Plug in the external SSD. Confirm its drive letter in File Explorer.
Update `SSD_ROOT` in `.env` if the letter differs from `D:`.

### Claude Code prompt

```
Read CLAUDE.md fully.

Stage 1 goal: create the SSD folder structure, database schema, and initialisation script.

Do these tasks in order:

1. Create backend/db/schema.sql using the exact CREATE TABLE and
   CREATE VIRTUAL TABLE statements from the CLAUDE.md "Database Schema" section.
   Include PRAGMA foreign_keys = ON at the top.

2. Create backend/db/init_db.py that:
   - Reads SSD_ROOT and DB_PATH from .env using python-dotenv.
   - Checks that the SSD path exists; exits with a clear error message if it does not.
   - Creates all seven subject folders under 03_KNOWLEDGE_BASE, each with
     subfolders 00_SYLLABUS through 05_STUDENT_WORK.
   - Creates 02_DATABASE, 04_REPORTS, and 07_BACKUPS folders.
   - Initialises csec.sqlite by running schema.sql using the sqlite-vec API
     pattern from CLAUDE.md (open_db function, enable_load_extension, etc.).
   - Prints a success summary listing every folder created and the DB path.

3. Create launch/backup.bat that copies csec.sqlite to
   07_BACKUPS\csec_backup_{date}.sqlite using Windows xcopy.

4. Create requirements.txt using the full dependency list from CLAUDE.md.

5. Create tests/test_schema.py that:
   - Opens the database, loads sqlite-vec, enables foreign keys.
   - Asserts all nine regular tables exist.
   - Asserts all three virtual vec tables exist.
   - Inserts a dummy subject row and asserts it reads back correctly.

After writing all files, run:
  python backend/db/init_db.py
  pytest tests/test_schema.py -v

Fix any errors before finishing. Both commands must succeed cleanly.
```

### Verify

- `D:\CSEC_AI_STUDY_PARTNER\` exists with all seven subject subfolders.
- `D:\CSEC_AI_STUDY_PARTNER\02_DATABASE\csec.sqlite` exists.
- `pytest tests/test_schema.py -v` passes all assertions.
- Run `launch\backup.bat` — a dated `.sqlite` file appears in `07_BACKUPS\`.
- Mark Stage 1 complete in CLAUDE.md Stage Tracker.

---

## Stage 2 — Syllabus Lock

**Goal:** Parse the POB syllabus CSV into the database, export it to Excel for human
sign-off, and lock it after approval.

### Manual first

Get the official CXC Principles of Business syllabus (PDF or structured document).
Place it at:
```
D:\CSEC_AI_STUDY_PARTNER\03_KNOWLEDGE_BASE\Principles_of_Business\00_SYLLABUS\
```

Prepare a CSV with these exact columns:
```
section_id, section_num, section_title, objective_id, objective_num,
content_stmt, skill_type, command_words, exam_weight
```
Copy the objectives from the CXC syllabus PDF into the CSV manually.
Name the file `pob_syllabus_raw.csv` in the same folder.

**Why manual CSV?** OCR of a CXC PDF is not reliable enough for the source of truth.
You compare the CSV against the PDF yourself before locking.

### Claude Code prompt

```
Read CLAUDE.md fully.

Stage 2 goal: load the POB syllabus CSV into the database and produce an Excel
review file for sign-off.

1. Create backend/db/syllabus_parser.py that:
   - Accepts --subject (e.g. Principles_of_Business) and --csv-file path as CLI args.
   - Reads the CSV with columns: section_id, section_num, section_title, objective_id,
     objective_num, content_stmt, skill_type, command_words, exam_weight.
   - Inserts the subject row into subjects (syllabus_locked = 0) if it does not exist.
   - Inserts all syllabus_sections rows.
   - Inserts all objectives rows.
   - Uses INSERT OR IGNORE so the script is safe to re-run.
   - Prints a count of sections and objectives inserted.

2. Create backend/db/export_for_review.py that:
   - Accepts --subject as a CLI arg.
   - Queries all objectives for that subject joined to their section.
   - Exports to an Excel file at 04_REPORTS\{subject}_syllabus_review.xlsx
     with columns: objective_id, section, objective_num, content_stmt,
     skill_type, command_words, exam_weight, verified.
   - Formats the header row in bold. Freezes the top row.
   - Prints the output path when done.

3. Create backend/db/lock_subject.py that:
   - Accepts --subject as a CLI arg.
   - Prints the count of objectives for that subject and asks for confirmation (y/n).
   - If confirmed: sets subjects.syllabus_locked = 1 and objectives.verified = 1
     for all objectives in that subject.
   - Prints a confirmation message.

4. Create tests/test_syllabus.py that:
   - Creates an in-memory SQLite database with the full schema.
   - Inserts a minimal mock syllabus: 1 subject, 2 sections, 4 objectives.
   - Asserts objective_id FK constraints are enforced.
   - Asserts that the lock_subject logic correctly flips syllabus_locked to 1.

Run pytest tests/test_syllabus.py -v. Fix errors before finishing.
```

### Verify

Run manually after Claude Code finishes:
```powershell
python backend/db/syllabus_parser.py --subject Principles_of_Business --csv-file "D:\...\pob_syllabus_raw.csv"
python backend/db/export_for_review.py --subject Principles_of_Business
```

Open the Excel file in `04_REPORTS\`. Check every objective against the CXC PDF.
Correct any errors in the CSV and re-run the parser until it matches exactly.

Once satisfied:
```powershell
python backend/db/lock_subject.py --subject Principles_of_Business
```

Mark Stage 2 complete in CLAUDE.md Stage Tracker.

---

## Stage 3 — Minimal Engine

**Goal:** Build the Ollama client wrapper and a RAM budget verification script.
Confirm the system stays under 8 GB peak RSS.

### Manual first — all manual (Claude Code cannot install Ollama)

1. Download and install Ollama from https://ollama.com/download (Windows installer).

2. Set the `OLLAMA_MODELS` environment variable to the SSD so models do not fill C:
   ```powershell
   setx OLLAMA_MODELS "D:\CSEC_AI_STUDY_PARTNER\01_MODELS\Ollama"
   ```
   Reboot or restart the Ollama service after setting this.

3. Pull both models:
   ```powershell
   ollama pull llama3.2:3b
   ollama pull nomic-embed-text
   ```

4. Confirm Ollama is running:
   ```powershell
   curl http://localhost:11434/api/tags
   ```

### Claude Code prompt

```
Read CLAUDE.md fully.

Stage 3 goal: build the Ollama client wrapper and RAM measurement script.

1. Create backend/ollama_client.py implementing the exact functions from
   the CLAUDE.md "Ollama API" section:
   - ollama_embed(text) — posts to /api/embeddings with keep_alive=0, returns list[float]
   - ollama_chat(messages, system, schema=None) — posts to /api/chat, returns str
   - ollama_health() — returns True if Ollama is reachable, False otherwise
   Use httpx throughout. Never import or use the Ollama Python SDK.

2. Create backend/ram_check.py that:
   - Uses psutil to measure the current Python process RSS in MB.
   - Calls ollama_embed("test sentence") and measures RSS after.
   - Calls ollama_embed("test sentence") a second time (warm repeat).
   - Calls ollama_chat with a simple "Hello" message and measures RSS after.
   - Calls ollama_embed again and measures RSS (confirms embedding model evicts).
   - Prints a table: step | RSS MB | delta MB.
   - Prints a final line: PASS if peak total RSS is under 7500 MB, WARN otherwise.
   Add psutil to requirements.txt if it is not already there.

3. Create tests/test_ollama_client.py that:
   - Mocks httpx.post to return canned responses.
   - Tests that ollama_embed returns a list of floats.
   - Tests that ollama_chat returns a string.
   - Tests that ollama_chat with a schema passes format in the payload.
   - Tests that ollama_health returns True on 200 and False on connection error.

Run pytest tests/test_ollama_client.py -v. Fix errors before finishing.
```

### Verify

With Ollama running:
```powershell
python backend/ram_check.py
```

The script must print **PASS**, not WARN.
If it prints WARN, change `MODEL_CHAT` in `.env` to a smaller quantized model
(e.g. `llama3.2:3b-instruct-q4_K_M`) and re-run until it passes.

Mark Stage 3 complete in CLAUDE.md Stage Tracker.

---

## Stage 4 — Ingestion Pipeline

**Goal:** Build the script that reads PDFs, chunks them, embeds each chunk,
validates the FK to an `objective_id`, and indexes into the vec_* tables.

### Manual first

Place at least the POB specimen paper and one mark scheme in their SSD folders
(`01_SPECIMEN_PAPERS` and `03_MARK_SCHEMES`). These will be the first real documents
the ingestion pipeline processes.

### Claude Code prompt

```
Read CLAUDE.md fully.

Stage 4 goal: build the ingestion pipeline.

Create backend/ingest.py that:

1. Takes --subject as a CLI arg. Checks that the subject exists in the DB
   with syllabus_locked = 1. Exits with a clear error if not locked.

2. Walks the knowledge base folder for that subject across all document type
   subfolders: 01_SPECIMEN_PAPERS, 02_PAST_PAPERS, 03_MARK_SCHEMES, 04_NOTES.
   Skips files already ingested by checking content_hash in the documents table.

3. For each new PDF, uses PyMuPDF (import fitz) to extract text page by page.
   Chunks each page into ~500-character overlapping segments (overlap = 100 chars).

4. For each chunk, applies a keyword-overlap heuristic to match it to an
   objective_id: search objectives.content_stmt for shared words with the chunk.
   If no confident match is found (overlap score below threshold), write the chunk
   to ingest_review_queue with reason "no_objective_match" and skip indexing.
   If matched, write to the chunks table and index the embedding.
   Every indexed chunk must have a real FK. No unmapped chunk is indexed silently.

5. For mark scheme PDFs specifically, also parses individual mark points.
   Assumes format: each award point starts on a new line with a bullet or number.
   Writes each point to mark_points linked to the chunk's objective_id.
   If the structure cannot be parsed, queues the chunk for review instead.

6. Embeds each chunk using ollama_embed (keep_alive=0 on every call).
   Routes to the correct vec table based on content_type:
   notes → vec_notes | past_paper → vec_past_papers | mark_scheme → vec_mark_schemes.

7. Prints a summary at the end:
   files processed | chunks indexed | mark points extracted |
   chunks queued for review | chunks skipped (duplicate hash).

Also add a --review-queue flag to ingest.py that lists queued chunks and
interactively prompts the user to assign an objective_id to each one.

Create tests/test_ingest.py that:
   - Creates a temporary SQLite DB with the full schema and sqlite-vec loaded.
   - Creates a dummy locked subject and objective in the DB.
   - Calls the ingest logic on a small test text string (not a real PDF).
   - Asserts a chunk row appears in the chunks table.
   - Asserts the chunk's rowid appears in the correct vec table.
   - Asserts a chunk with no objective match goes to ingest_review_queue.

Run pytest tests/test_ingest.py -v. Fix errors before finishing.
```

### Verify

```powershell
python backend/ingest.py --subject Principles_of_Business
```

Check the summary. Then inspect any queued chunks:
```powershell
python backend/ingest.py --review-queue
```

Assign objective IDs to queued chunks before moving on.
Re-run `pytest tests/ -v` — all previous tests must still pass.
Mark Stage 4 complete in CLAUDE.md Stage Tracker.

---

## Stage 5 — Deterministic Core

**Goal:** Build all deterministic logic modules, the controller, and the four agent
prompt files. This stage is the reliability backbone of the system.

### Manual first

Nothing. This stage is pure code.

### Claude Code prompt

```
Read CLAUDE.md fully, especially the "Deterministic vs LLM" table,
the "Grading Contract", and the "Leitner Scheduler" sections.

Stage 5 goal: build scope.py, retrieval.py, grade.py, schedule.py, weakness.py,
controller.py, the four prompt files, and a full test suite.

1. Create backend/scope.py implementing is_in_scope() exactly as in CLAUDE.md.
   Add a second function: get_objective(db, objective_id) that returns the full
   objectives row or None.

2. Create backend/retrieval.py with get_context(db, request: dict) → dict:
   - If request contains all of (subject_id, paper, year, question_num):
     structured lookup via chunks WHERE on those fields (no embedding call).
   - Otherwise: semantic fallback — embed request["query"], search the correct
     vec table filtered by subject_id, join back to chunks.
   - Always return: objective_id, chunk_text, source_file, page
     (or None if nothing found).

3. Create backend/grade.py with grade_answer(db, question_id, student_answer, messages) → dict:
   - Fetches all mark_points for the question_id from DB.
   - If no mark_points found: returns {"error": "no_mark_scheme"}.
   - Loads prompts/examiner.txt as the system prompt.
   - Calls ollama_chat with GRADING_SCHEMA as the format constraint.
   - Parses the JSON response with json.loads.
   - Calls compute_score() from CLAUDE.md on the result.
   - Returns the full grading result including score_pct, awarded, total, missed_points.

4. Create backend/schedule.py implementing update_leitner() exactly as in CLAUDE.md.
   Add get_due_objectives(db, subject_id) → list that queries weakness_log WHERE
   next_review <= date.today().isoformat() AND subject_id = ?
   ORDER BY leitner_box ASC.

5. Create backend/weakness.py with log_weakness(db, grading_result: dict, session_id: int):
   - Uses Pydantic to validate the incoming grading_result has the required fields.
   - Upserts into weakness_log: if objective_id already exists, updates score_pct,
     reason, and calls update_leitner with the new score.
     If new, inserts with box=1 and next_review=today.
   - Raises ValueError (never silently fails) if Pydantic validation fails.

6. Create backend/controller.py with handle_request(db, request: dict) → dict:
   Wires the modules together:
   - Route "teach":  scope check → retrieval → LLM (tutor prompt) → return lesson + question.
   - Route "grade":  scope check → retrieval → grade_answer → log_weakness → return result.
   - Route "plan":   get_due_objectives → build revision plan → return plan.
   If scope check fails: return {"error": "out_of_scope"} immediately — no LLM call.

7. Create the four prompt files in prompts/:

   archivist.txt — system prompt for scope and objective lookup.
     Must: identify the exact subject and objective_id, confirm scope, output JSON.

   tutor.txt — system prompt for one-objective lessons.
     Use clear, simple language. End with exactly one targeted active-recall question.
     Never answer the question it poses.

   examiner.txt — system prompt for point-matching grading.
     Receives the student answer and each mark point as a list.
     Outputs one boolean per point plus evidence text. Nothing else.

   planner.txt — system prompt for revision plans.
     Takes a list of weak objectives ordered by Leitner box and due date.

8. Create tests/test_core.py with tests for:
   - scope.py: in-scope objective returns True; unlocked subject returns False;
     unknown objective_id returns False.
   - grade.py: mock ollama_chat to return a valid grading JSON;
     assert compute_score returns correct pct for 2 of 3 points.
   - schedule.py: update_leitner(3, 75) returns (4, future_date);
     update_leitner(3, 40) returns (1, tomorrow).
   - weakness.py: valid input writes to DB; invalid input raises ValueError.
   - retrieval.py: structured lookup is called when all four keys are present;
     semantic search is called when they are not.

   Mock all DB and Ollama calls. Tests must not require Ollama to be running.

Run pytest tests/test_core.py -v. All tests must pass before finishing.
```

### Verify

```powershell
pytest tests/ -v
```

All tests pass (including previous stages). Then run a manual smoke test:
```powershell
python -c "
from backend.db.init_db import open_db
from backend.controller import handle_request
db = open_db()
result = handle_request(db, {
    'route': 'teach',
    'subject_id': 'Principles_of_Business',
    'query': 'nature of business'
})
print(result)
"
```

You should get a lesson with a question at the end — not an error.
Mark Stage 5 complete in CLAUDE.md Stage Tracker.

---

## Stage 6 — FastAPI + UI + Launcher

**Goal:** Wrap the controller in a FastAPI app with JSON endpoints, serve a lightweight
HTML chat page directly from FastAPI, and build the `start.bat` launcher.

### Manual first

Nothing.

### Claude Code prompt

```
Read CLAUDE.md fully.

Stage 6 goal: build the FastAPI app, a minimal browser-based chat UI, and the launcher.

1. Create backend/app.py:
   - FastAPI app with a lifespan context manager that:
     a. Checks the SSD is mounted; calls sys.exit with a clear message if not.
     b. Calls ollama_health(). If False, logs a warning but continues.
     c. Opens the DB connection and stores it in app.state.db.
   - POST /api/chat — accepts {"message": str, "subject_id": str, "route": str}.
     Calls controller.handle_request. Returns the result as JSON.
   - GET /api/subjects — returns a list of locked subjects from the DB.
   - GET /api/due/{subject_id} — returns due objectives from weakness_log.
   - GET /health — returns {"status": "ok", "ollama": bool, "db": bool}.
   - GET / — serves backend/static/chat.html using FileResponse.

2. Create backend/static/chat.html — a minimal single-page chat interface:
   - Input box for the student's message.
   - Dropdown to select subject (populated from GET /api/subjects on page load).
   - Radio buttons for route: Teach / Quiz+Grade / Revision Plan.
   - A scrollable chat history div.
   - Sends POST /api/chat and renders the response in the chat div.
   - No build tools. No npm. No React. Plain HTML + vanilla JS + inline CSS only.
   - Clean and readable in any modern browser.

3. Create launch/start.bat with exactly this content:
   @echo off
   echo Checking SSD...
   if not exist "%SSD_ROOT%" (
       echo ERROR: SSD not mounted at %SSD_ROOT%. Plug in the drive and retry.
       pause
       exit /b 1
   )
   echo Starting Ollama...
   start "" ollama serve
   timeout /t 3 /nobreak >nul
   curl -s http://localhost:11434/api/tags >nul 2>&1
   if errorlevel 1 (
       echo ERROR: Ollama did not start. Check Ollama installation.
       pause
       exit /b 1
   )
   echo Starting FastAPI...
   cd /d "%~dp0.."
   start "" python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
   timeout /t 2 /nobreak >nul
   curl -s http://127.0.0.1:8000/health >nul 2>&1
   if errorlevel 1 (
       echo ERROR: FastAPI did not start. Check the terminal for errors.
       pause
       exit /b 1
   )
   echo Study system ready. Opening browser...
   start http://127.0.0.1:8000
   Add a comment at the top: "Run this from the repo root. Set SSD_ROOT in .env first."

   Gating policy (do not change):
   - Ollama reachability is a HARD gate. The curl on /api/tags (== ollama_client.ollama_health())
     must keep its `exit /b 1`. Ollama down is a real blocker and must stop startup.
   - ram_check.py is ADVISORY ONLY. It always exits 0 and must NOT gate startup. If you run it
     in start.bat, run it for its printed WARN/PASS output and ignore its exit code — the real
     RAM test is whether a session runs without freezing, not a snapshot at launch.

4. Create tests/test_api.py using FastAPI's TestClient:
   - Mock the DB and controller.
   - Test GET /health returns 200.
   - Test POST /api/chat with valid payload returns a JSON response.
   - Test POST /api/chat with missing subject_id returns 422.

Run pytest tests/test_api.py -v. Fix errors before finishing.
```

### Verify

Start the system in dev mode first (not via start.bat):
```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
```

Open `http://127.0.0.1:8000` in a browser.
- Select Principles of Business from the dropdown.
- Type a topic (e.g. "Explain the nature of a business").
- Send it. You should get a lesson with a question in the chat window.

Then test the launcher:
```powershell
cd C:\csec-study-partner
launch\start.bat
```

Mark Stage 6 complete in CLAUDE.md Stage Tracker.

---

## Stage 7 — Pilot (Principles of Business)

**Goal:** Run a full end-to-end integration test suite and a manual study session
on Principles of Business. Nothing ships to other subjects until this passes.

### Manual first

Confirm all of the following are true before running Claude Code:
- POB syllabus is locked (Stage 2 ✓)
- At least one past-paper mark scheme is ingested (Stage 4 ✓)
- The system starts and the UI loads (Stage 6 ✓)

### Claude Code prompt

```
Read CLAUDE.md fully.

Stage 7 goal: build a POB integration test suite that covers the full study loop.

Create tests/test_pilot_pob.py with an integration test class that:

1. Uses a real (test copy) SQLite database with the full schema, pre-populated with:
   - Principles_of_Business subject with syllabus_locked = 1.
   - At least 3 objectives from the Nature of Business section.
   - At least 5 mark_points across those objectives.
   - At least 2 chunks in vec_notes and 2 in vec_mark_schemes.

2. Tests the full teach loop:
   - Request route="teach" for a known objective_id.
   - Assert response contains lesson text and exactly one question.

3. Tests the grading loop:
   - Simulate a student answer; mock ollama_chat to return a valid grading JSON.
   - Assert score is computed correctly in Python (not by the LLM).
   - Assert weakness_log is updated with the correct leitner_box.

4. Tests the scope gate:
   - Request a topic for a subject that does not exist in the DB.
   - Assert response is {"error": "out_of_scope"}, not a lesson.

5. Tests the revision plan:
   - Pre-populate weakness_log with 3 objectives due today (mix of box 1 and box 2).
   - Request route="plan".
   - Assert the returned plan lists at least those 3 objectives ordered by box.

6. Tests traceability (VAL-08):
   - Make a question-based request that triggers the structured lookup path.
   - Assert the response includes objective_id, source_file, and page.

All mocks must clearly label what is being mocked.
Ollama must be mocked in all tests — no live Ollama required to run the suite.

Run pytest tests/test_pilot_pob.py -v. All 6 test groups must pass.
```

### Verify — manual study session

Run the full system. Spend 20–30 minutes actually studying with it:

1. Ask for lessons on three different POB topics.
2. Answer the questions it gives you — get some right, some wrong deliberately.
3. Check weakness_log updated:
   ```powershell
   python -c "
   from backend.db.init_db import open_db
   db = open_db()
   [print(dict(r)) for r in db.execute('SELECT * FROM weakness_log').fetchall()]
   "
   ```
4. Request a revision plan. Confirm it surfaces topics you got wrong.
5. Ask about a non-CSEC topic. Confirm it refuses politely.

When the session behaves as expected, mark Stage 7 complete in CLAUDE.md Stage Tracker.

---

## Stage 8 — Subject Rollout

Repeat Stage 2 (syllabus parsing + lock) and Stage 4 (ingestion) for each remaining
subject, one at a time. Each subject must be locked before it can be studied.

**Recommended order:**
1. Economics
2. Principles_of_Accounts
3. Mathematics
4. Integrated_Science
5. Information_Technology
6. English

### Claude Code prompt (run once per subject)

```
Read CLAUDE.md fully.

Rollout task: add [SUBJECT_NAME] to the system.

1. Run syllabus_parser.py for [SUBJECT_NAME].
   CSV path: [paste the full path to your CSV for this subject]

2. Run export_for_review.py for [SUBJECT_NAME].
   I will manually review the Excel file and confirm before locking.

3. Once I confirm approval, run lock_subject.py for [SUBJECT_NAME].

4. Run ingest.py --subject [SUBJECT_NAME].

5. Run the full test suite: pytest tests/ -v.
   All tests must pass before this subject is considered done.

6. Update the CLAUDE.md Stage Tracker to reflect current progress.
```

---

## Stage 9 — Optional: Open WebUI / CrewAI

**Do not start Stage 9 until Stages 1–8 are complete and Stage 7 manual
validation has fully passed.**

**Open WebUI (v3.1):** Add as a second front-end option. The FastAPI backend stays
unchanged. Configure Open WebUI to point at `http://localhost:8000/api/chat`.

**CrewAI (v3.2):** Wrap the proven Python workflow functions (scope.py, retrieval.py,
grade.py, schedule.py) as CrewAI tools. The deterministic logic stays in Python —
CrewAI manages only the orchestration flow between agents. Never move scoring,
scheduling, or scope checks into CrewAI tool bodies.

---

## Prompting Tips for Claude Code

**Re-read CLAUDE.md at the start of every stage.**
Start each session with `/clear` so Claude Code re-reads CLAUDE.md fresh without
accumulated context from the previous stage.

**One stage at a time.**
Never combine two stages in one prompt. Claude Code works better with a bounded
task that has clear test conditions.

**Always end with a test run.**
Every prompt above ends with a `pytest` command. If Claude Code finishes but tests
fail, do not move to the next stage. Ask it to fix the failures in the same session.

**Give it the full schema on complex tasks.**
CLAUDE.md already covers this, but if Claude Code seems to be guessing at column
names, paste the relevant schema section directly into the prompt.

**Question every new dependency.**
The dependency list in CLAUDE.md is intentional. If Claude Code wants to add a
package not in that list, ask why before approving it.

**If a stage produces passing tests but wrong behaviour:**
Run the manual verification steps before marking the stage complete. Tests prove
the code runs; verification proves the system works.
