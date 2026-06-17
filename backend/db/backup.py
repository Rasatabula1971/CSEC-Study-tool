# PHASE: build
"""
backend/db/backup.py
====================
Stage 14 (backup hardening). A safety net for build-time scripts that mutate the
live SQLite DB (ingestion, derivation, recovery, locking). Before any destructive
run, a timestamped copy of the DB is taken on the SSD so a bad run can always be
rolled back.

This is a build-time module (CLAUDE.md PHASE: build) -- it is never imported or
called on a runtime/student-facing path. Runtime code never mutates the corpus.

Two public names:
  * backup_database(label)  -- copy DB_PATH -> {SSD_ROOT}/07_BACKUPS/csec_{ts}_{label}.sqlite
  * backup_first(label)     -- decorator that runs backup_database(label) before the
                               wrapped function; a failed backup aborts the function.

A rolling rule keeps only the 30 most recent backups; older files are pruned in the
same call so the SSD does not fill up over a long subject-rollout campaign.
"""

import functools
import glob
import os
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

# Keep at most this many backup files in 07_BACKUPS. Older ones are deleted on
# every backup_database() call (rolling retention).
MAX_BACKUPS = 30


def _prune_backups(backup_dir: str, keep: int = MAX_BACKUPS) -> list[str]:
    """Delete all but the `keep` most recent csec_*.sqlite files in backup_dir.

    Recency is by mtime (newest kept). Returns the list of deleted paths. A file
    that vanishes underneath us (concurrent prune) is ignored.
    """
    backups = glob.glob(os.path.join(backup_dir, "csec_*.sqlite"))
    # Newest first, tie-broken by name so the order is deterministic.
    backups.sort(key=lambda p: (os.path.getmtime(p), p), reverse=True)
    deleted: list[str] = []
    for stale in backups[keep:]:
        try:
            os.remove(stale)
            deleted.append(stale)
        except OSError:
            pass  # already gone -- nothing to prune
    return deleted


def backup_database(label: str) -> str:
    """Copy the live SQLite DB to a timestamped file in {SSD_ROOT}/07_BACKUPS.

    The filename encodes the reason: csec_{YYYY-MM-DD_HHMMSS}_{label}.sqlite, so a
    directory listing shows why each backup was taken ('pre_ingest', 'pre_migration',
    'manual', ...). Uses shutil.copy2 to preserve the source mtime.

    Returns the path to the new backup file. Raises RuntimeError if the SSD is not
    mounted, the DB is missing, or the copy fails -- the caller must see the failure
    clearly (see backup_first, which aborts the wrapped function on a raised error).

    After a successful copy the 30-most-recent rolling rule prunes older backups.
    """
    ssd_root = os.getenv("SSD_ROOT")
    if not ssd_root or not os.path.exists(ssd_root):
        raise RuntimeError(
            f"SSD not mounted at {ssd_root!r}. Plug in the drive before running a "
            "destructive build script."
        )

    db_path = os.getenv("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        raise RuntimeError(
            f"Database not found at {db_path!r}. Nothing to back up -- aborting."
        )

    backup_dir = os.path.join(ssd_root, "07_BACKUPS")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(backup_dir, f"csec_{timestamp}_{label}.sqlite")

    try:
        shutil.copy2(db_path, dest)
    except OSError as exc:
        raise RuntimeError(f"Backup copy failed: {exc}") from exc

    _prune_backups(backup_dir)
    return dest


def backup_first(label: str):
    """Decorator: run backup_database(label) before the wrapped function.

    If the backup fails, the wrapped function does NOT run -- the RuntimeError
    propagates so a missing SSD or failed copy stops the destructive operation
    before it can touch the DB.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            backup_database(label)
            return fn(*args, **kwargs)
        return wrapper
    return decorator
