"""
migrate_mark_points_question_id.py
===================================
One-time data migration: adds the '-stem' suffix to mark_points.question_id
rows created by the old PDF-based ingester (before ingest_solutions.py was
rewritten). The new text ingester already stores question_id = chunk_id (which
ends in '-stem'), so this migration makes the convention uniform across all rows.

Safe to run multiple times: the WHERE clause is the idempotency guard.
"""

import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

from backend.db.init_db import open_db

db = open_db(os.getenv("DB_PATH"))

# Count before
before = db.execute(
    "SELECT COUNT(*) as n FROM mark_points WHERE question_id NOT LIKE '%-stem'"
).fetchone()["n"]
print(f"Rows to migrate: {before}")

if before == 0:
    print("Nothing to do — all rows already use the -stem convention.")
else:
    db.execute(
        "UPDATE mark_points SET question_id = question_id || '-stem' "
        "WHERE question_id NOT LIKE '%-stem'"
    )
    db.commit()

    # Verify
    after = db.execute(
        "SELECT COUNT(*) as n FROM mark_points WHERE question_id NOT LIKE '%-stem'"
    ).fetchone()["n"]
    print(f"Rows remaining without -stem: {after}")

    if after == 0:
        print(f"Done. {before} rows migrated successfully.")
    else:
        print(f"WARNING: {after} rows still missing -stem — check manually.")
