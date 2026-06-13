"""
backend/ingest_solutions.py
===========================
Deterministic ingester for CSEC Paper 2 *worked-solution* PDFs -- the "answer
half" of the app. Each solution document is, in effect, a mark scheme: every
answer bullet is a markable point. This module turns those PDFs into the rows
the point-matching grader (backend/grade.py) needs:

    documents   one row per solution PDF (content_type = "mark_scheme")
    chunks      one "stem" chunk per sub-question (the question prose), so the
                question picker and structured retrieval can show/trace it
    mark_points one row per answer bullet, keyed by a unique question_id AND a
                real objective_id  (grade.py fetches WHERE question_id = ?)

Why a separate module from ingest.py:
  ingest.py does generic 500-char windowing and writes mark_points with
  question_id = NULL -- the grader can never find those. These solution PDFs
  have a strict, stable structure (Question N -> N(x) Title -> "(K marks)" ->
  bullets) that we parse exactly so every mark point is tied to its question.

Hard rules honoured (CLAUDE.md):
  * Rule 1 -- a sub-question with no confident objective match is queued for
    review and NOT stored. No mark point ever lacks a real objective_id.
  * Rule 2 -- parsing is pure Python/regex; the LLM is never asked to extract
    or score. Scoring stays in grade.compute_score.
  * Embeddings are OPTIONAL here (default off): grading and the question picker
    need no vectors, so this runs fully offline without Ollama. Pass --embed
    (or embed_fn=) to also index stem chunks into vec_mark_schemes once Ollama
    is up.

Structure of a solution PDF (verified across June 2010 -> January 2026):
    "January 2026 Paper 02"          <- session header (or "May/June 2010 ...")
    "Question 1"
    "1(a) Maria's career choices"    <- sub-question header  N(letter)
    "...question prose... (3 marks)"  <- stem ends at the (K marks) token
    "Three careers ... are:"          <- answer lead-in (not a bullet)
    "•"                          <- bullet glyph alone on its line
    "Accountant - a person who ..."   <- the point text (may span lines)
    "•"
    ...

Usage:
    # Offline (no embeddings) -- default source is the KB mark-schemes folder.
    python backend/ingest_solutions.py --subject Principles_of_Business

    # Point at the raw download folder instead.
    python backend/ingest_solutions.py --subject Principles_of_Business \\
        --src "D:\\CSEC\\CSEC_Materials\\POB\\downloads\\pdfs"

    # Also embed stem chunks into vec_mark_schemes (needs Ollama running).
    python backend/ingest_solutions.py --subject Principles_of_Business --embed
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
from ingest import best_objective, MIN_KEYWORD_OVERLAP  # noqa: E402

# --- Short subject -> id prefix (matches the objective_id prefixes loaded in
#     Stage 2). Falls back to the matched objective's own prefix at runtime. ---
SUBJECT_CODE = {
    "Principles_of_Business": "POB",
    "Economics": "ECON",
    "Mathematics": "MATH",
    "English": "ENG",
    "Principles_of_Accounts": "POA",
    "Integrated_Science": "ISCI",
    "Information_Technology": "IT",
}

# --- Line patterns ----------------------------------------------------------
# Session header: "January 2026 Paper 02" / "May/June 2010 Paper 02".
HEADER_RE = re.compile(r"\b(?P<mon>May/June|June|May|January)\s+(?P<year>\d{4})\s+Paper\s+0?2\b", re.I)
# Sub-question header. Covers every form seen across June 2010 -> January 2026:
#   "1(a) Title"  "1(a)(i) Title"  "(a)(i) Title"  "(a) Title"
# The leading question number is optional (nested forms inherit the current
# "Question N"); the roman part is optional. A title after the marker is required
# so prose lines like "Three stakeholders ... are:" never match.
SUBPART_RE = re.compile(
    r"^(?P<q>\d+)?\(\s*(?P<sub>[a-z])\s*\)(?:\(\s*(?P<roman>[ivx]+)\s*\))?\s+(?P<title>.+\S)\s*$"
)
QUESTION_RE = re.compile(r"^Question\s+\d+\b", re.I)
SECTION_RE = re.compile(r"^Section\s+[IVX]+\b", re.I)
MARKS_RE = re.compile(r"\((?P<n>\d+)\s*marks?\)", re.I)
BULLET = "•"  # the bullet glyph these PDFs use, alone on its own line

MONTH_TAG = {"january": "Jan", "june": "Jun", "may/june": "Jun", "may": "Jun"}


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


# ---------------------------------------------------------------------------
# Pure parsing  (no DB, no PyMuPDF -- unit-testable on plain text)
# ---------------------------------------------------------------------------
def parse_session(text: str) -> dict | None:
    """Find the session header. Returns {month_tag, year, paper_label} or None."""
    m = HEADER_RE.search(text)
    if not m:
        return None
    mon = m.group("mon").lower()
    tag = MONTH_TAG.get(mon, mon[:3].title())
    label = "January" if tag == "Jan" else "June"
    return {"month_tag": tag, "year": int(m.group("year")), "paper_label": f"Paper 2 - {label}"}


def split_points(body_lines: list[str]) -> list[str]:
    """Split an answer body into individual mark points on the bullet glyph.

    Text before the first bullet (the "Three ... are:" lead-in) is dropped.
    Each bullet's text may span several lines; they are joined and whitespace
    is normalised. Lines that are themselves only a bullet are separators.
    """
    points: list[str] = []
    current: list[str] | None = None
    for raw in body_lines:
        line = raw.strip()
        if not line:
            continue
        if line == BULLET:
            if current is not None:
                points.append(" ".join(current).strip())
            current = []
            continue
        if line.startswith(BULLET):  # "• text" on one line
            if current is not None:
                points.append(" ".join(current).strip())
            current = [line[len(BULLET):].strip()]
            continue
        if current is not None:
            current.append(line)
    if current is not None:
        points.append(" ".join(current).strip())
    return [p for p in points if p]


def _is_bullet(line: str) -> bool:
    return line == BULLET or line.startswith(BULLET)


def parse_subquestions(lines: list[tuple[int, str]]) -> list[dict]:
    """Parse (page, line) pairs into a list of top-level sub-questions.

    Each result: {question_num, sub, label, page, stem, points, marks}.

    The gradeable unit is the top-level "N(x)"; nested "N(x)(i)" / "N(x)(ii)"
    parts are folded into the same sub-question (their bullets accumulate), so a
    question that jumps straight to "1(a)(i)" is still captured and the merged
    answer text maps reliably to an objective.

    The answer body is anchored on the FIRST bullet (not on the "(K marks)"
    token) so bullets are captured even when a sub-question states no mark total:
      stem   = header + prose before the first bullet, trimmed at the "(K marks)"
               line when present (drops the "... are:" answer lead-in) so the
               stem is clean question text for the picker.
      points = bullet-split everything from the first bullet onward.
      marks  = the stated total if present in the stem, else len(points).
    A sub-question with no bullets returns points == [] (prose answer).
    """
    subs: list[dict] = []
    cur: dict | None = None
    current_q = 0  # last "Question N" / N(x) seen; nested "(a)(i)" forms inherit it

    def close():
        nonlocal cur
        if cur is None:
            return
        seg = cur.pop("_seg")
        header = cur.pop("_header")
        cur.pop("_key", None)
        idx = next((i for i, l in enumerate(seg) if _is_bullet(l)), None)
        if idx is None:
            stem_lines, points = seg, []
        else:
            stem_lines, points = seg[:idx], split_points(seg[idx:])

        # Split the stem region at the "(K marks)" line: everything up to and
        # including it is the question stem; everything after is the prose answer
        # body (only meaningful for no-bullet questions, where it would otherwise
        # be discarded -- bullet questions carry their answer in `points`).
        stem_kept: list[str] = [header]
        answer_lines: list[str] = []
        marks = 0
        mk = MARKS_RE.search(header)
        if mk:
            marks = int(mk.group("n"))
            answer_lines = list(stem_lines)
        else:
            seen_marks = False
            for l in stem_lines:
                if not seen_marks:
                    stem_kept.append(l)
                    mk = MARKS_RE.search(l)
                    if mk:
                        marks = int(mk.group("n"))
                        seen_marks = True
                else:
                    answer_lines.append(l)
        cur["stem"] = " ".join(s.strip() for s in stem_kept).strip()
        cur["answer_body"] = " ".join(a.strip() for a in answer_lines).strip()
        cur["points"] = points
        cur["marks"] = marks or len(points)
        subs.append(cur)
        cur = None

    for page, raw in lines:
        line = raw.strip()
        if not line:
            continue
        qm = QUESTION_RE.match(line)
        if qm:
            current_q = int(re.search(r"\d+", line).group())
            continue
        m = SUBPART_RE.match(line)
        # Require a leading question digit OR a roman part. A bare "(a) ..." with
        # neither is almost always an inline enumeration inside an answer, not a
        # new sub-question header -- matching those fragments real sub-questions.
        if m and (m.group("q") or m.group("roman")):
            q = int(m.group("q")) if m.group("q") else current_q
            current_q = q
            sub = m.group("sub")
            key = (q, sub)
            if cur is not None and cur["_key"] == key:
                # Nested "(i)/(ii)" continuation of the same N(x): fold it in so
                # its bullets accumulate under one gradeable question.
                cur["_seg"].append(line)
                continue
            close()
            cur = {
                "question_num": q,
                "sub": sub,
                "label": f"{q}({sub})",
                "page": page,
                "_key": key,
                "_header": line,
                "_seg": [],
            }
            continue
        if cur is None:
            continue  # preamble before the first sub-question (headers etc.)
        cur["_seg"].append(line)

    close()
    return subs


# ---------------------------------------------------------------------------
# PDF text extraction (page-aware so chunks carry a real page number)
# ---------------------------------------------------------------------------
def extract_lines(pdf_path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, line_text), ...] for the whole PDF via PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")
    doc = fitz.open(pdf_path)
    out: list[tuple[int, str]] = []
    try:
        for pno in range(doc.page_count):
            for line in doc.load_page(pno).get_text("text").splitlines():
                out.append((pno + 1, line))
    finally:
        doc.close()
    return out


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
# Ingest one already-extracted solution document
# ---------------------------------------------------------------------------
def ingest_solution_lines(db: sqlite3.Connection, *, lines: list[tuple[int, str]],
                          subject_id: str, source_file: str, objectives: list[dict],
                          counts: dict, code: str, embed_fn=None,
                          min_overlap: int = MIN_KEYWORD_OVERLAP,
                          content_hash: str | None = None) -> dict | None:
    """Parse + store one solution document. Returns its session meta, or None.

    Writes a documents row, then per sub-question a stem chunk + bullet
    mark_points. Unmatched / bullet-less sub-questions go to ingest_review_queue
    and store nothing else. Caller commits.
    """
    full_text = "\n".join(t for _, t in lines)
    meta = parse_session(full_text)
    if meta is None:
        db.execute(
            "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) VALUES (?, ?, ?)",
            (source_file, full_text[:500], "no_session_header"),
        )
        return None

    chash = content_hash or hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    doc_id = f"sol-{chash[:12]}"
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
        "source_file, content_hash) VALUES (?, ?, 'mark_scheme', ?, ?, ?, ?)",
        (doc_id, subject_id, meta["paper_label"], meta["year"], source_file, chash),
    )
    counts["files"] += 1

    for sq in parse_subquestions(lines):
        match_text = f"{sq['stem']} {' '.join(sq['points'])}"
        obj_id, _score = best_objective(match_text, objectives, min_overlap)
        if obj_id is None:
            db.execute(
                "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) VALUES (?, ?, ?)",
                (source_file, f"{sq['label']}: {sq['stem'][:280]}", "no_objective_match"),
            )
            counts["queued_no_objective"] += 1
            continue

        question_id = f"{code}-{meta['year']}{meta['month_tag']}-P2-q{sq['question_num']}{sq['sub']}"

        if not sq["points"]:
            # Prose-only answer: no discrete bullets to grade against. Index the
            # stem for retrieval, then queue the stem AND the prose answer body so
            # the LLM extractor (extract_prose_markpoints.py) has both the question
            # context and the answer text. objective_id + doc_id are stored on the
            # row itself so the extractor never has to guess via a text lookup.
            _insert_stem_chunk(db, doc_id, subject_id, obj_id, sq, question_id, embed_fn)
            queue_text = f"{sq['stem']}\n\nANSWER:\n{sq.get('answer_body', '')}"
            db.execute(
                "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, "
                "objective_id, doc_id) VALUES (?, ?, ?, ?, ?)",
                (source_file, queue_text, "prose_answer_no_bullets", obj_id, doc_id),
            )
            counts["queued_no_points"] += 1
            continue

        _insert_stem_chunk(db, doc_id, subject_id, obj_id, sq, question_id, embed_fn)
        counts["questions"] += 1
        for order, pt in enumerate(sq["points"], 1):
            db.execute(
                "INSERT OR IGNORE INTO mark_points (mark_point_id, objective_id, "
                "question_id, doc_id, point_text, marks_value, point_order) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (f"{question_id}-mp{order}", obj_id, question_id, doc_id, pt, order),
            )
            counts["mark_points"] += 1

    return meta


def _insert_stem_chunk(db, doc_id, subject_id, obj_id, sq, question_id, embed_fn):
    """One chunk holding the question stem, keyed so the picker can find it.

    chunk_id = "<question_id>-stem"; question_num = the "1(a)" label. Embedding
    is skipped when embed_fn is None (offline mode); grading needs no vector.
    """
    stem = sq["stem"] or sq["label"]
    cur = db.execute(
        "INSERT OR IGNORE INTO chunks (doc_id, objective_id, subject_id, chunk_text, "
        "page, question_num, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, obj_id, subject_id, stem, sq["page"], sq["label"], f"{question_id}-stem"),
    )
    if embed_fn is not None and cur.lastrowid:
        db.execute(
            "INSERT OR REPLACE INTO vec_mark_schemes(rowid, embedding) VALUES (?, ?)",
            (cur.lastrowid, serialize_vec(embed_fn(stem))),
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def assert_subject_locked(db: sqlite3.Connection, subject_id: str) -> None:
    row = db.execute(
        "SELECT syllabus_locked FROM subjects WHERE subject_id = ?", (subject_id,)
    ).fetchone()
    if row is None:
        sys.exit(f"ERROR: subject '{subject_id}' is not in the database. Lock its syllabus first (Stage 2).")
    if row["syllabus_locked"] != 1:
        sys.exit(f"ERROR: subject '{subject_id}' is not locked (syllabus_locked != 1). Ingestion is blocked.")


def load_objectives(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    return [
        dict(r) for r in db.execute(
            "SELECT objective_id, content_stmt FROM objectives WHERE subject_id = ?",
            (subject_id,),
        ).fetchall()
    ]


def already_ingested(db: sqlite3.Connection, content_hash: str) -> bool:
    return db.execute(
        "SELECT 1 FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone() is not None


def ensure_queue_columns(db: sqlite3.Connection) -> None:
    """Add objective_id / doc_id to ingest_review_queue if a pre-existing DB
    lacks them (SQLite has no ALTER TABLE ... ADD COLUMN IF NOT EXISTS). Safe to
    run repeatedly; both columns are nullable so the migration needs no default.
    """
    cols = {r[1] for r in db.execute("PRAGMA table_info(ingest_review_queue)")}
    if "objective_id" not in cols:
        db.execute("ALTER TABLE ingest_review_queue ADD COLUMN objective_id TEXT")
    if "doc_id" not in cols:
        db.execute("ALTER TABLE ingest_review_queue ADD COLUMN doc_id TEXT REFERENCES documents(doc_id)")
    db.commit()


def ingest_subject(db: sqlite3.Connection, subject_id: str, src_dir: Path,
                   embed_fn=None) -> dict:
    """Ingest every *.pdf in src_dir as a solution document for subject_id."""
    assert_subject_locked(db, subject_id)
    ensure_queue_columns(db)
    objectives = load_objectives(db, subject_id)
    if not objectives:
        sys.exit(f"ERROR: no objectives loaded for '{subject_id}'. Nothing to map to.")
    if not src_dir.is_dir():
        sys.exit(f"ERROR: source folder not found: {src_dir}")

    code = SUBJECT_CODE.get(subject_id) or objectives[0]["objective_id"].split("-")[0]
    counts = new_counts()
    for pdf in sorted(src_dir.glob("*.pdf")):
        chash = file_hash(pdf)
        if already_ingested(db, chash):
            counts["skipped_duplicate"] += 1
            continue
        lines = extract_lines(pdf)
        ingest_solution_lines(
            db, lines=lines, subject_id=subject_id, source_file=str(pdf),
            objectives=objectives, counts=counts, code=code, embed_fn=embed_fn,
            content_hash=chash,
        )
        db.commit()
    return counts


def print_summary(counts: dict) -> None:
    print("\n" + "=" * 60)
    print("Solutions ingestion summary")
    print("=" * 60)
    print(f"  files processed          : {counts['files']}")
    print(f"  gradeable questions      : {counts['questions']}")
    print(f"  mark points extracted    : {counts['mark_points']}")
    print(f"  queued (no objective)    : {counts['queued_no_objective']}")
    print(f"  queued (no bullets)      : {counts['queued_no_points']}")
    print(f"  files skipped (dup)      : {counts['skipped_duplicate']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Paper 2 solution PDFs into mark_points.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--src", help="folder of solution PDFs (default: KB 03_MARK_SCHEMES)")
    ap.add_argument("--embed", action="store_true",
                    help="also embed stem chunks into vec_mark_schemes (needs Ollama)")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    if args.src:
        src_dir = Path(args.src)
    else:
        src_dir = Path(os.getenv("KB_ROOT")) / args.subject / "03_MARK_SCHEMES"

    embed_fn = ollama_embed if args.embed else None
    db = open_db(db_path)
    try:
        counts = ingest_subject(db, args.subject, src_dir, embed_fn=embed_fn)
        print_summary(counts)
    finally:
        db.close()


if __name__ == "__main__":
    main()
