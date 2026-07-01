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
    point_order   INTEGER,
    point_group_id TEXT
);

CREATE TABLE IF NOT EXISTS study_sessions (
    session_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id   TEXT NOT NULL REFERENCES subjects(subject_id),
    objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
    mode         TEXT NOT NULL,
    outcome      TEXT,
    score_pct    INTEGER,
    is_retry     INTEGER DEFAULT 0,   -- 1 = a re-attempt, 0 = the first try
    created_at   TEXT DEFAULT (datetime('now'))
);

-- App-level singletons for a single-student, no-accounts offline app (UI overhaul
-- m017). Two keys: 'current_subject_id' (sticky subject) and 'welcome_message_seen'.
CREATE TABLE IF NOT EXISTS app_state (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT DEFAULT (datetime('now'))
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

-- sqlite-vec virtual tables (EMBED_DIM = 768 for nomic-embed-text)
-- rowid = chunks.id in every vec table
CREATE VIRTUAL TABLE IF NOT EXISTS vec_notes
    USING vec0(embedding float[768]);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_past_papers
    USING vec0(embedding float[768]);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_mark_schemes
    USING vec0(embedding float[768]);

CREATE TABLE IF NOT EXISTS objective_videos (
    video_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
    subject_id   TEXT NOT NULL REFERENCES subjects(subject_id),
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    channel      TEXT,
    duration_str TEXT,
    source_file  TEXT NOT NULL,
    added_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(objective_id, url)
);
