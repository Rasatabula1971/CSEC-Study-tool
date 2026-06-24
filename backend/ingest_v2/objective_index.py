# PHASE: build
"""
backend/ingest_v2/objective_index.py
====================================
ObjectiveIndex: the locked syllabus of one subject, loaded once at orchestrator
startup, with the three resolution helpers adapters use to bind content to a real
objective_id.

  * resolve_by_section_objective(section_num, obj_num) -> objective_id | None
        Authoritative structural lookup. Builds the canonical
        ``{PREFIX}-{section}.{obj}`` id and returns it only if it exists in the
        locked syllabus. Used by the Caribbean AI and MoE SLMS adapters, which
        learn (section, objective) from front-matter / filenames.

  * resolve_by_keyword(text) -> (objective_id | None, score)
        Keyword-overlap fallback, delegating to v1 ingest.best_objective so the
        GenericPDFAdapter is byte-equivalent to v1. score is the shared-content-word
        count as a float; None when below v1's MIN_KEYWORD_OVERLAP threshold.

  * all_objective_ids() -> set[str]
        The full set of valid objective_ids, for cheap membership validation
        (Rule 1: a record whose objective_id is not in this set is sent to review).
"""

import sqlite3
import sys
from pathlib import Path

# backend/ on path so the bare v1 import below resolves regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ingest as v1_ingest  # noqa: E402  -- v1 keyword matcher, reused for parity

from backend.ingest_v2.subject_prefix import prefix_for


class ObjectiveIndex:
    """In-memory view of one subject's locked objectives + resolution helpers."""

    def __init__(self, db: sqlite3.Connection, subject_id: str):
        self.subject_id = subject_id
        self.prefix = prefix_for(subject_id)
        # Objective rows for keyword matching. match_text = content_stmt + parent
        # section/topic title (v1_ingest.build_match_text), so a chunk that names the
        # topic but not the terse content_stmt still reaches the threshold. LEFT JOIN
        # keeps an objective whose section row is missing (match_text -> content_stmt).
        self._objectives = [
            {
                "objective_id": r["objective_id"],
                "content_stmt": r["content_stmt"],
                "match_text": v1_ingest.build_match_text(r["content_stmt"], r["section_title"]),
            }
            for r in db.execute(
                "SELECT o.objective_id, o.content_stmt, s.title AS section_title "
                "FROM objectives o "
                "LEFT JOIN syllabus_sections s ON s.section_id = o.section_id "
                "WHERE o.subject_id = ?",
                (subject_id,),
            ).fetchall()
        ]
        self._ids = {o["objective_id"] for o in self._objectives}

    # --- structural resolution -------------------------------------------
    def resolve_by_section_objective(self, section_num, obj_num) -> str | None:
        """Return the canonical objective_id for (section, objective), or None if
        no such objective exists in the locked syllabus."""
        candidate = f"{self.prefix}-{section_num}.{obj_num}"
        return candidate if candidate in self._ids else None

    def build_objective_id(self, section_num, obj_num) -> str:
        """Construct the canonical id WITHOUT validating membership. Adapters use
        this to report what they *tried* to map to when validation then fails, so a
        review record can name the bad id."""
        return f"{self.prefix}-{section_num}.{obj_num}"

    # --- keyword resolution (v1 parity) ----------------------------------
    def resolve_by_keyword(self, text: str) -> tuple[str | None, float]:
        """(objective_id | None, score). Delegates to v1 ingest.best_objective so
        keyword behaviour and the MIN_KEYWORD_OVERLAP threshold match v1 exactly."""
        obj_id, score = v1_ingest.best_objective(text, self._objectives)
        return obj_id, float(score)

    # --- membership -------------------------------------------------------
    def all_objective_ids(self) -> set[str]:
        return set(self._ids)

    def __contains__(self, objective_id: str) -> bool:
        return objective_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)
