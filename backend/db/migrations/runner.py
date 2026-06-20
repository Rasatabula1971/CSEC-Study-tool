# PHASE: build
"""
backend/db/migrations/runner.py
===============================
Standalone, file-based migration runner for the ingest_v2 framework.

Why a separate runner? The live runtime migrations (m001-m017) live inline in
``app.py::apply_runtime_migrations`` and run on every server startup. The
ingest_v2 schema change (m018) is build-time infrastructure that must NOT be
auto-applied to the live DB on startup -- the builder applies it manually after
review. So m018 lives as a ``.sql`` file applied by this runner.

It reuses the SAME ``schema_migrations`` ledger as app.py, so once m018 is
applied (here, or by a future wiring into startup), it is detected as already
applied and never re-runs. The version recorded for ``m018_mcq_questions.sql``
is ``m018_mcq_questions``.

Idempotency mirrors app.py::_run_migration:
  * a version already in schema_migrations -> skip (returns "already_applied").
  * each statement runs individually; an ALTER that hits 'duplicate column name'
    (the column was already added by a partial earlier run) is tolerated so the
    rest of the migration still completes and records.

Usage:
    python -m backend.db.migrations.runner --version m018_mcq_questions
    python -m backend.db.migrations.runner --version m018_mcq_questions --db-path C:\tmp\temp.sqlite
    python -m backend.db.migrations.runner --version m018_mcq_questions --status

--db-path (alias --db) targets a specific DB without reading DB_PATH from .env, so a
migration can be applied to a temp copy while the live DB is left untouched.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[3] / ".env")

MIGRATIONS_DIR = Path(__file__).resolve().parent

# Human-readable descriptions recorded alongside the version in schema_migrations.
DESCRIPTIONS = {
    "m018_mcq_questions": "mcq_questions table + chunks.source_family (ingest_v2)",
}


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open a DB with sqlite-vec loaded and FKs on (same pattern as app.open_db).

    sqlite-vec is loaded so the migration runner can operate on a DB whose
    vec0 virtual tables would otherwise fail to open. FKs are enabled so the
    new table's REFERENCES are real."""
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


def _ensure_schema_migrations(db: sqlite3.Connection) -> None:
    """Bootstrap the migration ledger if a fresh DB has never run app.py's
    startup migrations. CREATE TABLE IF NOT EXISTS makes this a no-op when the
    ledger already exists (the common case on the live DB)."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  TEXT DEFAULT (datetime('now'))
        )
        """
    )
    db.commit()


def is_applied(db: sqlite3.Connection, version: str) -> bool:
    """True if `version` (or its '[pre-existing]' variant) is recorded."""
    _ensure_schema_migrations(db)
    row = db.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    return row is not None


def migration_path(version: str) -> Path:
    """Resolve a version id to its .sql file. Raises FileNotFoundError if absent."""
    path = MIGRATIONS_DIR / f"{version}.sql"
    if not path.exists():
        raise FileNotFoundError(f"migration file not found: {path}")
    return path


def apply_migration(db: sqlite3.Connection, version: str,
                    description: str | None = None) -> str:
    """Apply the migration `version` from its .sql file exactly once.

    Returns one of:
      "already_applied" -- version was already in schema_migrations, nothing done.
      "applied"         -- the SQL ran and the version row was recorded.

    The function is idempotent and safe to call repeatedly. An ALTER ADD COLUMN
    that hits 'duplicate column name' (a partial earlier run already added it) is
    tolerated per-statement so the migration still completes and records."""
    _ensure_schema_migrations(db)
    if is_applied(db, version):
        return "already_applied"

    sql = migration_path(version).read_text(encoding="utf-8")
    description = description or DESCRIPTIONS.get(version, version)

    try:
        # Strip line comments BEFORE splitting on ';'. A literal ';' inside a
        # comment (e.g. "Additive; existing rows get NULL.") would otherwise
        # break the naive split mid-statement -- the exact failure CLAUDE.md
        # warns about. Stripping comments lets the .sql file carry natural prose.
        for statement in _strip_sql_comments(sql).split(";"):
            s = statement.strip()
            if not s:
                continue
            try:
                db.execute(s)
            except sqlite3.OperationalError as e:
                # A column added by a partial earlier run: tolerate and continue,
                # mirroring app.py::_run_migration's [pre-existing] handling.
                if "duplicate column name" in str(e).lower():
                    continue
                raise
        db.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
            (version, description),
        )
        db.commit()
        return "applied"
    except Exception:
        db.rollback()
        raise


def _strip_sql_comments(sql: str) -> str:
    """Remove '--' line comments so a ';' inside a comment can't break the split.

    Conservative line-level strip: everything from the first '--' to end-of-line
    is dropped. Our migration files never put '--' inside a string literal, so a
    naive find is safe here (and far simpler than a full SQL tokenizer)."""
    out = []
    for line in sql.splitlines():
        idx = line.find("--")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Apply a standalone .sql migration to a DB (idempotent)."
    )
    ap.add_argument("--version", required=True,
                    help="migration id, e.g. m018_mcq_questions")
    ap.add_argument("--db", "--db-path", dest="db",
                    help="path to the DB to migrate (defaults to DB_PATH in .env). "
                         "Use this to target a temp copy without touching the live DB.")
    ap.add_argument("--status", action="store_true",
                    help="report whether the migration is already applied; do not apply")
    args = ap.parse_args()

    db_path = args.db or os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: no DB path. Pass --db or set DB_PATH in .env.")
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}.")

    db = _open_db(db_path)
    try:
        if args.status:
            state = "applied" if is_applied(db, args.version) else "not applied"
            print(f"{args.version}: {state} (db={db_path})")
            return
        result = apply_migration(db, args.version)
        print(f"{args.version}: {result} (db={db_path})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
