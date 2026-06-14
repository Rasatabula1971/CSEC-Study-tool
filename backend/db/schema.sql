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
    objective_id  TEXT PRIMARY KEY,
    section_id    TEXT NOT NULL REFERENCES syllabus_sections(section_id),
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_num TEXT NOT NULL,
    content_stmt  TEXT NOT NULL,
    skill_type    TEXT,
    command_words TEXT,
    exam_weight   TEXT,
    verified      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
    content_type  TEXT NOT NULL,
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
    mark_point_id TEXT PRIMARY KEY,
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
    mode         TEXT NOT NULL,
    outcome      TEXT,
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
    task_type    TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS ingest_review_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file  TEXT NOT NULL,
    chunk_text   TEXT NOT NULL,
    reason       TEXT,
    objective_id TEXT,                          -- known at queue time (prose rows)
    doc_id       TEXT REFERENCES documents(doc_id),
    created_at   TEXT DEFAULT (datetime('now'))
);

-- sqlite-vec virtual tables (EMBED_DIM = 768 for nomic-embed-text)
-- rowid = chunks.id in every vec table
CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes
    USING vec0(embedding float[768]);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_past_papers
    USING vec0(embedding float[768]);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_mark_schemes
    USING vec0(embedding float[768]);
