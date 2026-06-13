"""
backend/retrieval.py
====================
Structured-first, semantic-fallback retrieval (CLAUDE.md "Retrieval Order").

  1. If the request carries an exact key (subject_id, paper, year, question_num),
     do a SQLite WHERE on chunks/documents -- no embedding call.
  2. Otherwise embed request["query"] (keep_alive=0 via ollama_embed) and search
     the correct vec_* table filtered by subject_id, joining back to chunks.

Every result carries objective_id + source_file + page so traceability (VAL-08)
is real. Returns None when nothing matches.

`_structured_lookup` and `_semantic_lookup` are module-level so they can be
swapped/observed in tests; `embed_fn` is injectable so tests never hit Ollama.
"""

import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_embed  # noqa: E402

# Free-text queries default to notes; callers may override via request["content_type"].
CONTENT_TYPE_VEC_TABLE = {
    "notes": "vec_notes",
    "past_paper": "vec_past_papers",
    "specimen": "vec_past_papers",
    "mark_scheme": "vec_mark_schemes",
}
DEFAULT_CONTENT_TYPE = "notes"

STRUCTURED_KEYS = ("subject_id", "paper", "year", "question_num")


def serialize_vec(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def has_structured_key(request: dict) -> bool:
    """True when every exact-lookup key is present and non-empty."""
    return all(request.get(k) not in (None, "") for k in STRUCTURED_KEYS)


def _structured_lookup(db: sqlite3.Connection, request: dict) -> dict | None:
    """Exact lookup by (subject_id, paper, year, question_num). No embedding."""
    row = db.execute(
        """
        SELECT c.objective_id, c.chunk_text, c.page, d.source_file
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id   = ?
          AND  d.paper        = ?
          AND  d.year         = ?
          AND  c.question_num = ?
        LIMIT  1
        """,
        (
            request["subject_id"],
            request["paper"],
            request["year"],
            request["question_num"],
        ),
    ).fetchone()
    return dict(row) if row is not None else None


def _semantic_lookup(db: sqlite3.Connection, request: dict,
                     embed_fn=ollama_embed, k: int = 5) -> dict | None:
    """Embed request['query'] and search the subject-filtered vec table."""
    query = request.get("query")
    if not query:
        return None
    subject_id = request["subject_id"]
    content_type = request.get("content_type", DEFAULT_CONTENT_TYPE)
    table = CONTENT_TYPE_VEC_TABLE.get(content_type, CONTENT_TYPE_VEC_TABLE[DEFAULT_CONTENT_TYPE])

    query_vec = serialize_vec(embed_fn(query))
    row = db.execute(
        f"""
        SELECT c.objective_id, c.chunk_text, c.page, d.source_file, v.distance
        FROM   {table} v
        JOIN   chunks c    ON c.id = v.rowid
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  v.embedding MATCH ?
          AND  v.rowid IN (SELECT id FROM chunks WHERE subject_id = ?)
        ORDER  BY v.distance
        LIMIT  ?
        """,
        (query_vec, subject_id, k),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result.pop("distance", None)
    return result


def get_context(db: sqlite3.Connection, request: dict, embed_fn=ollama_embed) -> dict | None:
    """Return {objective_id, chunk_text, source_file, page} for a request, or None.

    Structured lookup is used when all of (subject_id, paper, year, question_num)
    are present; otherwise the semantic fallback runs. No embedding call is made
    on the structured path.
    """
    if has_structured_key(request):
        return _structured_lookup(db, request)
    return _semantic_lookup(db, request, embed_fn=embed_fn)
