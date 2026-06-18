# PHASE: build
"""
backend/ingest.py
=================
Stage 4 ingestion pipeline: PDF -> chunk -> match to objective -> embed ->
FK-validate -> index into the correct vec_* table.

Hard rules (CLAUDE.md):
  * Every indexed chunk carries a real objectives.objective_id FK. A chunk with
    no confident objective match is written to ingest_review_queue and NOT
    indexed -- no unmapped chunk is ever indexed silently (Rule 1).
  * The subject must exist with syllabus_locked = 1 before anything is ingested.
  * Embeddings go through ollama_client.ollama_embed (httpx, keep_alive=0). The
    embed function is injectable so the pipeline is unit-testable without Ollama.

Content-type routing (folder -> content_type -> vec table):
    01_SPECIMEN_PAPERS -> specimen    -> vec_past_papers
    02_PAST_PAPERS     -> past_paper  -> vec_past_papers
    03_MARK_SCHEMES    -> mark_scheme -> vec_mark_schemes
    04_NOTES           -> notes       -> vec_notes

Usage:
    python backend/ingest.py --subject Principles_of_Business
    python backend/ingest.py --review-queue
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

# backend/ on sys.path so `from ollama_client import ...` works from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_embed  # noqa: E402
from db.backup import backup_first  # noqa: E402

# --- Chunking ---------------------------------------------------------------
CHUNK_SIZE = 500
CHUNK_OVERLAP = 100

# --- Keyword-overlap matching ----------------------------------------------
# A chunk is mapped to the objective with the most shared content words. If the
# best score is below this floor the chunk is queued for human review instead.
MIN_KEYWORD_OVERLAP = 2

# --- Folder / content-type / vec-table routing -----------------------------
FOLDER_CONTENT_TYPE = {
    "01_SPECIMEN_PAPERS": "specimen",
    "02_PAST_PAPERS": "past_paper",
    "03_MARK_SCHEMES": "mark_scheme",
    "04_NOTES": "notes",
}

VEC_TABLE = {
    "specimen": "vec_past_papers",
    "past_paper": "vec_past_papers",
    "mark_scheme": "vec_mark_schemes",
    "notes": "vec_notes",
}

# Light stopword list -- enough to stop function words dominating the overlap.
STOPWORDS = {
    "the", "and", "for", "are", "with", "that", "this", "from", "have", "has",
    "was", "were", "will", "shall", "should", "would", "can", "could", "may",
    "its", "their", "his", "her", "they", "them", "you", "your", "our", "out",
    "into", "onto", "over", "under", "such", "than", "then", "when", "what",
    "which", "who", "whom", "how", "why", "where", "all", "any", "each", "both",
    "more", "most", "some", "not", "but", "also", "only", "very", "must",
    "use", "used", "using", "one", "two", "three", "etc", "eg",
}

GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# DB helpers (same pattern as init_db / syllabus_parser)
# ---------------------------------------------------------------------------
def open_db(db_path: str) -> sqlite3.Connection:
    """Open the SSD database with sqlite-vec loaded and FKs enabled."""
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
    """Pack a float list into the little-endian blob sqlite-vec expects."""
    return struct.pack(f"{len(v)}f", *v)


def index_chunk(db: sqlite3.Connection, rowid: int,
                embedding: list[float], table: str) -> None:
    db.execute(
        f"INSERT OR REPLACE INTO {table}(rowid, embedding) VALUES (?, ?)",
        (rowid, serialize_vec(embedding)),
    )


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------
def chunk_page(text: str, size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split one page of text into ~`size`-char windows overlapping by `overlap`."""
    text = (text or "").strip()
    if not text:
        return []
    step = max(size - overlap, 1)
    chunks: list[str] = []
    i, n = 0, len(text)
    while i < n:
        chunks.append(text[i:i + size])
        if i + size >= n:
            break
        i += step
    return chunks


_WORD_RE = re.compile(r"[a-z]+")


def tokenize(s: str) -> set[str]:
    """Lowercase content words (len > 2, not a stopword)."""
    return {
        w for w in _WORD_RE.findall((s or "").lower())
        if len(w) > 2 and w not in STOPWORDS
    }


def _match_objective(ctoks: set, objectives: list[dict]) -> tuple[str | None, int]:
    """(objective_id, score) for the highest keyword-overlap objective, no threshold."""
    best_id, best_score = None, 0
    for obj in objectives:
        shared = len(ctoks & tokenize(obj["content_stmt"]))
        if shared > best_score:
            best_id, best_score = obj["objective_id"], shared
    return best_id, best_score


