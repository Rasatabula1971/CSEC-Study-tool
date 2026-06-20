"""
tests/test_builder_console.py
=============================
UI overhaul session 3: the Builder console + its PIN gate and reset action.

GET /builder serves the console, which is gated client-side by the session-2 PIN
modal (reused). The real backend contract this test pins down: the PIN endpoint
gates (wrong -> false, correct -> true) and the reset action flips
welcome_message_seen back to '0' (re-arming the first-launch message).

Run: pytest tests/test_builder_console.py -v
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
        pytest.skip("sqlite-vec not installed -- skipping builder-console tests")
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
    app_module.app.state.db = conn
    return TestClient(app_module.app)


def test_builder_page_served_with_pin_gate():
    html = _client().get("/builder").text
    # The console serves a PIN gate (blocks until a correct PIN unlocks it) and the
    # builder utility links + reset action.
    assert 'id="gate"' in html
    assert '/api/builder/verify-pin' in html
    assert '/upload' in html
    assert '/lessons/status' in html
    assert '/api/state/welcome-reset' in html


def test_pin_endpoint_gates(monkeypatch):
    monkeypatch.setenv("BUILDER_PIN", "1971")
    client = _client()
    assert client.post("/api/builder/verify-pin", json={"pin": "0000"}).json() == {"ok": False}
    assert client.post("/api/builder/verify-pin", json={"pin": "1971"}).json() == {"ok": True}


def test_reset_welcome_flips_flag():
    client = _client()
    # Mark seen, then the builder reset flips it back to unseen.
    client.post("/api/state/welcome-seen")
    assert client.get("/api/state/welcome-seen").json() == {"seen": True}
    assert client.post("/api/state/welcome-reset").json() == {"ok": True}
    assert client.get("/api/state/welcome-seen").json() == {"seen": False}
