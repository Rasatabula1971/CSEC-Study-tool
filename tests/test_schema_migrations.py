"""
tests/test_schema_migrations.py
===============================
Stage 14 tests for the version-tracked migration ledger in backend/app.py.

Covers _run_migration (applies + records a new version, no-ops an applied one,
records a 'duplicate column' ALTER as [pre-existing]) and the idempotency of
apply_runtime_migrations (running it twice leaves schema_migrations unchanged).

Real in-memory SQLite with the canonical schema + sqlite-vec loaded.

Run: pytest tests/test_schema_migrations.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"

import app as app_module  # noqa: E402  (_run_migration, apply_runtime_migrations)


def open_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping schema_migrations tests")
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            db.execute(stmt)
    db.commit()
    return db


def _count(db, version):
    return db.execute(
        "SELECT COUNT(*) c FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()["c"]


def test_run_migration_applies_and_records():
    """A new version runs its SQL and is recorded exactly once."""
    db = open_db()
    app_module._ensure_schema_migrations(db)

    applied = app_module._run_migration(
        db, "test_001_add_table", "a scratch table",
        "CREATE TABLE scratch (id INTEGER PRIMARY KEY, val TEXT)",
    )

    assert applied is True
    # The SQL really ran -- the table exists and is usable.
    db.execute("INSERT INTO scratch (val) VALUES ('x')")
    assert db.execute("SELECT val FROM scratch").fetchone()["val"] == "x"
    # Recorded once, without the [pre-existing] suffix.
    row = db.execute(
        "SELECT description FROM schema_migrations WHERE version = 'test_001_add_table'"
    ).fetchone()
    assert row["description"] == "a scratch table"
    assert _count(db, "test_001_add_table") == 1
    db.close()


def test_run_migration_noop_when_already_applied():
    """A second call for an applied version does nothing and adds no duplicate row."""
    db = open_db()
    app_module._ensure_schema_migrations(db)
    app_module._run_migration(
        db, "test_002", "scratch", "CREATE TABLE scratch2 (id INTEGER PRIMARY KEY)"
    )

    # Second call: the SQL would fail (table already exists) if it ran, so a no-op
    # return both proves it skipped AND that no duplicate ledger row was written.
    applied_again = app_module._run_migration(
        db, "test_002", "scratch", "CREATE TABLE scratch2 (id INTEGER PRIMARY KEY)"
    )

    assert applied_again is False
    assert _count(db, "test_002") == 1
    db.close()


def test_run_migration_records_duplicate_column_as_pre_existing():
    """An ALTER whose column already exists is recorded [pre-existing], not raised."""
    db = open_db()
    app_module._ensure_schema_migrations(db)
    # Pre-add the column as if an old try/except migration already ran it.
    db.execute("ALTER TABLE mark_points ADD COLUMN scratch_col TEXT")
    db.commit()

    applied = app_module._run_migration(
        db, "test_003_dup_col", "mark_points.scratch_col",
        "ALTER TABLE mark_points ADD COLUMN scratch_col TEXT",
    )

    assert applied is True
    row = db.execute(
        "SELECT description FROM schema_migrations WHERE version = 'test_003_dup_col'"
    ).fetchone()
    assert row["description"].endswith("[pre-existing]")
    assert _count(db, "test_003_dup_col") == 1
    db.close()


def test_run_migration_reraises_unexpected_error():
    """A non-duplicate-column error propagates instead of being silently recorded."""
    db = open_db()
    app_module._ensure_schema_migrations(db)

    with pytest.raises(sqlite3.OperationalError):
        app_module._run_migration(
            db, "test_004_bad_sql", "syntax error",
            "CREATE TABLE (this is not valid sql",
        )
    # Nothing recorded -- the migration did not succeed.
    assert _count(db, "test_004_bad_sql") == 0
    db.close()


def test_apply_runtime_migrations_is_idempotent():
    """Running the full migration pass twice leaves schema_migrations unchanged."""
    db = open_db()

    app_module.apply_runtime_migrations(db)
    count_after_first = db.execute(
        "SELECT COUNT(*) c FROM schema_migrations"
    ).fetchone()["c"]

    app_module.apply_runtime_migrations(db)
    count_after_second = db.execute(
        "SELECT COUNT(*) c FROM schema_migrations"
    ).fetchone()["c"]

    assert count_after_first > 0
    assert count_after_first == count_after_second
    db.close()
