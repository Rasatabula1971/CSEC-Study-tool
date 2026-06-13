"""
backend/db/syllabus_parser.py
=============================
Loads a verified syllabus CSV into csec.sqlite: one subject row, the distinct
sections, and every objective. Uses INSERT OR IGNORE throughout, so re-running
is safe and never duplicates or overwrites rows.

Expected CSV columns (produced by extract_syllabus.py, then hand-verified):
    section_id, section_num, section_title, objective_id, objective_num,
    content_stmt, skill_type, command_words, exam_weight

`command_words` in the CSV is a single verb or a pipe-delimited list
("Describe|State"); it is stored in the DB as a JSON array ('["Describe","State"]')
to match the schema's command_words contract.

Usage:
    python backend/db/syllabus_parser.py --subject Principles_of_Business \
        --csv-file "D:\\...\\pob_syllabus_raw.csv"
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

REQUIRED_COLUMNS = {
    "section_id", "section_num", "section_title", "objective_id",
    "objective_num", "content_stmt",
}


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


def display_name(subject_id: str) -> str:
    """Principles_of_Business -> 'Principles of Business'."""
    return subject_id.replace("_", " ")


def command_words_to_json(value: str) -> str:
    """Normalise 'Describe|State' or 'Describe' (or '') to a JSON array string."""
    if not value:
        return "[]"
    parts = [p.strip() for p in value.replace(",", "|").split("|") if p.strip()]
    return json.dumps(parts)


def load_rows(csv_path: Path) -> list[dict]:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            sys.exit(
                f"ERROR: CSV is missing required columns: {', '.join(sorted(missing))}\n"
                f"Found columns: {', '.join(reader.fieldnames or [])}"
            )
        rows = [r for r in reader if (r.get("objective_id") or "").strip()]
    return rows


def insert_syllabus(db: sqlite3.Connection, subject_id: str,
                    rows: list[dict]) -> tuple[int, int, int]:
    """Insert subject, sections and objectives with INSERT OR IGNORE.

    Returns (subjects_inserted, sections_inserted, objectives_inserted) where each
    count is the number of *new* rows actually written this run.
    """
    cursor = db.cursor()

    cursor.execute(
        "INSERT OR IGNORE INTO subjects (subject_id, display_name) VALUES (?, ?)",
        (subject_id, display_name(subject_id)),
    )
    subjects_inserted = cursor.rowcount if cursor.rowcount > 0 else 0

    sections_inserted = 0
    seen_sections: set[str] = set()
    for r in rows:
        section_id = (r["section_id"] or "").strip()
        if not section_id or section_id in seen_sections:
            continue
        seen_sections.add(section_id)
        cursor.execute(
            "INSERT OR IGNORE INTO syllabus_sections "
            "(section_id, subject_id, title, section_num) VALUES (?, ?, ?, ?)",
            (
                section_id,
                subject_id,
                (r.get("section_title") or "").strip(),
                (r.get("section_num") or "").strip(),
            ),
        )
        sections_inserted += cursor.rowcount if cursor.rowcount > 0 else 0

    objectives_inserted = 0
    for r in rows:
        cursor.execute(
            "INSERT OR IGNORE INTO objectives "
            "(objective_id, section_id, subject_id, objective_num, content_stmt, "
            " skill_type, command_words, exam_weight) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (r["objective_id"] or "").strip(),
                (r["section_id"] or "").strip(),
                subject_id,
                (r.get("objective_num") or "").strip(),
                (r.get("content_stmt") or "").strip(),
                (r.get("skill_type") or "").strip() or None,
                command_words_to_json((r.get("command_words") or "").strip()),
                (r.get("exam_weight") or "").strip() or None,
            ),
        )
        objectives_inserted += cursor.rowcount if cursor.rowcount > 0 else 0

    db.commit()
    return subjects_inserted, sections_inserted, objectives_inserted


def main() -> None:
    ap = argparse.ArgumentParser(description="Load a verified syllabus CSV into the DB.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--csv-file", required=True, help="Path to the verified syllabus CSV")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    csv_path = Path(args.csv_file)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    rows = load_rows(csv_path)
    if not rows:
        sys.exit("ERROR: CSV contained no objective rows.")

    db = open_db(db_path)
    try:
        subj_n, sec_n, obj_n = insert_syllabus(db, args.subject, rows)
        total_sections = len({(r["section_id"] or "").strip() for r in rows})
        total_objectives = len(rows)
    finally:
        db.close()

    print(f"Subject           : {args.subject} ({'inserted' if subj_n else 'already present'})")
    print(f"Sections inserted : {sec_n}  (of {total_sections} in CSV)")
    print(f"Objectives inserted: {obj_n}  (of {total_objectives} in CSV)")
    if sec_n < total_sections or obj_n < total_objectives:
        print("Note: rows not inserted already existed (INSERT OR IGNORE) — safe re-run.")
    print("\nNext: python backend/db/export_for_review.py --subject", args.subject)


if __name__ == "__main__":
    main()
