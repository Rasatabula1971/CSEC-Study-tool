# PHASE: build
"""
backend/ingest_worked_solutions.py
==================================
Dedicated ingester for the Macmillan **Worked Solutions** book
("Principles of Business: Worked Solutions for CSEC Examinations 2007-2011",
Alvin Ramsaroop, 2012). This book is a *mark scheme*: every bulleted answer
point is a markable point. It is structurally different from the per-paper
solution text files handled by ingest_solutions.py and from the generic PDF
windowing in ingest.py, so it gets its own parser.

What it produces (per CLAUDE.md):
    documents    one row per YEAR (2007-2011), content_type='mark_scheme',
                 paper='Paper_02'. doc_id = "POB-WS-<year>".
    chunks       one '-stem' chunk per gradeable leaf part (e.g. 4(a)(i)),
                 indexed into vec_mark_schemes for retrieval. Every chunk
                 carries a real objectives.objective_id FK (Rule 1).
    mark_points  one row per answer bullet, keyed by question_id == the stem
                 chunk_id (the '-stem' convention ingest_solutions.py uses, so
                 grade.fetch_mark_points / the quiz picker resolve them).

How a question is mapped to an objective (HYBRID, chosen by the user):
    1. The book's own "Table of topics" (PDF pages 142-143) maps each Paper 02
       question PART (e.g. "4a") to a broad SYLLABUS TOPIC. We parse it with
       fitz.find_tables().
    2. The topic is matched to a syllabus_sections row by content-word overlap.
       Topics that have left the current syllabus (the book predates the
       2017+ revision: "Social Accounting and Global Trade", "Regional and
       Global Business Environment") match nothing and are queued, never guessed.
    3. WITHIN that section only, each leaf part is matched to a specific
       objective by embedding the question context (stem + first 2-3 bullets)
       and taking the best cosine match against the section's objective
       content_stmts. >= SIM_THRESHOLD writes; below it queues with the top-3
       candidates pre-listed (fast triage later).

Hard rules honoured:
  * Rule 1 -- no objective_id => no chunk and no mark_point. Everything that
    cannot be confidently mapped is queued to ingest_review_queue.
  * Rule 2 -- parsing is pure Python; the LLM is never asked to extract or
    score. Embeddings (deterministic vectors) are the only model use, and only
    to RANK objectives within a section the book already chose.

The structure (page ranges, question-number format, bullet char, mark-rule
phrasing) was verified by Stage-0 reconnaissance on the actual 144-page PDF.

Usage:
    # dry run on the real book (parses + ranks, writes NOTHING):
    python backend/ingest_worked_solutions.py --subject Principles_of_Business \\
        --file "D:\\CSEC\\...\\...worked-solutions...20072011_compress.pdf" --dry-run

    # real ingest:
    python backend/ingest_worked_solutions.py --subject Principles_of_Business \\
        --file "E:\\CSEC_AI_STUDY_PARTNER\\03_KNOWLEDGE_BASE\\Principles_of_Business\\03_MARK_SCHEMES\\<book>.pdf"
"""

import argparse
import hashlib
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# backend/ on sys.path so the bare imports resolve from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_embed  # noqa: E402
from ingest import (  # noqa: E402
    open_db,
    serialize_vec,
    tokenize,
    assert_subject_locked,
)
from ingest_solutions import ensure_queue_columns  # noqa: E402

# --- Constants verified by Stage-0 recon on the real book ------------------
# Paper 02 year sections, 1-based PDF page numbers, inclusive (from the
# embedded outline; derive_layout() re-reads the outline at runtime so this is
# a documented fallback, not a magic guess).
YEAR_RANGES = {
    2007: (76, 91),
    2008: (92, 104),
    2009: (105, 116),
    2010: (117, 128),
    2011: (129, 139),
}
TOPIC_TABLE_PAGES = (142, 143)
# The two year-column layout of the topic table: page 142 carries 2007/8/9,
# page 143 carries 2010/11. Each year has an "MC Paper 01" column then a
# "Paper 02" column; we only use Paper 02.
TOPIC_PAGE_YEARS = {142: (2007, 2008, 2009), 143: (2010, 2011)}

