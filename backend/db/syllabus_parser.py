# PHASE: build
"""
backend/db/syllabus_parser.py
=============================
Loads a verified syllabus into csec.sqlite: one subject row, the distinct
sections, and every objective. Uses INSERT OR IGNORE throughout, so re-running
is safe and never duplicates or overwrites rows.

Accepts either a CSV (produced by extract_syllabus.py, then hand-verified) or a
JSON file (e.g. one pulled from Google Drive). Both are normalised to the same
row shape before insertion, so the FK guarantees and idempotency are identical.

Expected CSV columns:
    section_id, section_num, section_title, objective_id, objective_num,
    content_stmt, skill_type, command_words, exam_weight

Accepted JSON shapes (field names are matched leniently — see _objective_row):
    1. Nested:  {"subject_id": "...", "sections": [
                    {"section_id": "POB-SEC-1", "section_num": "1",
                     "title": "The Nature of Business",
                     "objectives": [
                         {"objective_id": "POB-1.1", "objective_num": "1.1",
                          "content_stmt": "...", "skill_type": "Knowledge",
                          "command_words": ["Define"], "exam_weight": "P1"}]}]}
    2. Flat list:    [ {section_id, section_title, objective_id, content_stmt, ...}, ... ]
    3. Flat object:  {"objectives": [ {section_id, objective_id, ...}, ... ]}

`command_words` may be a single verb, a pipe/comma-delimited string
("Describe|State"), or a JSON array (["Describe", "State"]); it is always stored
in the DB as a JSON array ('["Describe","State"]') per the schema contract.

Usage:
    python backend/db/syllabus_parser.py --subject Principles_of_Business \
        --file "D:\\...\\pob_syllabus_raw.csv"
    python backend/db/syllabus_parser.py --subject Principles_of_Business \
        --file "D:\\...\\pob_syllabus.json"
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

# backend/db on sys.path so `from backup import backup_first` resolves whether this
# is run as `python backend/db/syllabus_parser.py` or imported in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backup import backup_first  # noqa: E402

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


# ---------------------------------------------------------------------------
# JSON normalisation (Drive-sourced syllabi)
# ---------------------------------------------------------------------------

def _first(d: dict, *keys: str, default: str = "") -> str:
    """Return the first present, non-empty value among `keys`, as a string."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return default


def _command_words_str(value) -> str:
    """Coerce a JSON command_words value (list | str | None) to a pipe string.

    The pipe string is what command_words_to_json() already understands, so the
    CSV and JSON paths converge on a single normaliser.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _objective_row(obj: dict, section: dict) -> dict:
    """Build one canonical row dict from an objective and its (maybe same) section.

    For flat shapes the objective dict carries its own section fields, so callers
    pass the same dict as both `obj` and `section`.
    """
    return {
        "section_id":    _first(section, "section_id", "sectionId", "id"),
        "section_num":   _first(section, "section_num", "section_number", "sectionNum", "num"),
        "section_title": _first(section, "section_title", "title", "name", "sectionTitle"),
        "objective_id":  _first(obj, "objective_id", "objectiveId", "id"),
        "objective_num": _first(obj, "objective_num", "objective_number", "objectiveNum", "num"),
        "content_stmt":  _first(obj, "content_stmt", "content", "statement", "text", "objective"),
        "skill_type":    _first(obj, "skill_type", "skill", "skillType"),
        "command_words": _command_words_str(
            obj.get("command_words", obj.get("commandWords", obj.get("command")))
        ),
        "exam_weight":   _first(obj, "exam_weight", "weight", "paper", "examWeight"),
    }


def rows_from_json_obj(data) -> list[dict]:
    """Normalise a parsed JSON document (any accepted shape) to canonical rows."""
    rows: list[dict] = []

    def add_section(section: dict) -> None:
        for o in section.get("objectives") or []:
            rows.append(_objective_row(o, section))

    if isinstance(data, dict) and isinstance(data.get("sections"), list):
        for section in data["sections"]:
            add_section(section)
    elif isinstance(data, dict) and isinstance(data.get("objectives"), list):
        for o in data["objectives"]:
            rows.append(_objective_row(o, o))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("objectives"), list):
                add_section(item)          # list of sections
            elif isinstance(item, dict):
                rows.append(_objective_row(item, item))  # flat list of objectives
    else:
        raise ValueError(
            "Unrecognised JSON shape: expected a list of objectives, a list of "
            "sections, or an object with a 'sections' or 'objectives' array."
        )

    return [r for r in rows if r["objective_id"]]


def load_json_rows(json_path: Path) -> list[dict]:
    """Read a JSON syllabus file and return canonical rows, validating required fields."""
    with json_path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    rows = rows_from_json_obj(data)
    if not rows:
        sys.exit(
            "ERROR: JSON contained no objective rows with an objective_id.\n"
            "Check the shape — see the accepted shapes in this file's docstring."
        )
    bad = [
        r["objective_id"] for r in rows
        if not r["section_id"] or not r["content_stmt"]
    ]
    if bad:
        sys.exit(
            "ERROR: these objectives are missing a section_id or content_stmt "
            "(both are required by the schema): " + ", ".join(bad[:20])
            + (" ..." if len(bad) > 20 else "")
        )
    return rows


def coerce_rows(path: Path) -> list[dict]:
    """Load rows from either a .json or .csv file, dispatching on extension."""
    if path.suffix.lower() == ".json":
        return load_json_rows(path)
    return load_rows(path)


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


@backup_first("pre_syllabus_parse")
def main() -> None:
    ap = argparse.ArgumentParser(description="Load a verified syllabus (CSV or JSON) into the DB.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--file", help="Path to the verified syllabus CSV or JSON")
    ap.add_argument("--csv-file", help="(alias for --file) path to the verified syllabus CSV")
    args = ap.parse_args()

    src = args.file or args.csv_file
    if not src:
        sys.exit("ERROR: provide --file pointing at a syllabus CSV or JSON.")

    db_path = os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    in_path = Path(src)
    if not in_path.exists():
        sys.exit(f"ERROR: syllabus file not found: {in_path}")

    rows = coerce_rows(in_path)
    if not rows:
        sys.exit("ERROR: syllabus file contained no objective rows.")

    db = open_db(db_path)
    try:
        subj_n, sec_n, obj_n = insert_syllabus(db, args.subject, rows)
        total_sections = len({(r["section_id"] or "").strip() for r in rows})
        total_objectives = len(rows)
    finally:
        db.close()

    print(f"Subject           : {args.subject} ({'inserted' if subj_n else 'already present'})")
    print(f"Sections inserted : {sec_n}  (of {total_sections} in {in_path.suffix.lstrip('.').upper() or 'file'})")
    print(f"Objectives inserted: {obj_n}  (of {total_objectives} in {in_path.suffix.lstrip('.').upper() or 'file'})")
    if sec_n < total_sections or obj_n < total_objectives:
        print("Note: rows not inserted already existed (INSERT OR IGNORE) — safe re-run.")
    print("\nNext: python backend/db/export_for_review.py --subject", args.subject)


if __name__ == "__main__":
    main()
