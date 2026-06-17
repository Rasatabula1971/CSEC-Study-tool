# PHASE: build
"""
backend/ingest_solutions.py
===========================
Deterministic ingester for Paper 2 *worked-solution* files in a simple, hand-
authored **text** format -- the "answer half" of the app. Each solution file is a
mark scheme: every answer bullet is a markable point. This turns those files into
the rows the quiz page and the point-matching grader (backend/grade.py) need:

    documents   one row per solution file (content_type = "mark_scheme")
    chunks      one "stem" chunk per question (the question prose), keyed with a
                '-stem' chunk_id so the quiz page (GET /api/questions, /api/filters)
                actually finds it -- the '-stem' suffix is the filter those routes
                use to hide ingest.py's garbled MCQ chunks
    mark_points one row per answer bullet, keyed by question_id (== the stem
                chunk_id) and a real objective_id (grade.fetch_mark_points keys on
                question_id, the quiz page counts marks WHERE question_id = chunk_id)

Why a separate module from ingest.py:
  ingest.py does generic 500-char windowing and gives chunks auto-generated ids
  ("{doc_id}-p{page}-c{idx}") that the quiz page's '-stem' filter never matches,
  and writes mark_points with question_id = NULL -- ungradeable. This module
  parses a strict, stable layout exactly so every chunk is '-stem' keyed and every
  mark point is tied to its question and objective.

Hard rules honoured (CLAUDE.md):
  * Rule 1 -- a question with no confident objective match is queued for review and
    NOT stored. No chunk or mark point ever lacks a real objective_id.
  * Rule 2 -- parsing is pure Python/regex; the LLM is never asked to extract or
    score. Scoring stays in grade.compute_score.

File format (one file per past paper -- see ingest_solutions_templates/ for a
full example):

    SUBJECT: Principles_of_Business
    PAPER: 2
    SESSION: June
    YEAR: 2024

    QUESTION 1
    <question prose, any number of lines>
    ANSWER:
    - first mark-scheme point
    - second mark-scheme point

    QUESTION 2
    ...

  * Header is `KEY: value` lines before the first QUESTION. PAPER and YEAR are
    required; SESSION (e.g. June / January) and SUBJECT are optional.
  * `QUESTION <n>` starts a block. Lines up to `ANSWER:` (also accepts `MARKS:` /
    `MARK SCHEME:`) are the stem; the bullets after it are the mark points. Bullet
    markers `-`, `*`, ``, `1.`, `(a)` are all accepted (ingest.parse_mark_points).

Usage:
    python backend/ingest_solutions.py --subject Principles_of_Business \\
        --solutions-dir "D:\\CSEC\\POB\\paper2_solutions_txt"

    # Skip embedding (fully offline; grading + the quiz picker need no vectors):
    python backend/ingest_solutions.py --subject Principles_of_Business \\
        --solutions-dir "<dir>" --no-embed
"""

import argparse
import hashlib
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# backend/ on sys.path so the bare imports below resolve from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_embed  # noqa: E402
from ingest import (  # noqa: E402
    best_objective,
    parse_mark_points,
    load_objectives,
    assert_subject_locked,
    MIN_KEYWORD_OVERLAP,
)

# --- Line patterns ----------------------------------------------------------
HEADER_RE = re.compile(r"^\s*(?P<key>SUBJECT|PAPER|SESSION|YEAR)\s*:\s*(?P<val>.+?)\s*$", re.I)
QUESTION_RE = re.compile(r"^\s*QUESTION\s+(?P<num>\d+)\b", re.I)
# The line that separates the question stem from its mark-scheme bullets.
ANSWER_RE = re.compile(r"^\s*(ANSWER|MARKS|MARK\s+SCHEME)\s*:?\s*$", re.I)