def best_objective(chunk: str, objectives: list[dict],
                   min_overlap: int = MIN_KEYWORD_OVERLAP,
                   preferred_objectives: list[str] | None = None) -> tuple[str | None, int]:
    """Return (objective_id, score) for the best keyword-overlap match.

    score = number of shared content words. Returns (None, best_score) when the
    best score is below `min_overlap` -- i.e. no confident match.

    When `preferred_objectives` is given (session 4: the objectives a Gemini
    classification bound the file to), the chunk is matched against ONLY those
    objectives first; a confident hit there wins. The full-syllabus search runs only
    when no preferred objective clears the threshold -- so the classification steers
    binding without ever forcing a weak match.
    """
    ctoks = tokenize(chunk)
    if not ctoks:
        return None, 0
    if preferred_objectives:
        pref_set = set(preferred_objectives)
        pref = [o for o in objectives if o["objective_id"] in pref_set]
        if pref:
            pid, pscore = _match_objective(ctoks, pref)
            if pscore >= min_overlap:
                return pid, pscore
    best_id, best_score = _match_objective(ctoks, objectives)
    if best_score >= min_overlap:
        return best_id, best_score
    return None, best_score


# Each award point begins on its own line with a bullet (-, *, •) or an
# enumerator like "1.", "(a)", "a)".
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\(?[a-z0-9]{1,3}[).])\s+(.+\S)\s*$", re.I)


def parse_mark_points(text: str) -> list[str]:
    """Extract individual award points from a mark-scheme chunk."""
    points = []
    for line in (text or "").splitlines():
        m = _BULLET_RE.match(line)
        if m:
            points.append(m.group(1).strip())
    return points


# --- past-paper filename / question-number parsing -------------------------
# Feeds documents.paper / documents.year / chunks.question_num so the quiz
# picker's filter dropdowns (GET /api/filters, GET /api/questions) have real
# values to show. All three are NULL for non-past_paper content.

# "P2", "p2", "Paper2", "Paper 2", "p_2" -> capture the paper digit. The
# (?!\d) lookahead stops "p2" in "p2_2022" from also swallowing a year digit.
_PAPER_RE = re.compile(r"p(?:aper)?[\s_]*([23])(?!\d)", re.I)
# Exam sitting word -> normalised label (May is folded into June per spec).
# Letter-aware boundaries so "_Jan_" is found (underscore is a word char, so
# \b would miss it) while a run-on like "MayJune" is correctly rejected.
_SITTING_RE = re.compile(r"(?<![a-z])(jan(?:uary)?|jun(?:e)?|may)(?![a-z])", re.I)
_SITTING_MAP = {
    "jan": "January", "january": "January",
    "jun": "June", "june": "June", "may": "June",
}
# Top-level question number at the start of a line: "1.", "2)", "3(", "1 (a)".
_QNUM_RE = re.compile(r"(?:^|\n)\s*(\d+)\s*[.()]")


def parse_past_paper_filename(filename: str) -> tuple[str | None, int | None]:
    """Best-effort (paper_str, year_int) from a loose past-paper filename.

    e.g. "June 2019 p2.pdf" -> ("Paper 2 - June 2019", 2019)
         "POB_p2_2022.pdf"  -> ("Paper 2 - 2022", 2022)
         "Jan 26 POB.PDF"   -> ("Paper 2 - January 2026", 2026)
    Returns (None, None) when no year can be found. Paper defaults to "Paper 2"
    when no paper marker is present.
    """
    stem = Path(filename).stem

    # Paper number (default Paper 2 if ambiguous).
    pm = _PAPER_RE.search(stem)
    paper = f"Paper {pm.group(1) if pm else '2'}"

    # Year: prefer a 4-digit year in 2002-2030.
    year = None
    for cand in re.findall(r"\d{4}", stem):
        n = int(cand)
        if 2002 <= n <= 2030:
            year = n
            break
    if year is None:
        # Fall back to a standalone 2-digit year, ignoring the paper digit.
        stem_no_paper = _PAPER_RE.sub(" ", stem)
        m2 = re.search(r"\b(\d{2})\b", stem_no_paper)
        if m2:
            n = int(m2.group(1))
            year = 2000 + n if n <= 30 else 1900 + n
    if year is None:
        return None, None

    # Sitting (optional).
    sm = _SITTING_RE.search(stem)
    sitting = _SITTING_MAP[sm.group(1).lower()] if sm else ""

    tail = f"{sitting} {year}".strip()
    return f"{paper} - {tail}", year


