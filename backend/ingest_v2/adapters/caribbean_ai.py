# PHASE: build
"""
backend/ingest_v2/adapters/caribbean_ai.py
==========================================
Adapter for Caribbean AI lesson markdown (.md with YAML front-matter).

The front-matter is authoritative: it names the syllabus section and the
objective numbers a lesson covers, so objective mapping is HIGH confidence (no
keyword guessing). Required keys: ``syllabus_section`` (int|str) and
``syllabus_objectives`` (list[int]).

Output: one ``notes`` chunk record per (chunk, objective). An objective number
that does not resolve to a real locked objective is sent to review (Rule 1).
"""

from pathlib import Path
from typing import Iterable

import frontmatter

from backend.ingest_v2.adapters.base import BaseAdapter, IngestRecord, sha256_file
from backend.ingest_v2.chunking import chunk_paragraphs
from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.normalize import normalize_text
from backend.ingest_v2.objective_index import ObjectiveIndex


class CaribbeanAIAdapter(BaseAdapter):
    source_family = "caribbean_ai"

    def matches(self, path: Path) -> bool:
        if path.suffix.lower() != ".md":
            return False
        parts = set(path.parts)
        return "Caribbean AI" in parts or "Caribbean_AI_Textbooks" in parts

    def extract(self, path: Path, manifest: SubjectManifest,
                objective_index: ObjectiveIndex) -> Iterable[IngestRecord]:
        chash = sha256_file(path)
        subject_id = manifest.subject_id

        def _review(reason: str, objective_id=None, text=None) -> IngestRecord:
            return IngestRecord(
                objective_id=objective_id or "REVIEW", subject_id=subject_id,
                source_family=self.source_family, content_type="notes",
                source_file=str(path), content_hash=chash, page=None,
                chunk_text=text, mcq_payload=None,
                confidence="review", review_reason=reason,
            )

        try:
            post = frontmatter.load(str(path))
        except Exception as e:  # malformed front-matter / unreadable file
            yield _review(f"caribbean_ai_frontmatter_parse_error: {e}")
            return

        meta = post.metadata or {}
        section = meta.get("syllabus_section")
        objectives = meta.get("syllabus_objectives")
        if section is None or not isinstance(objectives, list) or not objectives:
            yield _review("caribbean_ai_missing_frontmatter")
            return

        # Resolve every objective number to a canonical id; invalid ones -> review.
        valid_ids: list[str] = []
        for obj_num in objectives:
            oid = objective_index.build_objective_id(section, obj_num)
            if oid in objective_index.all_objective_ids():
                valid_ids.append(oid)
            else:
                yield _review("objective_id_not_in_syllabus", objective_id=oid)

        if not valid_ids:
            return

        body = normalize_text(post.content)
        chunks = chunk_paragraphs(body)
        if not chunks:
            yield _review("caribbean_ai_empty_body")
            return

        # Many-to-many: the same chunk binds to every objective the lesson covers.
        for chunk in chunks:
            for oid in valid_ids:
                yield IngestRecord(
                    objective_id=oid, subject_id=subject_id,
                    source_family=self.source_family, content_type="notes",
                    source_file=str(path), content_hash=chash, page=None,
                    chunk_text=chunk, mcq_payload=None,
                    confidence="high", review_reason=None,
                )
