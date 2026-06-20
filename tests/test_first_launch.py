"""
tests/test_first_launch.py
==========================
UI overhaul session 2: the first-launch routing on GET /.

GET / serves first_launch.html until the welcome flag is set (server-side check,
no client flash), then serves the redesigned Welcome page. Uses a real in-memory DB
(schema.sql + migrations) on app.state.db so app_state reads/writes are genuine.

Run: pytest tests/test_first_launch.py -v
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import app as app_module  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"


def _client():
    try:
        import sqlite_vec
    except ImportError:
        pytest.skip("sqlite-vec not installed -- skipping first-launch tests")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA_PATH.read_text(encoding="utf-8").split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    app_module.apply_runtime_migrations(conn)
    conn.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) "
        "VALUES ('Principles_of_Business', 'Principles of Business', 1)"
    )
    conn.commit()
    app_module.app.state.db = conn
    return TestClient(app_module.app)


def test_root_serves_first_launch_when_unseen():
    client = _client()
    res = client.get("/")
    assert res.status_code == 200
    assert "next best thing" in res.text          # first_launch.html copy
    assert "— Dad" in res.text


def test_root_serves_welcome_when_seen():
    client = _client()
    # Mark the flag directly, then GET / must serve the Welcome page.
    client.post("/api/state/welcome-seen")
    res = client.get("/")
    assert res.status_code == 200
    assert "next best thing" not in res.text      # not the first-launch page
    assert "Continue studying" in res.text        # Welcome-page marker


def test_transition_from_first_launch_to_welcome():
    client = _client()
    # Before: first launch.
    assert "next best thing" in client.get("/").text
    # The Continue button POSTs this, then navigates to / again.
    assert client.post("/api/state/welcome-seen").json() == {"ok": True}
    # After: Welcome page, never the message again.
    after = client.get("/")
    assert "next best thing" not in after.text
    assert "Continue studying" in after.text