def detect_question_num(chunk_text: str) -> str | None:
    """Return the leading top-level question number in a chunk, or None.

    Intentionally simple: top-level numbers only (sub-parts a/b/c deferred).
    """
    m = _QNUM_RE.search((chunk_text or "")[:200])
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Counts / summary
# ---------------------------------------------------------------------------
def new_counts() -> dict:
    return {
        "files": 0,
        "pages": 0,
        "chunks_indexed": 0,
        "mark_points": 0,
        "queued": 0,
        "skipped_duplicate": 0,
        "pp_with_qnum": 0,
    }


# ---------------------------------------------------------------------------
# Core: ingest one page of already-extracted text
# ---------------------------------------------------------------------------
def ingest_page(db: sqlite3.Connection, *, doc_id: str, subject_id: str,
                content_type: str, source_file: str, page: int, text: str,
                objectives: list[dict], counts: dict, embed_fn=ollama_embed,
                min_overlap: int = MIN_KEYWORD_OVERLAP,
                preferred_objectives: list[str] | None = None) -> None:
    """Chunk one page, match each chunk to an objective, then index or queue it.

    Indexed chunks always have a real objective_id FK. Unmatched chunks go to
    ingest_review_queue and are NOT indexed. The document row (doc_id) must
    already exist. Caller commits. `preferred_objectives` steers each chunk toward
    the classification-bound objectives first (session 4).
    """
    table = VEC_TABLE[content_type]
    for idx, ctext in enumerate(chunk_page(text)):
        ctext = ctext.strip()
        if not ctext:
            continue

        obj_id, _score = best_objective(ctext, objectives, min_overlap,
                                        preferred_objectives=preferred_objectives)
        if obj_id is None:
            db.execute(
                "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) "
                "VALUES (?, ?, ?)",
                (source_file, ctext, "no_objective_match"),
            )
            counts["queued"] += 1
            continue

        # Past-paper chunks carry a top-level question number so the quiz
        # picker can filter by question; other content types leave it NULL.
        question_num = detect_question_num(ctext) if content_type == "past_paper" else None

        chunk_id = f"{doc_id}-p{page}-c{idx}"
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, "
            "page, question_num, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, obj_id, subject_id, ctext, page, question_num, chunk_id),
        )
        rowid = cur.lastrowid
        index_chunk(db, rowid, embed_fn(ctext), table)
        counts["chunks_indexed"] += 1
        if question_num is not None:
            counts["pp_with_qnum"] += 1

        if content_type == "mark_scheme":
            points = parse_mark_points(ctext)
            if points:
                for order, pt in enumerate(points, 1):
                    mp_id = f"{obj_id}-{doc_id}-p{page}c{idx}-mp{order}"
                    db.execute(
                        "INSERT OR IGNORE INTO mark_points (mark_point_id, "
                        "objective_id, question_id, doc_id, point_text, "
                        "marks_value, point_order) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (mp_id, obj_id, None, doc_id, pt, 1, order),
                    )
                    counts["mark_points"] += 1
            else:
                # Indexed for retrieval, but flag for manual mark-point entry.
                db.execute(
                    "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) "
                    "VALUES (?, ?, ?)",
                    (source_file, ctext, "markscheme_no_points"),
                )
                counts["queued"] += 1


