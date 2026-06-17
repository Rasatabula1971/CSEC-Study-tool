"""
tests/test_backup.py
====================
Stage 14 (backup hardening) tests for backend/db/backup.py.

Covers backup_database (file creation, missing-SSD guard, 30-file rolling prune)
and the backup_first decorator (backup runs before the wrapped function; a failed
backup aborts the function). No SSD or live DB required -- everything points at a
tempdir, and env vars are set per-test so backup_database reads them at call time.

Run: pytest tests/test_backup.py -v
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from db import backup as backup_mod  # noqa: E402
from db.backup import backup_database, backup_first  # noqa: E402


def _make_fake_ssd(tmp_path):
    """Lay out a tempdir as a stand-in SSD: a DB file + the 07_BACKUPS folder.
    Returns (ssd_root, db_path, backup_dir)."""
    ssd_root = tmp_path / "ssd"
    db_path = ssd_root / "02_DATABASE" / "csec.sqlite"
    backup_dir = ssd_root / "07_BACKUPS"
    db_path.parent.mkdir(parents=True)
    backup_dir.mkdir(parents=True)
    db_path.write_bytes(b"SQLite format 3\x00 fake db contents")
    return ssd_root, db_path, backup_dir


def test_backup_database_creates_file(tmp_path, monkeypatch):
    """backup_database('test') copies the DB to 07_BACKUPS with the label in the name."""
    ssd_root, db_path, backup_dir = _make_fake_ssd(tmp_path)
    monkeypatch.setenv("SSD_ROOT", str(ssd_root))
    monkeypatch.setenv("DB_PATH", str(db_path))

    dest = backup_database("test")

    assert os.path.exists(dest)
    assert Path(dest).parent == backup_dir
    assert Path(dest).name.startswith("csec_")
    assert Path(dest).name.endswith("_test.sqlite")
    # The copy is byte-for-byte identical to the source.
    assert Path(dest).read_bytes() == db_path.read_bytes()


def test_backup_database_raises_when_ssd_missing(tmp_path, monkeypatch):
    """A missing SSD raises RuntimeError -- nothing is copied."""
    ssd_root, db_path, _ = _make_fake_ssd(tmp_path)
    monkeypatch.setenv("SSD_ROOT", str(ssd_root))
    monkeypatch.setenv("DB_PATH", str(db_path))
    # Force the mount check to fail even though the tempdir exists.
    monkeypatch.setattr(backup_mod.os.path, "exists", lambda p: False)

    with pytest.raises(RuntimeError):
        backup_database("test")


def test_prune_keeps_only_30(tmp_path, monkeypatch):
    """With 35 pre-existing backups, a new backup leaves exactly 30 (rolling rule)."""
    ssd_root, db_path, backup_dir = _make_fake_ssd(tmp_path)
    monkeypatch.setenv("SSD_ROOT", str(ssd_root))
    monkeypatch.setenv("DB_PATH", str(db_path))

    # 35 dummy backups with staggered mtimes so "most recent" is well-defined.
    for i in range(35):
        f = backup_dir / f"csec_2026-01-01_0000{i:02d}_dummy.sqlite"
        f.write_bytes(b"old")
        os.utime(f, (1000 + i, 1000 + i))

    backup_database("fresh")  # adds one more (36), then prunes back to 30

    remaining = list(backup_dir.glob("csec_*.sqlite"))
    assert len(remaining) == 30
    # The just-written backup (newest mtime) must survive the prune.
    assert any(p.name.endswith("_fresh.sqlite") for p in remaining)


def test_backup_first_runs_backup_before_function(tmp_path, monkeypatch):
    """The decorator calls backup_database before the wrapped function body runs."""
    order = []
    monkeypatch.setattr(backup_mod, "backup_database",
                        lambda label: order.append(f"backup:{label}"))

    @backup_first("pre_thing")
    def do_work():
        order.append("work")
        return "done"

    result = do_work()

    assert result == "done"
    assert order == ["backup:pre_thing", "work"]


def test_backup_first_aborts_function_when_backup_fails(tmp_path, monkeypatch):
    """If the backup raises, the wrapped function never runs and the error propagates."""
    ran = {"work": False}

    def failing_backup(label):
        raise RuntimeError("SSD not mounted")

    monkeypatch.setattr(backup_mod, "backup_database", failing_backup)

    @backup_first("pre_thing")
    def do_work():
        ran["work"] = True

    with pytest.raises(RuntimeError):
        do_work()

    assert ran["work"] is False