# ---------------------------------------------------------------------------
# DB helpers (same pattern as ingest.py)
# ---------------------------------------------------------------------------
def open_db(db_path: str) -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("ERROR: sqlite-vec is not installed. Run: pip install sqlite-vec")
    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def serialize_vec(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _slug(s: str) -> str:
    """Strip everything but letters/digits -- for a chunk_id-safe paper token."""
    return re.sub(r"[^A-Za-z0-9]", "", s)


# ---------------------------------------------------------------------------
# Pure parsing  (no DB, no I/O -- unit-testable on plain strings)
# ---------------------------------------------------------------------------
def parse_header(text: str) -> dict | None:
    """Read the PAPER / YEAR / SESSION / SUBJECT header.

    Returns {subject, paper_num, session, year, paper_label, paper_short} or None
    when the required PAPER and a 4-digit YEAR are missing. `paper_label` matches
    the quiz dropdown format ("Paper 2 - June 2024"); `paper_short` is a compact,
    chunk_id-safe token ("June2024").
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if QUESTION_RE.match(line):
            break
        m = HEADER_RE.match(line)
        if m:
            fields[m.group("key").upper()] = m.group("val").strip()

    if "PAPER" not in fields or "YEAR" not in fields:
        return None
    ym = re.search(r"\d{4}", fields["YEAR"])
    pm = re.search(r"\d+", fields["PAPER"])
    if not ym or not pm:
        return None

    year = int(ym.group())
    paper_num = pm.group()
    session = fields.get("SESSION", "").strip()
    if session:
        paper_label = f"Paper {paper_num} - {session} {year}"
        paper_short = _slug(f"{session}{year}") or f"P{paper_num}{year}"
    else:
        paper_label = f"Paper {paper_num} - {year}"
        paper_short = f"P{paper_num}{year}"
    return {
        "subject": fields.get("SUBJECT", "").strip(),
        "paper_num": paper_num,
        "session": session,
        "year": year,
        "paper_label": paper_label,
        "paper_short": paper_short,
    }


def parse_questions(text: str) -> list[dict]:
    """Split the body into questions. Each: {num, stem, points}.

    Lines from a `QUESTION <n>` header up to the `ANSWER:` separator are the stem;
    bullets after it become `points` via ingest.parse_mark_points (so the bullet
    grammar matches the rest of the ingester). A question with no `ANSWER:` block,
    or no bullets under it, yields points == [].
    """
    questions: list[dict] = []
    cur: dict | None = None
    in_answer = False

    def close():
        nonlocal cur, in_answer
        if cur is None:
            return
        cur["stem"] = " ".join(s.strip() for s in cur.pop("_stem") if s.strip()).strip()
        cur["points"] = parse_mark_points("\n".join(cur.pop("_answer")))
        questions.append(cur)
        cur, in_answer = None, False

    for raw in text.splitlines():
        qm = QUESTION_RE.match(raw)
        if qm:
            close()
            cur = {"num": int(qm.group("num")), "_stem": [], "_answer": []}
            in_answer = False
            continue
        if cur is None:
            continue  # header / preamble before the first question
        if ANSWER_RE.match(raw):
            in_answer = True
            continue
        (cur["_answer"] if in_answer else cur["_stem"]).append(raw)

    close()
    return questions


# ---------------------------------------------------------------------------
# Counts / summary
# ---------------------------------------------------------------------------
def new_counts() -> dict:
    return {
        "files": 0,
        "questions": 0,
        "mark_points": 0,
        "queued_no_objective": 0,
        "queued_no_points": 0,
        "skipped_duplicate": 0,
    }


# ---------------------------------------------------------------------------
# Ingest one already-read solution document
# ---------------------------------------------------------------------------
def ingest_solution_text(db: sqlite3.Connection, *, text: str, subject_id: str,
                         source_file: str, objectives: list[dict], counts: dict,
                         embed_fn=None, min_overlap: int = MIN_KEYWORD_OVERLAP,
                         content_hash: str | None = None) -> dict | None:
    """Parse + store one solution document. Returns its header meta, or None.

    Writes a documents row, then per question a '-stem' chunk + bullet mark_points.
    A question whose stem+answer matches no objective, or that carries no bullets,
    is queued in ingest_review_queue and stores nothing else. Caller commits.
    """
    meta = parse_header(text)
    if meta is None:
        db.execute(
            "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) VALUES (?, ?, ?)",
            (source_file, text[:500], "no_header"),
        )
        return None

    chash = content_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc_id = f"sol-{chash[:12]}"
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
        "source_file, content_hash) VALUES (?, ?, 'mark_scheme', ?, ?, ?, ?)",
        (doc_id, subject_id, meta["paper_label"], meta["year"], source_file, chash),
    )
    counts["files"] += 1

    for q in parse_questions(text):
        match_text = f"{q['stem']} {' '.join(q['points'])}".strip()
        obj_id, _score = best_objective(match_text, objectives, min_overlap)
        if obj_id is None:
            db.execute(
                "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) VALUES (?, ?, ?)",
                (source_file, f"Q{q['num']}: {q['stem'][:280]}", "no_objective_match"),
            )
            counts["queued_no_objective"] += 1
            continue

        if not q["points"]:
            # Prose-only answer: no discrete bullets to grade against. Queue the
            # stem + answer-less context for manual handling; don't write a chunk
            # the quiz page would show with zero marks.
            db.execute(
                "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, "
                "objective_id, doc_id) VALUES (?, ?, ?, ?, ?)",
                (source_file, f"Q{q['num']}: {q['stem'][:280]}",
                 "no_mark_points", obj_id, doc_id),
            )
            counts["queued_no_points"] += 1
            continue

        # chunk_id is what the quiz page filters on (LIKE '%-stem') AND what its
        # mark-count subquery / grading key on (question_id == chunk_id).
        chunk_id = f"{obj_id}-{meta['paper_short']}-q{q['num']}-stem"
        _insert_stem_chunk(db, doc_id, subject_id, obj_id, q, chunk_id, embed_fn)
        counts["questions"] += 1
        for order, pt in enumerate(q["points"], 1):
            db.execute(
                "INSERT OR IGNORE INTO mark_points (mark_point_id, objective_id, "
                "question_id, doc_id, point_text, marks_value, point_order) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (f"{chunk_id}-mp{order}", obj_id, chunk_id, doc_id, pt, order),
            )
            counts["mark_points"] += 1

    return meta


def _insert_stem_chunk(db, doc_id, subject_id, obj_id, q, chunk_id, embed_fn):
    """One chunk holding the question stem, keyed so the quiz page can find it.

    question_num = the question number (so /api/questions' `question_num IS NOT NULL`
    filter passes). Embedding is skipped when embed_fn is None.
    """
    stem = q["stem"] or f"Question {q['num']}"
    cur = db.execute(
        "INSERT OR IGNORE INTO chunks (doc_id, objective_id, subject_id, chunk_text, "
        "page, question_num, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, obj_id, subject_id, stem, None, str(q["num"]), chunk_id),
    )
    if embed_fn is not None and cur.lastrowid:
        db.execute(
            "INSERT OR REPLACE INTO vec_mark_schemes(rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, serialize_vec(embed_fn(stem))),
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def already_ingested(db: sqlite3.Connection, content_hash: str) -> bool:
    return db.execute(
        "SELECT 1 FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone() is not None


def ensure_queue_columns(db: sqlite3.Connection) -> None:
    """Add objective_id / doc_id to ingest_review_queue if a pre-existing DB lacks
    them (SQLite has no ADD COLUMN IF NOT EXISTS). Safe to run repeatedly.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(ingest_review_queue)")}
    if "objective_id" not in cols:
        db.execute("ALTER TABLE ingest_review_queue ADD COLUMN objective_id TEXT")
    if "doc_id" not in cols:
        db.execute("ALTER TABLE ingest_review_queue ADD COLUMN doc_id TEXT REFERENCES documents(doc_id)")
    db.commit()


def ingest_subject(db: sqlite3.Connection, subject_id: str, solutions_dir: Path,
                   embed_fn=None) -> dict:
    """Ingest every *.txt in solutions_dir as a solution document for subject_id."""
    assert_subject_locked(db, subject_id)
    ensure_queue_columns(db)
    objectives = load_objectives(db, subject_id)
    if not objectives:
        sys.exit(f"ERROR: no objectives loaded for '{subject_id}'. Nothing to map to.")
    if not solutions_dir.is_dir():
        sys.exit(f"ERROR: solutions folder not found: {solutions_dir}")

    counts = new_counts()
    for path in sorted(solutions_dir.glob("*.txt")):
        chash = file_hash(path)
        if already_ingested(db, chash):
            counts["skipped_duplicate"] += 1
            continue
        text = path.read_text(encoding="utf-8")
        ingest_solution_text(
            db, text=text, subject_id=subject_id, source_file=str(path),
            objectives=objectives, counts=counts, embed_fn=embed_fn,
            content_hash=chash,
        )
        db.commit()
    return counts


def print_summary(counts: dict) -> None:
    print("\n" + "=" * 60)
    print("Solutions ingestion summary")
    print("=" * 60)
    print(f"  files processed          : {counts['files']}")
    print(f"  questions parsed         : {counts['questions']}")
    print(f"  mark points created      : {counts['mark_points']}")
    print(f"  queued (no objective)    : {counts['queued_no_objective']}")
    print(f"  queued (no mark points)  : {counts['queued_no_points']}")
    print(f"  files skipped (dup)      : {counts['skipped_duplicate']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Paper 2 solution text files into mark_points.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--solutions-dir", required=True,
                    help="folder of *.txt solution files (one per past paper)")
    ap.add_argument("--no-embed", action="store_true",
                    help="skip embedding stem chunks into vec_mark_schemes (fully offline)")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    embed_fn = None if args.no_embed else ollama_embed
    db = open_db(db_path)
    try:
        counts = ingest_subject(db, args.subject, Path(args.solutions_dir), embed_fn=embed_fn)
        print_summary(counts)
    finally:
        db.close()


if __name__ == "__main__":
    main()
