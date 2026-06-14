"""
backend/notes.py
================
Welcome-page "Add Study Notes" engine. The student pastes or uploads material and
the system decides where it belongs:

  1. classify_notes() -- the LLM picks the subject (schema-constrained JSON), then
     a deterministic cosine-similarity pass ranks the subject's objective
     content_stmts to suggest the top matching objective_ids. The LLM only chooses
     the subject; objective ranking is pure Python (CLAUDE.md "Deterministic vs LLM").

  2. save_notes() -- once the student confirms a subject + objective, the text is
     chunked with the SAME logic as ingest.py, embedded, and indexed into
     chunks + vec_notes under the confirmed objective_id. A documents row with
     content_type='notes' anchors the chunks. Every indexed chunk carries a real
     objectives.objective_id FK (Rule 1).

chat_fn / embed_fn are injectable so this module is testable without Ollama.
"""

import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from ingest import chunk_page, index_chunk  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

# The classifier must answer with exactly these fields (Ollama `format` schema).
CLASSIFY_SCHEMA = {
    "type": "object",
    "required": ["subject_id", "confidence", "reasoning"],
    "properties": {
        "subject_id": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reasoning": {"type": "string"},
    },
}

# How many objective suggestions classify_notes returns.
TOP_K_OBJECTIVES = 3

# Process-lifetime cache of objective embeddings so a classify call doesn't
# re-embed all ~116 objective statements every time. Keyed by (subject_id, the
# exact set of objective_ids) so the cache self-invalidates if objectives change.
_OBJ_EMBED_CACHE: dict[tuple, list[tuple[str, str, list[float]]]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_objectives(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    return [
        dict(r) for r in db.execute(
            "SELECT objective_id, content_stmt FROM objectives WHERE subject_id = ? "
            "ORDER BY objective_id",
            (subject_id,),
        ).fetchall()
    ]


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is a zero vector."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _objective_embeddings(db: sqlite3.Connection, subject_id: str,
                          embed_fn) -> list[tuple[str, str, list[float]]]:
    """(objective_id, content_stmt, embedding) for a subject, cached per process."""
    objs = _load_objectives(db, subject_id)
    key = (subject_id, tuple(o["objective_id"] for o in objs))
    cached = _OBJ_EMBED_CACHE.get(key)
    if cached is not None:
        return cached
    embedded = [
        (o["objective_id"], o["content_stmt"], embed_fn(o["content_stmt"]))
        for o in objs
    ]
    _OBJ_EMBED_CACHE[key] = embedded
    return embedded


# ---------------------------------------------------------------------------
# 1. Classify
# ---------------------------------------------------------------------------
def classify_notes(db: sqlite3.Connection, text: str,
                   available_subjects: list[str],
                   chat_fn=ollama_chat, embed_fn=ollama_embed) -> dict:
    """Decide which subject + objectives a note excerpt belongs to.

    Returns:
        {
          "subject_id": str | None,
          "confidence": "high" | "medium" | "low",
          "reasoning":  str,
          "suggested_objectives": [
            {"objective_id", "content_stmt", "similarity"}, ...  # top 3, may be []
          ],
        }
    A subject the LLM names that isn't in `available_subjects` is treated as no
    match (subject_id -> None) -- the UI then falls back to manual selection.
    """
    excerpt = (text or "")[:2000]
    prompt = (PROMPTS_DIR / "classify_notes.txt").read_text(encoding="utf-8")
    prompt = prompt.replace("[SUBJECTS]", ", ".join(available_subjects))
    prompt = prompt.replace("[TEXT]", excerpt)

    raw = chat_fn([{"role": "user", "content": excerpt}], system=prompt,
                  schema=CLASSIFY_SCHEMA)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = {}

    subject_id = parsed.get("subject_id")
    confidence = parsed.get("confidence", "low")
    reasoning = parsed.get("reasoning", "")

    # Guard: only accept a subject that is actually offered/locked.
    if subject_id not in available_subjects:
        subject_id = None

    result = {
        "subject_id": subject_id,
        "confidence": confidence,
        "reasoning": reasoning,
        "suggested_objectives": [],
    }
    if subject_id is None:
        return result

    qvec = embed_fn(excerpt)
    scored = [
        {"objective_id": oid, "content_stmt": stmt,
         "similarity": round(_cosine(qvec, ovec), 2)}
        for oid, stmt, ovec in _objective_embeddings(db, subject_id, embed_fn)
    ]
    scored.sort(key=lambda s: s["similarity"], reverse=True)
    result["suggested_objectives"] = scored[:TOP_K_OBJECTIVES]
    return result


# ---------------------------------------------------------------------------
# 2. Save
# ---------------------------------------------------------------------------
def save_notes(db: sqlite3.Connection, subject_id: str, objective_id: str,
               text: str, source_file: str = "pasted_notes",
               embed_fn=ollama_embed) -> dict:
    """Chunk, embed, and index note text under a confirmed subject + objective.

    Creates a documents row (content_type='notes') and one chunks + vec_notes row
    per chunk, every chunk FK'd to objective_id. Returns
    {doc_id, chunks_created, objective_id}. Raises ValueError if the objective is
    not real / not in the subject (no chunk is ever indexed unmapped, Rule 1).
    """
    if not (text or "").strip():
        raise ValueError("No note text to save.")

    obj = db.execute(
        "SELECT 1 FROM objectives WHERE objective_id = ? AND subject_id = ?",
        (objective_id, subject_id),
    ).fetchone()
    if obj is None:
        raise ValueError(
            f"objective_id '{objective_id}' is not an objective of '{subject_id}'."
        )

    # content_hash is salted with the timestamp because notes are a living feature
    # -- the same passage may legitimately be added more than once over time, so we
    # don't dedup on text the way ingest.py dedups whole source PDFs.
    stamp = datetime.now().isoformat()
    content_hash = hashlib.sha256(
        f"{objective_id}|{stamp}|{text}".encode("utf-8")
    ).hexdigest()
    doc_id = f"notes-{content_hash[:12]}"

    db.execute(
        "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
        "source_file, content_hash) VALUES (?, ?, 'notes', NULL, NULL, ?, ?)",
        (doc_id, subject_id, source_file, content_hash),
    )

    chunks_created = 0
    for idx, ctext in enumerate(chunk_page(text)):
        ctext = ctext.strip()
        if not ctext:
            continue
        chunk_id = f"{doc_id}-c{idx}"
        cur = db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, "
            "page, question_num, chunk_id) VALUES (?, ?, ?, ?, NULL, NULL, ?)",
            (doc_id, objective_id, subject_id, ctext, chunk_id),
        )
        index_chunk(db, cur.lastrowid, embed_fn(ctext), "vec_notes")
        chunks_created += 1

    db.commit()
    return {"doc_id": doc_id, "chunks_created": chunks_created,
            "objective_id": objective_id}
