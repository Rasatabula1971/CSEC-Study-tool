-- m018_mcq_questions
-- =================================================================
-- ingest_v2 framework: imported multiple-choice question banks
-- (Kerwin Springer JSON for now, other sources later) + a per-chunk
-- source_family audit column.
--
-- NOTE ON NAMING: this table is DELIBERATELY *not* called
-- practice_questions. That name is already taken by a load-bearing
-- runtime table (controller.py reads/writes it for generated quiz +
-- synthesis question stems, created by migration m001). mcq_questions
-- is a semantically distinct concept -- externally-authored MCQs with
-- fixed options and a known correct answer -- so it gets its own table
-- and the existing practice_questions table is left untouched. This is
-- an additive, non-breaking migration.
--
-- Idempotency: CREATE ... IF NOT EXISTS makes the table/indexes safe to
-- re-run. The chunks.source_family ALTER has no IF NOT EXISTS in SQLite;
-- the migration runner (runner.py) tolerates a 'duplicate column name'
-- error per statement, so re-running after a partial apply is a no-op.
-- =================================================================

CREATE TABLE IF NOT EXISTS mcq_questions (
    mcq_id          TEXT PRIMARY KEY,        -- e.g. "ECON-eco21-001"
    objective_id    TEXT NOT NULL REFERENCES objectives(objective_id),
    subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
    source          TEXT NOT NULL,           -- "kerwin_springer" | "moe_slms" | etc.
    source_topic    TEXT,                    -- original topic string from the bank
    source_subtopic TEXT,
    difficulty      TEXT,                    -- "core" | "extended" | etc.
    stem            TEXT NOT NULL,
    options_json    TEXT NOT NULL,           -- JSON object {"A": "...", "B": "..."}
    correct_option  TEXT NOT NULL,           -- "A" | "B" | "C" | "D"
    explanation     TEXT,
    verified        INTEGER DEFAULT 0,       -- 0 until human-confirmed against syllabus
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_mcq_objective ON mcq_questions(objective_id);
CREATE INDEX IF NOT EXISTS idx_mcq_subject ON mcq_questions(subject_id);

-- Per-chunk provenance of which adapter family produced a chunk
-- ("caribbean_ai" | "moe_slms" | "generic_pdf" | "kerwin_pdf") for
-- audit/debug. Additive; existing rows get NULL.
ALTER TABLE chunks ADD COLUMN source_family TEXT;
