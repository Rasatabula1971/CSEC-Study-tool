# PHASE: build
"""
backend/ingest_v2/adapters/markdown_notes.py
============================================
Opt-in adapter for hand-authored markdown notes (.md) that no specialised adapter
claimed -- e.g. Integrated Science's "Supplemental Gap Sources", "Miss Francois",
"KCR Science", and Collins mark-scheme .md files. Without it those notes are
silently dropped: GenericPDFAdapter only claims .pdf, and CaribbeanAIAdapter only
claims .md under a "Caribbean AI" folder.

It reuses the generic_pdf machinery verbatim -- v1.chunk_page (same 500/100
overlapping windows) and ObjectiveIndex.resolve_by_keyword (keyword overlap vs
objectives.content_stmt): matched -> confidence "medium", unmatched -> "review".
content_type is read from the parent folder with GenericPDFAdapter's exact mapping,
so a .md under "Mark Schemes" is a mark_scheme, under "Notes" is notes, etc. There
is NO new mapping logic and NO markdown-specific processing -- the raw .md text is
chunked exactly as PDF page text is.

OPT-IN, like the Office adapter: wired ONLY when manifest.enable_markdown_adapter is
True, so POB/Economics dispatch is unchanged (test_pob_parity untouched -- v1 never
ingested .md, so a subject that leaves the flag False produces no .md chunks).

Excluded: auto-generated index/coverage artifacts (leading-underscore names, e.g.
_integrated-science_lesson_coverage.md) -- confirmed build artifacts (each opens with
a "Generated: <timestamp>" line), not lesson content. And .md under a "Caribbean AI"
folder, which CaribbeanAIAdapter (registered earlier) owns.
"""

import sys
from pathlib import Path
from typing import Iterable

from backend.ingest_v2.adapters.base import BaseAdapter, IngestRecord, sha256_file
from backend.ingest_v2.adapters.generic_pdf import GenericPDFAdapter
from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.objective_index import ObjectiveIndex

# backend/ on path for the bare v1 import (same hook generic_pdf uses).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ingest as v1  # noqa: E402

# Folders whose .md is owned by a more specific adapter (CaribbeanAIAdapter).
_DEFER_FOLDERS = {"Caribbean AI", "Caribbean_AI_Textbooks"}


class MarkdownNotesAdapter(BaseAdapter):
    source_family = "markdown_notes"

    def matches(self, path: Path) -> bool:
        if path.suffix.lower() != ".md":
            return False
        if path.name.startswith("_"):
            return False  # auto-generated index/coverage artifact, not lesson content
        if _DEFER_FOLDERS & set(path.parts):
            return False  # claimed by CaribbeanAIAdapter (registered earlier)
        return True

    def extract(self, path: Path, manifest: SubjectManifest,
                objective_index: ObjectiveIndex) -> Iterable[IngestRecord]:
        chash = sha256_file(path)
        subject_id = manifest.subject_id
        content_type = GenericPDFAdapter.content_type_for(path)

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")

        # A markdown file has no pages; treat the whole file as one page and chunk it
        # exactly as the PDF path chunks page text (v1.chunk_page, 500/100 windows).
        for seq, raw_chunk in enumerate(v1.chunk_page(text)):
            chunk = raw_chunk.strip()
            if not chunk:
                continue
            oid, _score = objective_index.resolve_by_keyword(chunk)
            if oid is None:
                yield IngestRecord(
                    objective_id="REVIEW", subject_id=subject_id,
                    source_family=self.source_family, content_type=content_type,
                    source_file=str(path), content_hash=chash, page=1,
                    chunk_text=chunk, mcq_payload=None, confidence="review",
                    review_reason="no_objective_match_via_keywords", chunk_seq=seq,
                )
            else:
                yield IngestRecord(
                    objective_id=oid, subject_id=subject_id,
                    source_family=self.source_family, content_type=content_type,
                    source_file=str(path), content_hash=chash, page=1,
                    chunk_text=chunk, mcq_payload=None, confidence="medium",
                    review_reason=None, chunk_seq=seq,
                )
