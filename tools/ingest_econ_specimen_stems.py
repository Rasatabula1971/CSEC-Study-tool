"""
tools/ingest_econ_specimen_stems.py
====================================
PHASE: build

Stage 4 of the mark scheme pipeline — wire-up for Economics.

Creates one documents row for the Specimen 1 paper (2016) and 24 -stem chunk
rows, one per locked question_id in mark_points for blocks 1-6 (Specimen 1,
pages 90-97 of csec-economics-syllabus-revised-2017.pdf).

After this script runs, /api/questions (quiz picker) and /api/questions/Economics
(grade-mode picker) return Economics questions alongside POB.

Usage:
    python tools/ingest_econ_specimen_stems.py
    python tools/ingest_econ_specimen_stems.py --dry-run

Idempotent: re-running with the same data leaves the DB unchanged.
"""

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
load_dotenv(_REPO_ROOT / ".env")

from backend.app import open_db, apply_runtime_migrations
from backend.db.backup import backup_first


# ── Specimen 1 document metadata ──────────────────────────────────────────────

SUBJECT_ID   = "Economics"
CONTENT_TYPE = "specimen"
PAPER        = "Specimen Paper - 2016"
YEAR         = 2016
SOURCE_FILE  = (
    r"D:\GPT Folder CSEC\Organized_CSEC_2027\Economics\Syllabus"
    r"\csec-economics-syllabus-revised-2017.pdf"
)
PAGE_RANGE   = "90-97"  # 1-indexed PDF pages

# ── Stem text per question_id (post-backfill format: ends in -stem) ───────────
# Q1-Q5 reconstructed from pages 90-97 of the Economics syllabus specimen mark
# scheme. The question prompts themselves are not on those pages (which
# contain only the mark scheme); those stems are inferred from the mark
# scheme answers and the S.O. codes to give Rylee enough context to practice.
# Q6 (added after the block-realignment below) instead uses the REAL
# question-paper prompt text extracted from pages 73-84 of the same PDF via
# tools/ingest_econ_specimen_questions.py -- not a mark-scheme-inferred
# paraphrase, since the genuine prompt text is directly available for it.
#
# Block assignment (current, post block-realignment -- see
# MARK_SCHEME_BUILD_PLAN.md and tools/fix_econ_q6_block_realignment.py):
# qb1-qb4 = Questions 1-4, unchanged. qb5 = ALL of Question 5, including the
# "Question 5 cont'd" content the extractor originally miscounted as a new
# block (now correctly merged under question_block_id=5 in the review CSV).
# qb6 = the REAL Question 6 (S.O: 6.9, 6.11, 6.12, 6.14, 6.15) -- the
# extractor originally routed this to question_block_id=7 because the bogus
# "Question 5 cont'd" match had already consumed block_id 6; that has been
# corrected too, so qb6 ids here now genuinely mean Question 6. Question 6(d)
# is confirmed absent from the source mark scheme -- no entry for it exists,
# intentionally.

