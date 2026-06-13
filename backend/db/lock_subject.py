"""
backend/db/lock_subject.py
==========================
Sets syllabus_locked = 1 for a subject AFTER its objectives have been manually
verified against the real CXC syllabus (see export_for_review.py).

The locked flag is the gate that scope.py / is_in_scope() checks before any LLM
or embedding call. Locking is the human sign-off step, so this script asks for an
explicit typed confirmation first. Pass --yes to skip the prompt (e.g. scripted
sign-off only — do NOT use it to bypass actual review).

Usage:
    python backend/db/lock_subject.py --subject Principles_of_Business
    python backend/db/lock_subject.py --subject Principles_of_Business --yes
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Lock a subject's syllabus after sign-off.")
    ap.add_argument("--subject", required=True, help="e.g. Principles_of_Business")
    ap.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    db = open_db(db_path)
    try:
        subject = db.execute(
            "SELECT subject_id, display_name, syllabus_locked "
            "FROM subjects WHERE subject_id = ?",
            (args.subject,),
        ).fetchone()
        if subject is None:
            sys.exit(
                f"ERROR: subject '{args.subject}' is not in the database.\n"
                "Run syllabus_parser.py first."
            )

        obj_count = db.execute(
            "SELECT COUNT(*) AS n FROM objectives WHERE subject_id = ?",
            (args.subject,),
        ).fetchone()["n"]

        if subject["syllabus_locked"] == 1:
            print(f"'{args.subject}' is already locked. Nothing to do.")
            return
        if obj_count == 0:
            sys.exit(
                f"ERROR: '{args.subject}' has no objectives — refusing to lock an "
                "empty syllabus. Run syllabus_parser.py first."
            )

        print(f"Subject     : {subject['display_name']} ({args.subject})")
        print(f"Objectives  : {obj_count}")
        print("Locking sets syllabus_locked = 1 — the subject becomes answerable.")
        print("Only do this AFTER verifying every objective against the CXC PDF.")

        if not args.yes:
            reply = input(f"\nType the subject name to confirm lock: ").strip()
            if reply != args.subject:
                sys.exit("Aborted: confirmation did not match. No changes made.")

        db.execute(
            "UPDATE subjects SET syllabus_locked = 1 WHERE subject_id = ?",
            (args.subject,),
        )
        db.commit()
        print(f"\nLOCKED: '{args.subject}' syllabus_locked = 1.")
        print("The subject is now in scope for tutoring, quizzing and grading.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
