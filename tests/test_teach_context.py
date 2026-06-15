"""
tests/test_teach_context.py
===========================
Tests for controller._objective_context() source enrichment.

When an objective has no ingested notes, the teach context must NOT collapse to a
bare four-word content statement. The resolution order is:
    notes  ->  any other chunk (mark_scheme > past_paper > specimen)  ->  enriched
    syllabus context (section title + objective + skill type + command words).
Each result carries a `context_source` tag for the UI.

All DB access is mocked (a tiny FakeDB dispatches on the SQL text), so these tests
need no SQLite schema and no Ollama.

Run: pytest tests/test_teach_context.py -v
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import backend.controller as controller  # noqa: E402


# ---------------------------------------------------------------------------
# Mock DB: dispatch each query to a canned row by matching the SQL text.
# Rows are plain dicts -- dict(row) and row["col"] both work, matching sqlite3.Row.
# ---------------------------------------------------------------------------
class FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class FakeDB:
    """Returns notes_row / anychunk_row / objective_row / section_row depending on
    which of _objective_context's queries (or get_objective's) is running."""

    def __init__(self, *, notes=None, anychunk=None, objective=None, section=None):
        self.notes = notes
        self.anychunk = anychunk
        self.objective = objective
        self.section = section
        self.sql_seen = []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())  # normalise whitespace for substring matching
        self.sql_seen.append(s)
        if "d.content_type = 'notes'" in s:
            return FakeCursor(self.notes)
        if "CASE d.content_type" in s:
            return FakeCursor(self.anychunk)
        if "FROM syllabus_sections" in s:
            return FakeCursor(self.section)
        if "FROM objectives WHERE objective_id" in s:
            return FakeCursor(self.objective)
        raise AssertionError(f"unexpected query: {s}")


def notes_row():
    return {
        "objective_id": "POB-1.1",
        "chunk_text": "A business is an organisation that supplies goods/services.",
        "page": 3,
        "source_file": "notes.pdf",
    }


def markscheme_row():
    return {
        "objective_id": "POB-1.1",
        "chunk_text": "Award 1 mark each: organises resources; bears risk.",
        "page": 12,
        "source_file": "POB_Paper2_June2024.txt",
        "content_type": "mark_scheme",
    }


def pastpaper_row():
    return {
        "objective_id": "POB-1.1",
        "chunk_text": "State THREE functions of an entrepreneur.",
        "page": 2,
        "source_file": "june2019_p2.pdf",
        "content_type": "past_paper",
    }


def objective_row():
    return {
        "objective_id": "POB-1.1",
        "section_id": "POB-SEC-1",
        "subject_id": "Principles_of_Business",
        "objective_num": "1.1",
        "content_stmt": "Define the term business.",
        "skill_type": "Knowledge",
        "command_words": '["Define"]',
    }


# ---------------------------------------------------------------------------
# Source preference
# ---------------------------------------------------------------------------
def test_notes_preferred_over_mark_scheme():
    db = FakeDB(notes=notes_row(), anychunk=markscheme_row())
    ctx = controller._objective_context(db, "POB-1.1")
    assert ctx["context_source"] == "notes"
    assert ctx["source_file"] == "notes.pdf"
    assert "supplies goods" in ctx["chunk_text"]
    # The notes hit short-circuits -- the any-chunk / fallback queries never run.
    assert not any("CASE d.content_type" in s for s in db.sql_seen)


def test_mark_scheme_used_when_no_notes():
    db = FakeDB(notes=None, anychunk=markscheme_row())
    ctx = controller._objective_context(db, "POB-1.1")
    assert ctx["context_source"] == "mark_scheme"
    assert ctx["source_file"] == "POB_Paper2_June2024.txt"
    assert "bears risk" in ctx["chunk_text"]
    # content_type is consumed into context_source, not leaked into the context dict.
    assert "content_type" not in ctx


def test_past_paper_used_when_no_notes_or_mark_scheme():
    db = FakeDB(notes=None, anychunk=pastpaper_row())
    ctx = controller._objective_context(db, "POB-1.1")
    assert ctx["context_source"] == "past_paper"
    assert ctx["chunk_text"].startswith("State THREE functions")


# ---------------------------------------------------------------------------
# Enriched syllabus fallback (zero chunks of any type)
# ---------------------------------------------------------------------------
def test_enriched_fallback_includes_section_title_and_metadata():
    db = FakeDB(
        notes=None, anychunk=None,
        objective=objective_row(),
        section={"title": "Nature of Business"},
    )
    ctx = controller._objective_context(db, "POB-1.1")
    assert ctx["context_source"] == "syllabus_only"
    assert ctx["source_file"] == "syllabus"
    text = ctx["chunk_text"]
    assert "Section: Nature of Business" in text          # section title present
    assert "Objective 1.1: Define the term business." in text
    assert "Skill type: Knowledge" in text
    assert "Command words: Define" in text                # JSON array rendered as a list


def test_enriched_fallback_handles_missing_metadata_gracefully():
    obj = objective_row()
    obj["skill_type"] = None
    obj["command_words"] = None
    db = FakeDB(notes=None, anychunk=None, objective=obj, section=None)
    ctx = controller._objective_context(db, "POB-1.1")
    assert ctx["context_source"] == "syllabus_only"
    assert "Section: (unknown section)" in ctx["chunk_text"]
    assert "Skill type: (unspecified)" in ctx["chunk_text"]
    assert "Command words: (none specified)" in ctx["chunk_text"]


def test_unknown_objective_returns_none():
    db = FakeDB(notes=None, anychunk=None, objective=None)
    assert controller._objective_context(db, "POB-9.9") is None


# ---------------------------------------------------------------------------
# Wiring: the teach response surfaces context_source
# ---------------------------------------------------------------------------
def test_teach_response_carries_context_source(monkeypatch):
    monkeypatch.setattr(controller, "subject_is_locked", lambda db, s: True)
    monkeypatch.setattr(controller, "is_in_scope", lambda db, s, o: True)
    monkeypatch.setattr(controller, "_load_prompt", lambda name: "SYSTEM")
    monkeypatch.setattr(controller, "_objective_context", lambda db, oid: {
        "objective_id": oid,
        "chunk_text": "Section: Nature of Business\nObjective 1.1: ...",
        "source_file": "syllabus",
        "page": None,
        "context_source": "syllabus_only",
    })

    out = controller._handle_teach(
        db=None,
        request={"subject_id": "Principles_of_Business",
                 "objective_id": "POB-1.1", "query": "teach me"},
        chat_fn=lambda messages, system: "A lesson.",
        embed_fn=None,
    )
    assert out["route"] == "teach"
    assert out["context_source"] == "syllabus_only"
    assert out["lesson"] == "A lesson."