STEM_TEXTS: dict[str, dict] = {
    # ── Q1  S.O: 1.6, 1.8 — Production Possibility Curve ───────────────────
    "ECON-qb1(a)(i)v1-stem": {
        "question_num": "1(a)(i)",
        "text": (
            "Question 1 — Production Possibility Curve (S.O: 1.6, 1.8)\n\n"
            "(a)(i) Define the term 'production possibility curve'. (1 mark)"
        ),
    },
    "ECON-qb1(b)(i)v1-stem": {
        "question_num": "1(b)(i)",
        "text": (
            "Question 1 — Production Possibility Curve (S.O: 1.6, 1.8)\n\n"
            "(b)(i) Using a production possibility curve (PPC), identify the "
            "type of opportunity cost illustrated when moving along the curve. "
            "(1 mark)"
        ),
    },
    "ECON-qb1(c)(i)v1-stem": {
        "question_num": "1(c)(i)",
        "text": (
            "Question 1 — Production Possibility Curve (S.O: 1.6, 1.8)\n\n"
            "(c)(i) A country's Production Possibility Curve shows possible "
            "combinations of sugar and bananas. The economy is operating at "
            "2,000 tonnes of sugar and 15,000 tonnes of bananas. Explain, "
            "with reference to the PPC, whether or not the country is operating "
            "efficiently. (4 marks)"
        ),
    },
    "ECON-qb1(d)v1-stem": {
        "question_num": "1(d)",
        "text": (
            "Question 1 — Production Possibility Curve (S.O: 1.6, 1.8)\n\n"
            "(d) You are considering opening an internet café. Identify and "
            "explain FOUR factors you would consider before deciding to set up "
            "this business. (6 marks)"
        ),
    },
    # ── Q2  S.O: 2.3, 2.7, 2.12, 2.17 — Production, Costs, Economies of Scale
    "ECON-qb2(a)(i)v1-stem": {
        "question_num": "2(a)(i)",
        "text": (
            "Question 2 — Production, Costs and Economies of Scale "
            "(S.O: 2.3, 2.7, 2.12, 2.17)\n\n"
            "(a)(i) Define the term 'economic system'. (2 marks)"
        ),
    },
    "ECON-qb2(b)v1-stem": {
        "question_num": "2(b)",
        "text": (
            "Question 2 — Production, Costs and Economies of Scale "
            "(S.O: 2.3, 2.7, 2.12, 2.17)\n\n"
            "(b) Give ONE example of capital equipment used in production. "
            "(1 mark)"
        ),
    },
    "ECON-qb2(c)v1-stem": {
        "question_num": "2(c)",
        "text": (
            "Question 2 — Production, Costs and Economies of Scale "
            "(S.O: 2.3, 2.7, 2.12, 2.17)\n\n"
            "(c) A small food stall holder is expanding into a restaurant "
            "business. Using TWO examples, explain the economies of scale that "
            "this person could benefit from as a result of this expansion. "
            "(6 marks)"
        ),
    },
    "ECON-qb2(d)(i)v1-stem": {
        "question_num": "2(d)(i)",
        "text": (
            "Question 2 — Production, Costs and Economies of Scale "
            "(S.O: 2.3, 2.7, 2.12, 2.17)\n\n"
            "(d)(i) The total cost (TC) of producing 3 units of output is $45. "
            "Calculate the Average Total Cost (ATC) at this level of output. "
            "Show all working. (2 marks)"
        ),
    },
    # ── Q3  S.O: 6.1, 6.7, 6.9, 6.16 — Exchange Rates and Trade ────────────
    "ECON-qb3(a)(i)v1-stem": {
        "question_num": "3(a)(i)",
        "text": (
            "Question 3 — Exchange Rates and International Trade "
            "(S.O: 6.1, 6.7, 6.9, 6.16)\n\n"
            "(a)(i) Define the term 'revaluation' as it applies to a fixed "
            "exchange rate system. (2 marks)"
        ),
    },
    "ECON-qb3(b)v1-stem": {
        "question_num": "3(b)",
        "text": (
            "Question 3 — Exchange Rates and International Trade "
            "(S.O: 6.1, 6.7, 6.9, 6.16)\n\n"
            "(b) State ONE protectionist measure that a government could use "
            "to protect its domestic industries from foreign competition. "
            "(1 mark)"
        ),
    },
    "ECON-qb3(c)v1-stem": {
        "question_num": "3(c)",
        "text": (
            "Question 3 — Exchange Rates and International Trade "
            "(S.O: 6.1, 6.7, 6.9, 6.16)\n\n"
            "(c) Outline TWO disadvantages of a floating exchange rate system "
            "for firms engaged in international trade. (6 marks)"
        ),
    },
    "ECON-qb3(d)v1-stem": {
        "question_num": "3(d)",
        "text": (
            "Question 3 — Exchange Rates and International Trade "
            "(S.O: 6.1, 6.7, 6.9, 6.16)\n\n"
            "(d) Explain ONE benefit of devaluation for a country's balance "
            "of trade position. (4 marks)"
        ),
    },
    # ── Q4  S.O: 5.2, 5.5 — Economic Goals and GDP ──────────────────────────
    "ECON-qb4(a)v1-stem": {
        "question_num": "4(a)",
        "text": (
            "Question 4 — Economic Goals and GDP (S.O: 5.2, 5.5)\n\n"
            "(a) Define the term 'economic goals'. (2 marks)"
        ),
    },
    "ECON-qb4(b)v1-stem": {
        "question_num": "4(b)",
        "text": (
            "Question 4 — Economic Goals and GDP (S.O: 5.2, 5.5)\n\n"
            "(b) List THREE economic goals that a government typically aims "
            "to achieve. (3 marks)"
        ),
    },
    "ECON-qb4(c)v1-stem": {
        "question_num": "4(c)",
        "text": (
            "Question 4 — Economic Goals and GDP (S.O: 5.2, 5.5)\n\n"
            "(c) Explain TWO limitations of using Gross Domestic Product (GDP) "
            "as a measure of the standard of living. (6 marks)"
        ),
    },
    "ECON-qb4(d)v1-stem": {
        "question_num": "4(d)",
        "text": (
            "Question 4 — Economic Goals and GDP (S.O: 5.2, 5.5)\n\n"
            "(d) Using the expenditure approach, calculate the GDP given the "
            "following data. Show all working.\n"
            "  Consumption (C) = $900m\n"
            "  Investment (I) = $500m\n"
            "  Government Expenditure (G) = $300m\n"
            "  Exports (X) = $300m\n"
            "  Imports (M) = $400m\n"
            "(4 marks)"
        ),
    },
    # ── Q5  S.O: 4.4, 4.6, 4.10 — Financial Sector ─────────────────────────
    "ECON-qb5(a)(i)v1-stem": {
        "question_num": "5(a)(i)",
        "text": (
            "Question 5 — The Financial Sector (S.O: 4.4, 4.6, 4.10)\n\n"
            "(a)(i) Define the term 'gold standard'. (2 marks)"
        ),
    },
    "ECON-qb5(b)v1-stem": {
        "question_num": "5(b)",
        "text": (
            "Question 5 — The Financial Sector (S.O: 4.4, 4.6, 4.10)\n\n"
            "(b) List THREE types of financial institutions found in the "
            "Caribbean. (3 marks)"
        ),
    },
    "ECON-qb5(c)v1-stem": {
        "question_num": "5(c)",
        "text": (
            "Question 5 — The Financial Sector (S.O: 4.4, 4.6, 4.10)\n\n"
            "(c) Explain ONE positive contribution of the informal financial "
            "sector to the economy. In your answer, provide a clear analysis "
            "of how this benefits the broader economy. (4 marks)"
        ),
    },
    # ── Q5 cont'd (S.O: 4.4, 4.6, 4.10) — now merged under question_block_id=5;
    #    the "qb" number in this id is a legacy string from before the block
    #    realignment and no longer reflects question_block_id directly (see
    #    the block-assignment note above).
    "ECON-qb5(d)v1-stem": {
        "question_num": "5(d)",
        "text": (
            "Question 5 cont'd — The Financial Sector (S.O: 4.4, 4.6, 4.10)\n\n"
            "(d) State and explain TWO advantages of online/electronic banking "
            "for consumers. (5 marks)"
        ),
    },
    # ── Q6  S.O: 6.9, 6.11, 6.12, 6.14, 6.15 — The Balance of Payments Account
    #    Real question-paper text (pages 73-84), not mark-scheme-inferred
    #    paraphrase -- verified via tools/ingest_econ_specimen_questions.py's
    #    PDF extraction. "ECON-qb6(c)v1-stem" previously carried question_num
    #    "5(c)-2" (a stale id from before the block realignment, when this
    #    content sat under the bogus "Question 5 cont'd" block); it has been
    #    corrected here to resolve to the real Q6(c) content. Question 6(d)
    #    is confirmed absent from the source mark scheme -- intentionally no
    #    entry for it.
    "ECON-qb6(a)v1-stem": {
        "question_num": "6(a)",
        "text": (
            "Question 6 — The Balance of Payments Account "
            "(S.O: 6.9, 6.11, 6.12, 6.14, 6.15)\n\n"
            "(a) List THREE examples of transfers found in the current account "
            "section of the balance of payments. (3 marks)"
        ),
    },
    "ECON-qb6(b)(i)v1-stem": {
        "question_num": "6(b)(i)",
        "text": (
            "Question 6 — The Balance of Payments Account "
            "(S.O: 6.9, 6.11, 6.12, 6.14, 6.15)\n\n"
            "Define the following term as it relates to a country's balance "
            "of payments account:\n\n(i) Investment income. (2 marks)"
        ),
    },
    "ECON-qb6(b)(ii)v1-stem": {
        "question_num": "6(b)(ii)",
        "text": (
            "Question 6 — The Balance of Payments Account "
            "(S.O: 6.9, 6.11, 6.12, 6.14, 6.15)\n\n"
            "Define the following term as it relates to a country's balance "
            "of payments account:\n\n(ii) Balance of payments disequilibrium. "
            "(2 marks)"
        ),
    },
    "ECON-qb6(c)v1-stem": {
        "question_num": "6(c)",
        "text": (
            "Question 6 — The Balance of Payments Account "
            "(S.O: 6.9, 6.11, 6.12, 6.14, 6.15)\n\n"
            "(c) Explain TWO ways in which increases in the exchange rate of "
            "a country can affect the balance of payments account. (8 marks)"
        ),
    },
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _doc_id_for_specimen() -> str:
    key = f"{SUBJECT_ID}:{CONTENT_TYPE}:{SOURCE_FILE}:2016-specimen-1"
    return f"specimen-{hashlib.sha1(key.encode()).hexdigest()[:12]}"


def _content_hash_for_specimen() -> str:
    key = f"econ-specimen-1-2016-stems-{':'.join(sorted(STEM_TEXTS.keys()))}"
    return hashlib.sha256(key.encode()).hexdigest()


def ingest_specimen_stems(db: sqlite3.Connection, dry_run: bool = False) -> int:
    """Create the specimen document row and 21 -stem chunk rows.

    Returns the number of chunk rows written (0 on dry-run).
    Raises ValueError if any question_id in STEM_TEXTS has no corresponding
    mark_points rows (FK safety check).
    """
    # Check all question_ids exist in mark_points after backfill
    missing = [
        qid for qid in STEM_TEXTS
        if not db.execute(
            "SELECT 1 FROM mark_points WHERE question_id = ? LIMIT 1", (qid,)
        ).fetchone()
    ]
    if missing:
        raise ValueError(
            f"{len(missing)} question_id(s) have no matching mark_points rows:\n"
            + "\n".join(f"  {q}" for q in sorted(missing))
        )

    # Confirm no existing document with this content_hash
    doc_id       = _doc_id_for_specimen()
    content_hash = _content_hash_for_specimen()

    existing_doc = db.execute(
        "SELECT doc_id FROM documents WHERE content_hash = ?", (content_hash,)
    ).fetchone()

    if dry_run:
        print(f"[dry-run] Would create document row: {doc_id!r}")
        for qid, meta in sorted(STEM_TEXTS.items()):
            existing_chunk = db.execute(
                "SELECT chunk_id FROM chunks WHERE chunk_id = ?", (qid,)
            ).fetchone()
            status = "ALREADY EXISTS" if existing_chunk else "would insert"
            print(f"  {status}: {qid!r}  Q{meta['question_num']}")
        return 0

    # Create/update the documents row
    if existing_doc:
        doc_id = existing_doc["doc_id"]
        print(f"Specimen document already exists: {doc_id!r}")
    else:
        db.execute(
            """
            INSERT INTO documents
                (doc_id, subject_id, content_type, paper, year, source_file, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (doc_id, SUBJECT_ID, CONTENT_TYPE, PAPER, YEAR, SOURCE_FILE, content_hash),
        )
        print(f"Created specimen document: {doc_id!r}  ({PAPER})")

    # For each question_id, get the primary objective from mark_points
    written = 0
    for qid in sorted(STEM_TEXTS.keys()):
        meta = STEM_TEXTS[qid]
        q_num = meta["question_num"]
        text  = meta["text"]

        # Primary objective: alphabetically first objective_id for this question_id
        obj_row = db.execute(
            "SELECT MIN(objective_id) AS obj FROM mark_points WHERE question_id = ?",
            (qid,),
        ).fetchone()
        if not obj_row or not obj_row["obj"]:
            print(f"  WARNING: no mark_points found for {qid!r} — skipping")
            continue
        objective_id = obj_row["obj"]

        existing = db.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id = ?", (qid,)
        ).fetchone()
        if existing:
            print(f"  SKIP (already exists): {qid!r}")
            continue

        db.execute(
            """
            INSERT INTO chunks
                (doc_id, objective_id, subject_id, chunk_text, page,
                 question_num, chunk_id, source_family)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id,
                objective_id,
                SUBJECT_ID,
                text,
                None,       # page is not meaningful for reconstructed stems
                q_num,
                qid,
                "specimen",
            ),
        )
        written += 1
        print(f"  inserted: {qid!r}  Q{q_num}")

    db.commit()
    print(f"\n{'[dry-run] ' if dry_run else ''}Written {written} stem chunk(s).")
    return written


# ── CLI ────────────────────────────────────────────────────────────────────────

@backup_first("pre_ingest_econ_specimen_stems")
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ingest Specimen 1 Economics question stems as -stem chunks."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate and preview without writing to the DB")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")

    db = open_db(db_path)
    apply_runtime_migrations(db)   # ensures ECON question_ids have -stem suffix

    try:
        written = ingest_specimen_stems(db, dry_run=args.dry_run)
    finally:
        db.close()

    if not args.dry_run:
        print(f"\nDone. {written} new stem chunks added.")
        print("Run /api/questions?subject_id=Economics to verify quiz picker.")


if __name__ == "__main__":
    main()