# ---------------------------------------------------------------------------
# Document / subject orchestration
# ---------------------------------------------------------------------------
def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def already_ingested(db: sqlite3.Connection, content_hash: str) -> bool:
    return db.execute(
        "SELECT 1 FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone() is not None


def assert_subject_locked(db: sqlite3.Connection, subject_id: str) -> None:
    row = db.execute(
        "SELECT syllabus_locked FROM subjects WHERE subject_id = ?", (subject_id,)
    ).fetchone()
    if row is None:
        sys.exit(
            f"ERROR: subject '{subject_id}' is not in the database. "
            "Load + lock its syllabus first (Stage 2)."
        )
    if row["syllabus_locked"] != 1:
        sys.exit(
            f"ERROR: subject '{subject_id}' is not locked (syllabus_locked != 1). "
            "Ingestion is blocked until the syllabus is signed off and locked."
        )


def load_objectives(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    return [
        dict(r) for r in db.execute(
            "SELECT objective_id, content_stmt FROM objectives WHERE subject_id = ?",
            (subject_id,),
        ).fetchall()
    ]


def content_type_from_path(path: str) -> str | None:
    parts = Path(path).parts
    for folder, ctype in FOLDER_CONTENT_TYPE.items():
        if folder in parts:
            return ctype
    return None


def extract_pdf_pages(pdf_path: Path):
    """Yield (page_number, page_text) for each page using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")
    doc = fitz.open(pdf_path)
    try:
        for pno in range(doc.page_count):
            yield pno + 1, doc.load_page(pno).get_text("text")
    finally:
        doc.close()


def ingest_subject(db: sqlite3.Connection, subject_id: str, kb_root: str,
                   embed_fn=ollama_embed) -> dict:
    """Walk the subject's KB folders and ingest every new PDF."""
    assert_subject_locked(db, subject_id)
    objectives = load_objectives(db, subject_id)
    if not objectives:
        sys.exit(f"ERROR: no objectives loaded for '{subject_id}'. Nothing to map chunks to.")

    counts = new_counts()
    subj_root = Path(kb_root) / subject_id
    if not subj_root.is_dir():
        sys.exit(f"ERROR: knowledge-base folder not found: {subj_root}")

    for folder, ctype in FOLDER_CONTENT_TYPE.items():
        fdir = subj_root / folder
        if not fdir.is_dir():
            continue
        for pdf in sorted(fdir.glob("*.pdf")):
            chash = file_hash(pdf)
            if already_ingested(db, chash):
                counts["skipped_duplicate"] += 1
                continue
            doc_id = f"{ctype}-{chash[:12]}"
            paper_str, year_int = (None, None)
            if ctype == "past_paper":
                paper_str, year_int = parse_past_paper_filename(pdf.name)
            db.execute(
                "INSERT INTO documents (doc_id, subject_id, content_type, "
                "paper, year, source_file, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (doc_id, subject_id, ctype, paper_str, year_int, str(pdf), chash),
            )
            counts["files"] += 1
            for page, text in extract_pdf_pages(pdf):
                counts["pages"] += 1
                ingest_page(
                    db, doc_id=doc_id, subject_id=subject_id, content_type=ctype,
                    source_file=str(pdf), page=page, text=text,
                    objectives=objectives, counts=counts, embed_fn=embed_fn,
                )
            db.commit()
    return counts


# ---------------------------------------------------------------------------
# Single-file entry point (Upload session 4)
# ---------------------------------------------------------------------------
# A staged upload arrives as already-extracted text (PDF/DOCX/image OCR done in the
# upload feature). These markers came from uploads.py; we split on them to recover
# page numbers so past-paper question-number detection still works.
_PAGE_MARKER_NUM_RE = re.compile(r"\[Page\s+(\d+)[^\]]*\]")


def _split_marked_pages(full_text: str):
    """Yield (page_number, text) from text carrying '[Page N]' markers. A body with no
    markers (e.g. a DOCX) is a single page 1."""
    markers = list(_PAGE_MARKER_NUM_RE.finditer(full_text or ""))
    if not markers:
        if (full_text or "").strip():
            yield 1, full_text
        return
    for i, m in enumerate(markers):
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(full_text)
        page_text = full_text[start:end]
        if page_text.strip():
            yield int(m.group(1)), page_text


def _document_pages(path: Path, full_text: str | None):
    """(page, text) pairs to ingest: from supplied full_text (preferred -- covers DOCX/
    images), else extracted from a PDF at `path` via PyMuPDF."""
    if full_text is not None:
        return list(_split_marked_pages(full_text))
    return list(extract_pdf_pages(Path(path)))


def ingest_document(db: sqlite3.Connection, *, path, subject_id: str,
                    content_type: str, objectives: list[dict],
                    embed_fn=ollama_embed, preferred_objectives: list[str] | None = None,
                    full_text: str | None = None, source_file: str | None = None,
                    min_overlap: int = MIN_KEYWORD_OVERLAP) -> dict:
    """Ingest ONE file into the corpus. Single-file entry point for the upload
    pipeline (session 4); mirrors what ingest_subject does per file.

    Mints a hash-based doc_id, inserts the documents row, then chunks the text
    (supplied via full_text -- the upload feature's already-extracted body, which
    also covers DOCX/images -- or extracted from a PDF at `path`), matching each chunk
    to an objective (preferring `preferred_objectives`), embedding, and indexing it.
    The caller commits. Returns {doc_id, chunks_created, objectives_hit,
    skipped_duplicate}; on a content-hash duplicate it writes nothing and returns
    skipped_duplicate=True.
    """
    path = Path(path)
    source_file = source_file or str(path)
    chash = file_hash(path)
    if already_ingested(db, chash):
        return {"doc_id": None, "chunks_created": 0, "objectives_hit": [],
                "skipped_duplicate": True}

    doc_id = f"{content_type}-{chash[:12]}"
    paper_str, year_int = (None, None)
    if content_type == "past_paper":
        paper_str, year_int = parse_past_paper_filename(path.name)
    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
        "source_file, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, subject_id, content_type, paper_str, year_int, source_file, chash),
    )

    counts = new_counts()
    for page, text in _document_pages(path, full_text):
        ingest_page(
            db, doc_id=doc_id, subject_id=subject_id, content_type=content_type,
            source_file=source_file, page=page, text=text, objectives=objectives,
            counts=counts, embed_fn=embed_fn, min_overlap=min_overlap,
            preferred_objectives=preferred_objectives,
        )
    objectives_hit = [
        r[0] for r in db.execute(
            "SELECT DISTINCT objective_id FROM chunks WHERE doc_id = ?", (doc_id,)
        ).fetchall()
    ]
    return {"doc_id": doc_id, "chunks_created": counts["chunks_indexed"],
            "objectives_hit": objectives_hit, "skipped_duplicate": False}


# ---------------------------------------------------------------------------
# Interactive review queue
# ---------------------------------------------------------------------------
def review_queue(db: sqlite3.Connection, embed_fn=ollama_embed) -> None:
    """List queued chunks and prompt for an objective_id to assign each."""
    rows = db.execute(
        "SELECT id, source_file, chunk_text, reason FROM ingest_review_queue ORDER BY id"
    ).fetchall()
    if not rows:
        print("Review queue is empty.")
        return

    print(f"{len(rows)} chunk(s) queued for review.\n")
    assigned = 0
    for r in rows:
        snippet = (r["chunk_text"] or "")[:300].replace("\n", " ")
        ellipsis = "..." if len(r["chunk_text"] or "") > 300 else ""
        print("=" * 60)
        print(f"queue id : {r['id']}")
        print(f"source   : {r['source_file']}")
        print(f"reason   : {r['reason']}")
        print(f"text     : {snippet}{ellipsis}")
        ans = input("objective_id to assign (blank = skip, q = quit): ").strip()
        if ans.lower() == "q":
            break
        if not ans:
            continue

        obj = db.execute(
            "SELECT subject_id FROM objectives WHERE objective_id = ?", (ans,)
        ).fetchone()
        if obj is None:
            print(f"  ! no such objective_id '{ans}' -- skipped")
            continue

        ctype = content_type_from_path(r["source_file"])
        doc = db.execute(
            "SELECT doc_id FROM documents WHERE source_file = ?", (r["source_file"],)
        ).fetchone()
        if ctype is None or doc is None:
            print("  ! could not resolve content type / document for this source -- skipped")
            continue

        chunk_id = f"{doc['doc_id']}-review-{r['id']}"
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, chunk_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc["doc_id"], ans, obj["subject_id"], r["chunk_text"], chunk_id),
        )
        try:
            index_chunk(db, cur.lastrowid, embed_fn(r["chunk_text"]), VEC_TABLE[ctype])
        except Exception as exc:  # embedding needs Ollama up
            db.rollback()
            print(f"  ! embedding failed ({exc}). Is Ollama running? -- left in queue")
            continue
        db.execute("DELETE FROM ingest_review_queue WHERE id = ?", (r["id"],))
        db.commit()
        assigned += 1
        print(f"  -> indexed under {ans}")

    print(f"\nAssigned {assigned} chunk(s); {len(rows) - assigned} left in queue.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def print_summary(counts: dict) -> None:
    print("\n" + "=" * 60)
    print("Ingestion summary")
    print("=" * 60)
    print(f"  files processed        : {counts['files']}")
    print(f"  pages read             : {counts['pages']}")
    print(f"  chunks indexed         : {counts['chunks_indexed']}")
    print(f"  mark points extracted  : {counts['mark_points']}")
    print(f"  chunks queued (review) : {counts['queued']}")
    print(f"  files skipped (dup)    : {counts['skipped_duplicate']}")
    print(f"  past_paper chunks with question_num : {counts['pp_with_qnum']}")


@backup_first("pre_ingest")
def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest a subject's PDFs into the vec index.")
    ap.add_argument("--subject", help="e.g. Principles_of_Business")
    ap.add_argument("--review-queue", action="store_true",
                    help="Interactively assign objective_ids to queued chunks.")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    db = open_db(db_path)
    try:
        if args.review_queue:
            review_queue(db)
            return
        if not args.subject:
            sys.exit("ERROR: provide --subject <Subject> or --review-queue.")
        counts = ingest_subject(db, args.subject, os.getenv("KB_ROOT"))
        print_summary(counts)
    finally:
        db.close()


if __name__ == "__main__":
    main()