SIM_THRESHOLD = 0.60          # objective match confidence floor (user spec 5g)
SECTION_MATCH_MIN = 0.5       # topic->section content-word Jaccard floor
SUBJECT_TOKENS = {"Principles_of_Business": "POB"}

WORD2NUM = {"one": 1, "two": 2, "three": 3, "four": 4,
            "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}

# --- Line grammar (operate on NFKC-normalised, stripped lines) --------------
QNUM_RE = re.compile(r"^(\d{1,2})\b\s*(.*)$")
PART_RE = re.compile(r"^\(([a-h])\)\s*(.*)$")               # (a)..(h); excludes (i)
SUB_RE = re.compile(r"^\((i{1,3}|iv|v|vi{0,3}|ix|x)\)\s*(.*)$", re.I)  # roman i..x
MARKS_RE = re.compile(r"\((\d+)\s*marks?\)", re.I)
RULE_MARKS_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\s+marks?\b", re.I)
BULLET = "•"  # •


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    """NFKC fold so ligatures become ASCII (fi -> fi, fl -> fl) before parsing."""
    return unicodedata.normalize("NFKC", text or "")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def subject_token(subject_id: str) -> str:
    return SUBJECT_TOKENS.get(subject_id, subject_id[:3].upper())


def rel_source(file_path: str) -> str:
    """source_file relative to KB_ROOT when the file lives under it, else its name."""
    kb = os.getenv("KB_ROOT")
    fp = Path(file_path).resolve()
    if kb:
        try:
            return str(fp.relative_to(Path(kb).resolve()))
        except ValueError:
            pass
    return Path(file_path).name


# ---------------------------------------------------------------------------
# Layout discovery (outline first, recon constants as fallback)
# ---------------------------------------------------------------------------
def derive_layout(doc) -> tuple[dict, tuple]:
    """(year_ranges, topic_table_pages) from the PDF outline, else recon defaults.

    The real book has a clean embedded outline whose entries name each
    "Paper 02 ... May/June <year>" section; we read the start pages from there
    and bound each year by the next section. Falls back to the recon constants
    if the outline is missing (e.g. a stripped copy).
    """
    try:
        toc = doc.get_toc()
    except Exception:
        toc = []
    starts: dict[int, int] = {}
    after = None
    for _lvl, title, page in toc:
        for yr in YEAR_RANGES:
            if "Paper 02" in title and str(yr) in title:
                starts[yr] = page
        if "How did you do" in title and after is None:
            after = page
    if len(starts) != len(YEAR_RANGES):
        return dict(YEAR_RANGES), TOPIC_TABLE_PAGES
    years = sorted(starts)
    ranges = {}
    for i, yr in enumerate(years):
        end = (starts[years[i + 1]] - 1) if i + 1 < len(years) else (after - 1 if after else doc.page_count)
        ranges[yr] = (starts[yr], end)
    topic_start = next((p for _l, t, p in toc if "Table of topics" in t), TOPIC_TABLE_PAGES[0])
    return ranges, (topic_start, topic_start + 1)


# ---------------------------------------------------------------------------
# Topic table -> {(year, qnum, letter): topic_string}
# ---------------------------------------------------------------------------
def expand_paper02_cell(cell_text: str) -> list[tuple[int, str]]:
    """Parse a Paper-02 topic-table cell into (qnum, letter) pairs.

    A cell may list several question groups, e.g. "4a, b, c, d 5b, c, d, e, f"
    -> [(4,'a'),(4,'b'),(4,'c'),(4,'d'),(5,'b'),(5,'c'),(5,'d'),(5,'e'),(5,'f')].
    "1c, 2a" -> [(1,'c'),(2,'a')]. "8d, e" -> [(8,'d'),(8,'e')]. Empty -> [].
    """
    out: list[tuple[int, str]] = []
    text = normalize(cell_text or "").strip()
    if not text:
        return out
    # Each group = a number followed by one or more comma-separated letters.
    for m in re.finditer(r"(\d+)\s*((?:[a-h]\s*,?\s*)+)", text, re.I):
        qnum = int(m.group(1))
        letters = re.findall(r"[a-h]", m.group(2), re.I)
        for ltr in letters:
            out.append((qnum, ltr.lower()))
    return out


def parse_topic_table(doc, topic_pages: tuple, verbose: bool = False) -> dict:
    """Read the book's topic table into {(year, qnum, letter): topic_string}.

    Uses fitz.find_tables(). Each topic row has, per year, an MC column then a
    Paper 02 column; only the Paper 02 column is used. Rows whose first cell is
    a header ('TOPIC', blank) are skipped.
    """
    ref_to_topic: dict[tuple, str] = {}
    for pno in topic_pages:
        if pno < 1 or pno > doc.page_count:
            continue
        page = doc[pno - 1]
        try:
            tabs = page.find_tables()
        except Exception:
            continue
        if not tabs.tables:
            continue
        years = TOPIC_PAGE_YEARS.get(pno)
        grid = tabs.tables[0].extract()
        for row in grid:
            cells = [normalize(c or "").replace("\n", " ").strip() for c in row]
            if not cells:
                continue
            topic = cells[0]
            if not topic or topic.upper().startswith("TOPIC") or "Paper 02" in topic:
                continue
            # Columns after the topic alternate MC, Paper02, MC, Paper02, ...
            data = cells[1:]
            if years is None:
                continue
            for idx, yr in enumerate(years):
                p02_col = idx * 2 + 1  # skip the MC column before each Paper 02 column
                if p02_col >= len(data):
                    continue
                for qnum, letter in expand_paper02_cell(data[p02_col]):
                    ref_to_topic[(yr, qnum, letter)] = topic
        if verbose:
            print(f"  topic table p{pno}: {len(grid)} rows parsed")
    return ref_to_topic


# ---------------------------------------------------------------------------
# Topic string -> section_id (content-word overlap; old-syllabus topics -> None)
# ---------------------------------------------------------------------------
def load_sections(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    return [
        dict(r) for r in db.execute(
            "SELECT section_id, title FROM syllabus_sections WHERE subject_id = ?",
            (subject_id,),
        ).fetchall()
    ]


def match_topic_to_section(topic: str, sections: list[dict]) -> dict | None:
    """Best section by Jaccard of content words; None if below SECTION_MATCH_MIN.

    'Role of Government in the Economy' vs 'ROLE OF GOVERNMENT IN AN ECONOMY'
    both reduce to {role, government, economy} -> Jaccard 1.0. Topics that have
    left the syllabus ('Social Accounting...', 'Regional and Global...') match
    no section above the floor and return None.
    """
    ttoks = tokenize(topic)
    if not ttoks:
        return None
    best, best_j = None, 0.0
    for sec in sections:
        stoks = tokenize(sec["title"])
        if not stoks:
            continue
        j = len(ttoks & stoks) / len(ttoks | stoks)
        if j > best_j:
            best, best_j = sec, j
    return best if best_j >= SECTION_MATCH_MIN else None


# ---------------------------------------------------------------------------
# Answer-page parser: year section -> list of leaf parts
# ---------------------------------------------------------------------------
def _parse_marks_value(rule_line: str) -> int:
    """Marks-per-point from a rule line ('one mark ... each' -> 1, default 1)."""
    m = RULE_MARKS_RE.search(rule_line or "")
    return WORD2NUM.get(m.group(1).lower(), 1) if m else 1


def parse_year_section(doc, year: int, start_page: int, end_page: int) -> list[dict]:
    """Walk a year's Paper 02 pages and return gradeable leaf parts.

    Leaf = the deepest (question, part, sub) that carries an answer. Each leaf:
      {year, qnum, part, sub, label, stem, bullets[list], marks_value, page}
    A leaf with bullets is gradeable; a prose/definition leaf (mark rule but no
    bullets) is returned with bullets == [] so the caller can queue it.

    Robustness (verified in recon): question headers are BARE integers 1-10 (no
    'Question' keyword) on their own line, qualified by an '(a)' appearing within
    ~200 chars AND by sequential numbering (so page numbers like '79' and the
    section-number that trails a running head are rejected).
    """
    # 1) Collect non-noise, non-blank lines (page, text).
    lines: list[tuple[int, str]] = []
    for pno in range(start_page, end_page + 1):
        for raw in normalize(doc[pno - 1].get_text()).split("\n"):
            s = raw.strip()
            if not s:
                continue
            up = s.upper()
            if "PRINCIPLES OF BUSINESS PAPER" in up or "GENERAL PROFICIENCY" in up:
                continue  # running head (its trailing section number is a bare
                          # int handled by the question-qualifier below)
            lines.append((pno, s))

    leaves: list[dict] = []
    expected_q = 1
    cur_q = cur_part = cur_sub = None
    buf: dict | None = None

    def lookahead_has_a(i: int) -> bool:
        acc = ""
        for _p, t in lines[i:]:
            acc += " " + t
            if len(acc) >= 200:
                break
        return "(a)" in acc

    def flush():
        nonlocal buf
        if buf is not None and (buf["bullets"] or buf["has_marks"]):
            label = f"{cur_q}({cur_part})" + (f"({cur_sub})" if cur_sub else "")
            stem = " ".join(x for x in buf["stem"] if x).strip()
            leaves.append({
                "year": year, "qnum": cur_q, "part": cur_part, "sub": cur_sub,
                "label": label, "stem": stem,
                "bullets": [b.strip() for b in buf["bullets"] if b.strip()],
                "marks_value": buf["marks_value"], "page": buf["page"],
            })
        buf = None

    def open_buf(page: int):
        nonlocal buf
        buf = {"page": page, "stem": [], "bullets": [], "marks_value": 1,
               "has_marks": False}

    def peel(text: str, page: int):
        """Peel an inline 'qnum (a)(i) text' tail into part/sub + stem."""
        nonlocal cur_part, cur_sub
        pm = PART_RE.match(text)
        if pm:
            flush()
            cur_part, cur_sub = pm.group(1), None
            open_buf(page)
            text = pm.group(2)
        sm = SUB_RE.match(text)
        if sm and buf is not None:
            cur_sub = sm.group(1).lower()
            text = sm.group(2)
        if text and buf is not None:
            buf["stem"].append(text)

    n = len(lines)
    for i in range(n):
        page, s = lines[i]
        nxt = lines[i + 1][1] if i + 1 < n else ""

        qm = QNUM_RE.match(s)
        is_bare_int = bool(re.fullmatch(r"\d+", s))
        # Question header? bare-or-prefixed int 1-10, sequential, with (a) ahead.
        if qm and qm.group(1).isdigit():
            qn = int(qm.group(1))
            rest = qm.group(2)
            qualifies = (1 <= qn <= 10 and qn == expected_q
                         and ("(a)" in rest or lookahead_has_a(i)))
            if qualifies:
                flush()
                cur_q, cur_part, cur_sub = qn, None, None
                expected_q = qn + 1
                buf = None
                if rest:
                    peel(rest, page)
                continue
            if is_bare_int:
                continue  # page number / stray section number -> drop

        if cur_q is None:
            continue  # preamble before the first question of the section

        pm = PART_RE.match(s)
        if pm:
            flush()
            cur_part, cur_sub = pm.group(1), None
            open_buf(page)
            rest = pm.group(2)
            sm = SUB_RE.match(rest)
            if sm:
                cur_sub = sm.group(1).lower()
                rest = sm.group(2)
            if rest:
                buf["stem"].append(rest)
            continue

        sm = SUB_RE.match(s)
        if sm:
            flush()
            cur_sub = sm.group(1).lower()
            open_buf(page)
            if sm.group(2):
                buf["stem"].append(sm.group(2))
            continue

        if MARKS_RE.search(s):
            if buf is not None:
                buf["has_marks"] = True
            continue
        # The line just before a "(N marks)" line is the mark rule, not a bullet.
        if MARKS_RE.search(nxt):
            if buf is not None:
                buf["marks_value"] = _parse_marks_value(s)
            continue

        if s.startswith(BULLET):
            if buf is not None:
                buf["bullets"].append(s[len(BULLET):].strip())
            continue

        # Plain text: a bullet continuation if we're mid-answer, else stem.
        if buf is not None:
            if buf["bullets"] and not buf["has_marks"]:
                buf["bullets"][-1] = (buf["bullets"][-1] + " " + s).strip()
            else:
                buf["stem"].append(s)

    flush()
    return leaves


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------
def new_counts() -> dict:
    return {
        "years": 0, "leaf_parts": 0, "mark_points": 0, "stems_indexed": 0,
        "matched": 0, "queued_low_conf": 0, "queued_topic_failed": 0,
        "queued_topic_not_syllabus": 0, "queued_no_points": 0,
        "docs_written": 0, "skipped_existing": 0,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _queue(db, dry_run, source_file, chunk_text, reason, objective_id=None, doc_id=None):
    if dry_run:
        return
    db.execute(
        "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, "
        "objective_id, doc_id) VALUES (?, ?, ?, ?, ?)",
        (source_file, chunk_text, reason, objective_id, doc_id),
    )


def _section_vectors(db, subject_id, section_id, embed_fn, cache) -> list[tuple]:
    """(objective_id, content_stmt, embedding) for a section, embedded once."""
    if section_id in cache:
        return cache[section_id]
    rows = db.execute(
        "SELECT objective_id, content_stmt FROM objectives "
        "WHERE subject_id = ? AND section_id = ? ORDER BY objective_id",
        (subject_id, section_id),
    ).fetchall()
    cache[section_id] = [(r["objective_id"], r["content_stmt"],
                          embed_fn(r["content_stmt"])) for r in rows]
    return cache[section_id]


def ingest_book(db: sqlite3.Connection, doc, *, subject_id: str, source_file: str,
                ref_to_topic: dict, layout: dict, embed_fn=ollama_embed,
                dry_run: bool = False, min_similarity: float = SIM_THRESHOLD,
                verbose: bool = False) -> dict:
    """Parse the book and write (or, in dry-run, only count) mark_points/chunks.

    ref_to_topic: {(year, qnum, letter): topic_string} from parse_topic_table.
    layout:       {year: (start_page, end_page)} (1-based inclusive).
    Returns the counts dict. Caller commits / closes.
    """
    sections = load_sections(db, subject_id)
    token = subject_token(subject_id)
    sec_cache: dict[str, list] = {}
    counts = new_counts()

    for year in sorted(layout):
        start, end = layout[year]
        leaves = parse_year_section(doc, year, start, end)
        if verbose:
            print(f"  {year}: pages {start}-{end} -> {len(leaves)} leaf parts")
        if not leaves:
            continue

        doc_id = f"{token}-WS-{year}"
        # Per-year content_hash over the year's normalised text (UNIQUE per year).
        ytext = "".join(normalize(doc[p - 1].get_text()) for p in range(start, end + 1))
        chash = hashlib.sha256(f"{doc_id}|{ytext}".encode("utf-8")).hexdigest()
        exists = db.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if exists:
            counts["skipped_existing"] += 1
        elif not dry_run:
            db.execute(
                "INSERT OR IGNORE INTO documents (doc_id, subject_id, content_type, "
                "paper, year, source_file, content_hash) "
                "VALUES (?, ?, 'mark_scheme', 'Paper_02', ?, ?, ?)",
                (doc_id, subject_id, year, source_file, chash),
            )
            counts["docs_written"] += 1
        else:
            counts["docs_written"] += 1

        counts["years"] = max(counts["years"], 0)

        for leaf in leaves:
            counts["leaf_parts"] += 1
            key = (year, leaf["qnum"], leaf["part"])  # leaf inheritance: ignore sub
            topic = ref_to_topic.get(key)

            if topic is None:
                _queue(db, dry_run, source_file,
                       f"{leaf['label']} (no topic-table entry): {leaf['stem'][:280]}",
                       reason="topic_mapping_failed", doc_id=doc_id)
                counts["queued_topic_failed"] += 1
                continue

            section = match_topic_to_section(topic, sections)
            if section is None:
                _queue(db, dry_run, source_file,
                       f"Topic '{topic}' is not in the current syllabus.\n"
                       f"Question: {leaf['label']}\nContext: {leaf['stem'][:200]}",
                       reason="topic_not_in_current_syllabus", doc_id=doc_id)
                counts["queued_topic_not_syllabus"] += 1
                continue

            if not leaf["bullets"]:
                # Prose / definition answer: no discrete bullets to grade.
                _queue(db, dry_run, source_file,
                       f"Section: {section['title']}\nQuestion: {leaf['label']}\n"
                       f"Prose answer, no bullets.\nContext: {leaf['stem'][:200]}",
                       reason="no_mark_points", doc_id=doc_id)
                counts["queued_no_points"] += 1
                continue

            # Hybrid step 3: rank objectives WITHIN the book-chosen section.
            context = (leaf["stem"] + " " + " ".join(leaf["bullets"][:3])).strip()
            sec_vecs = _section_vectors(db, subject_id, section["section_id"],
                                        embed_fn, sec_cache)
            scored = sorted(
                ((oid, stmt, _cosine(embed_fn(context), ovec))
                 for oid, stmt, ovec in sec_vecs),
                key=lambda x: x[2], reverse=True,
            )
            best = scored[0] if scored else None

            if best is None or best[2] < min_similarity:
                cands = "\n".join(
                    f"  {oid}: {stmt} (sim={sim:.2f})" for oid, stmt, sim in scored[:3]
                ) or "  (no objectives in section)"
                _queue(db, dry_run, source_file,
                       f"Section: {section['title']}\nTop candidates (best first):\n"
                       f"{cands}\nQuestion: {leaf['label']}\n"
                       f"Context: {context[:200]}",
                       reason="low_confidence_match", doc_id=doc_id)
                counts["queued_low_conf"] += 1
                continue

            objective_id = best[0]
            counts["matched"] += 1
            chunk_id = (f"{doc_id}-q{leaf['qnum']}{leaf['part']}"
                        + (f"-{leaf['sub']}" if leaf["sub"] else "") + "-stem")
            stem_text = leaf["stem"] or leaf["label"]

            if not dry_run:
                cur = db.execute(
                    "INSERT OR IGNORE INTO chunks (doc_id, objective_id, subject_id, "
                    "chunk_text, page, question_num, chunk_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, objective_id, subject_id, stem_text, leaf["page"],
                     leaf["label"], chunk_id),
                )
                if cur.rowcount:  # newly inserted -> index its stem
                    db.execute(
                        "INSERT OR IGNORE INTO vec_mark_schemes(rowid, embedding) "
                        "VALUES (?, ?)",
                        (cur.lastrowid, serialize_vec(embed_fn(stem_text))),
                    )
                    counts["stems_indexed"] += 1
            else:
                if not db.execute("SELECT 1 FROM chunks WHERE chunk_id = ?",
                                  (chunk_id,)).fetchone():
                    counts["stems_indexed"] += 1

            mp_base = chunk_id[:-len("-stem")]
            for order, bullet in enumerate(leaf["bullets"], 1):
                mp_id = f"{mp_base}-mp{order}"
                if not dry_run:
                    db.execute(
                        "INSERT OR IGNORE INTO mark_points (mark_point_id, objective_id, "
                        "question_id, doc_id, point_text, marks_value, point_order) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (mp_id, objective_id, chunk_id, doc_id, bullet,
                         leaf["marks_value"], order),
                    )
                counts["mark_points"] += 1

        if not dry_run:
            db.commit()

    counts["years"] = len([y for y in layout if layout[y]])
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def print_summary(counts: dict, elapsed: float, dry_run: bool) -> None:
    total = counts["leaf_parts"]
    queued = (counts["queued_low_conf"] + counts["queued_topic_failed"]
              + counts["queued_topic_not_syllabus"] + counts["queued_no_points"])
    print("\n" + "=" * 62)
    print("Worked-solutions ingestion summary" + ("  (DRY RUN -- nothing written)" if dry_run else ""))
    print("=" * 62)
    print(f"  years processed              : {counts['years']}")
    print(f"  documents written            : {counts['docs_written']}"
          + (f"  (skipped existing: {counts['skipped_existing']})" if counts['skipped_existing'] else ""))
    print(f"  leaf parts found             : {total}")
    print(f"  mark_points written          : {counts['mark_points']}")
    print(f"  stems indexed (vec_mark_schemes): {counts['stems_indexed']}")
    print("  -- topic mapping --")
    print(f"    matched (objective written): {counts['matched']}")
    print(f"    queued low-confidence      : {counts['queued_low_conf']}")
    print(f"    queued topic-not-in-table  : {counts['queued_topic_failed']}")
    print(f"    queued topic-left-syllabus : {counts['queued_topic_not_syllabus']}")
    print(f"    queued prose (no bullets)  : {counts['queued_no_points']}")
    pct = (100 * counts["matched"] / total) if total else 0
    print(f"    => matched {counts['matched']}/{total} ({pct:.0f}%), queued {queued}/{total}")
    print(f"  time taken                   : {elapsed:.1f}s")
    print("\nRun python check_mark_points_coverage.py to see updated objective coverage.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest the Macmillan POB Worked Solutions book as a mark scheme.")
    ap.add_argument("--subject", default="Principles_of_Business")
    ap.add_argument("--file", required=True, help="path to the worked-solutions PDF")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse + rank and report counts; write nothing to the DB")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        import fitz  # PyMuPDF
    except ImportError:
        sys.exit("ERROR: PyMuPDF not installed. Run: pip install pymupdf")

    pdf = Path(args.file)
    if not pdf.exists():
        sys.exit(f"ERROR: file not found: {pdf}")

    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    db = open_db(db_path)
    try:
        assert_subject_locked(db, args.subject)  # exits clearly if not locked
        ensure_queue_columns(db)
        doc = fitz.open(str(pdf))
        layout, topic_pages = derive_layout(doc)
        if args.verbose:
            print(f"layout: {layout}")
            print(f"topic table pages: {topic_pages}")
        ref_to_topic = parse_topic_table(doc, topic_pages, verbose=args.verbose)
        if not ref_to_topic:
            sys.exit("ERROR: could not parse the topic table (find_tables found nothing). "
                     "The book structure may differ -- stop and inspect.")
        t0 = time.time()
        counts = ingest_book(
            db, doc, subject_id=args.subject, source_file=rel_source(str(pdf)),
            ref_to_topic=ref_to_topic, layout=layout, dry_run=args.dry_run,
            verbose=args.verbose,
        )
        print_summary(counts, time.time() - t0, args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
