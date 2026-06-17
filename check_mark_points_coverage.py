"""
check_mark_points_coverage.py
=============================
One-off helper (repo root, NOT backend/): report how much of each subject's
syllabus has deterministic mark_points to grade against. Run it after any
mark-point ingestion to see coverage move.

  - Per subject: total objectives, objectives with >= 1 mark_point, objectives
    with zero, and percentage covered.
  - The 10 objectives with the MOST mark_points (deepest coverage).
  - 10 objectives with ZERO mark_points (the gaps the Gemini syllabus-grader
    fallback currently has to cover).

mark_points has no subject_id column, so subject scoping joins through
objectives (objectives.objective_id = mark_points.objective_id). Reads DB_PATH
from .env. Read-only.

Usage:
    python check_mark_points_coverage.py                 # all subjects
    python check_mark_points_coverage.py --subject Principles_of_Business
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")


def open_db() -> sqlite3.Connection:
    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path!r}. Check .env DB_PATH.")
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    return db


def subjects(db: sqlite3.Connection, only: str | None) -> list[str]:
    if only:
        return [only]
    return [r["subject_id"] for r in db.execute(
        "SELECT subject_id FROM subjects ORDER BY subject_id").fetchall()]


def coverage(db: sqlite3.Connection, subject_id: str) -> dict:
    total = db.execute(
        "SELECT COUNT(*) n FROM objectives WHERE subject_id = ?", (subject_id,)
    ).fetchone()["n"]
    with_mp = db.execute(
        "SELECT COUNT(DISTINCT m.objective_id) n FROM mark_points m "
        "JOIN objectives o ON o.objective_id = m.objective_id "
        "WHERE o.subject_id = ?", (subject_id,)
    ).fetchone()["n"]
    return {"total": total, "with_mp": with_mp, "zero": total - with_mp,
            "pct": (100 * with_mp / total) if total else 0.0}


def top_objectives(db: sqlite3.Connection, subject_id: str, limit: int = 10) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT o.objective_id, COUNT(m.mark_point_id) cnt, o.content_stmt "
        "FROM objectives o JOIN mark_points m ON m.objective_id = o.objective_id "
        "WHERE o.subject_id = ? "
        "GROUP BY o.objective_id ORDER BY cnt DESC, o.objective_id LIMIT ?",
        (subject_id, limit),
    ).fetchall()


def zero_objectives(db: sqlite3.Connection, subject_id: str, limit: int = 10) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT o.objective_id, o.content_stmt FROM objectives o "
        "LEFT JOIN mark_points m ON m.objective_id = o.objective_id "
        "WHERE o.subject_id = ? AND m.mark_point_id IS NULL "
        "ORDER BY o.objective_id LIMIT ?",
        (subject_id, limit),
    ).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser(description="Report mark_point coverage per objective.")
    ap.add_argument("--subject", help="limit to one subject_id")
    args = ap.parse_args()

    db = open_db()
    try:
        for subject_id in subjects(db, args.subject):
            cov = coverage(db, subject_id)
            print("=" * 70)
            print(f"{subject_id}")
            print("=" * 70)
            print(f"  objectives total          : {cov['total']}")
            print(f"  with >= 1 mark_point       : {cov['with_mp']}")
            print(f"  with zero mark_points      : {cov['zero']}")
            print(f"  coverage                  : {cov['pct']:.1f}%")
            if cov["total"] == 0:
                print()
                continue

            print("\n  deepest coverage (top 10 by mark_point count):")
            for r in top_objectives(db, subject_id):
                print(f"    {r['objective_id']:<10} {r['cnt']:>4}  {r['content_stmt'][:60]}")

            zeros = zero_objectives(db, subject_id)
            if zeros:
                print(f"\n  zero mark_points ({cov['zero']} total; showing up to 10):")
                for r in zeros:
                    print(f"    {r['objective_id']:<10}       {r['content_stmt'][:60]}")
            print()
    finally:
        db.close()


if __name__ == "__main__":
    main()
