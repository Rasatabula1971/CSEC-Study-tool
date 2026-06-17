# PHASE: build
"""
backend/db/init_db.py
=====================
Creates the SSD folder structure and initialises csec.sqlite.

Usage:
    python backend/db/init_db.py

Reads SSD_ROOT and DB_PATH from .env in the repo root.
Safe to re-run: CREATE TABLE IF NOT EXISTS means nothing is overwritten.
"""

import os
import sqlite3
import struct
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root (works whether called from root or backend/db/)
load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

SUBJECTS = [
    "Principles_of_Business",
    "Economics",
    "Mathematics",
    "English",
    "Principles_of_Accounts",
    "Integrated_Science",
    "Information_Technology",
]

SUBJECT_SUBFOLDERS = [
    "00_SYLLABUS",
    "01_SPECIMEN_PAPERS",
    "02_PAST_PAPERS",
    "03_MARK_SCHEMES",
    "04_NOTES",
    "05_STUDENT_WORK",
]

TOP_LEVEL_FOLDERS = [
    "01_MODELS/Ollama",
    "02_DATABASE",
    "03_KNOWLEDGE_BASE",
    "04_REPORTS",
    "07_BACKUPS",
]


def get_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        print(f"ERROR: {key} is not set in .env")
        sys.exit(1)
    return val


def create_folder_structure(ssd_root: Path) -> list[str]:
    """Create all required folders on the SSD. Returns list of created paths."""
    created = []

    # Top-level folders
    for folder in TOP_LEVEL_FOLDERS:
        p = ssd_root / folder
        if not p.exists():
            p.mkdir(parents=True)
            created.append(str(p))

    # Per-subject knowledge base subfolders
    kb_root = ssd_root / "03_KNOWLEDGE_BASE"
    for subject in SUBJECTS:
        for subfolder in SUBJECT_SUBFOLDERS:
            p = kb_root / subject / subfolder
            if not p.exists():
                p.mkdir(parents=True)
                created.append(str(p))

    return created


def open_db(db_path: str) -> sqlite3.Connection:
    """Open the database with sqlite-vec loaded and foreign keys enabled."""
    try:
        import sqlite_vec
    except ImportError:
        print("ERROR: sqlite-vec is not installed.")
        print("Run: pip install sqlite-vec")
        sys.exit(1)

    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def init_schema(db: sqlite3.Connection, schema_path: Path) -> None:
    """Run schema.sql against the database."""
    sql = schema_path.read_text(encoding="utf-8")
    # Execute each statement individually (sqlite3 doesn't support executescript
    # inside a transaction cleanly with virtual tables)
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt:
            db.execute(stmt)
    db.commit()


def verify_schema(db: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Return (regular_tables, virtual_tables) found in the database."""
    rows = db.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table') ORDER BY name"
    ).fetchall()
    regular = [r["name"] for r in rows if not r["name"].startswith("vec_")]
    virtual_rows = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'vec_%'"
    ).fetchall()
    virtual = [r["name"] for r in virtual_rows]
    return regular, virtual


def main():
    ssd_root = Path(get_env("SSD_ROOT"))
    db_path  = get_env("DB_PATH")

    print("=" * 60)
    print("CSEC AI Study Partner — Stage 1 Initialisation")
    print("=" * 60)
    print(f"SSD root : {ssd_root}")
    print(f"DB path  : {db_path}")
    print()

    # 1. Check SSD is mounted
    if not ssd_root.exists():
        print(
            f"ERROR: SSD not found at {ssd_root}\n"
            "  • Plug in the external SSD, or\n"
            "  • Update SSD_ROOT in .env to the correct drive letter."
        )
        sys.exit(1)

    # 2. Create folder structure
    print("Creating folder structure...")
    created = create_folder_structure(ssd_root)
    if created:
        for p in created:
            print(f"  + {p}")
    else:
        print("  (all folders already exist)")

    # 3. Initialise database
    print(f"\nInitialising database at {db_path} ...")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = open_db(db_path)

    schema_path = Path(__file__).parent / "schema.sql"
    if not schema_path.exists():
        print(f"ERROR: schema.sql not found at {schema_path}")
        sys.exit(1)

    init_schema(db, schema_path)
    regular, virtual = verify_schema(db)
    db.close()

    print(f"  Regular tables  : {', '.join(regular)}")
    print(f"  Virtual tables  : {', '.join(virtual) if virtual else '(none yet — loaded at runtime)'}")

    # 4. Summary
    print()
    print("=" * 60)
    print("SUCCESS")
    print("=" * 60)
    print(f"  Folders created : {len(created)}")
    print(f"  Database        : {db_path}")
    print(f"  Regular tables  : {len(regular)}")
    print()
    print("Next steps:")
    print("  1. Run the backup:  launch\\backup.bat")
    print("  2. Run the tests:   pytest tests/test_schema.py -v")
    print("  3. Proceed to Stage 2: syllabus_parser.py")


if __name__ == "__main__":
    main()
