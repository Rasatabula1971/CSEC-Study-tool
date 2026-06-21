# PHASE: build
# PARITY CONTRACT: this adapter intentionally does NOT call normalize_text and uses
# no OCR -- it must stay byte-equivalent to v1 ingest. This is a deliberate contract,
# not an oversight; do not "fix" it to match the other adapters.
"""
backend/ingest_v2/adapters/generic_pdf.py
=========================================
Catch-all adapter for any .pdf no specialised adapter claimed (past papers, mark
schemes, specimen papers, loose notes). This IS the v1 path, ported to the adapter
interface and kept BYTE-EQUIVALENT to v1:

  * text via v1.extract_pdf_pages (PyMuPDF, no OCR -- v1 had none, and adding it
    would create chunks v1 never produced, breaking parity);
  * chunking via v1.chunk_page (same 500/100 windows);
  * objective mapping by keyword overlap (v1.best_objective, via ObjectiveIndex),
    matched -> confidence "medium", unmatched -> review;
  * NO normalize_text (it would alter chunk_text vs v1);
  * each record carries chunk_seq = the exact enumerate(chunk_page) index so the
    orchestrator reproduces v1's chunk_id / mark_point_id.

Mark-scheme award-point extraction is unchanged from v1; it lives in the
orchestrator (run for every mark_scheme chunk) so it is identical across the
generic path and any future mark-scheme source.

content_type is read from the parent folder, recognising BOTH the live numeric KB
skeleton (02_PAST_PAPERS ...) and the new Organized_CSEC_2027 names (Past Papers ...).
"""

import sys
from pathlib import Path
from typing import Iterable

from backend.ingest_v2.adapters.base import BaseAdapter, IngestRecord, sha256_file
from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.objective_index import ObjectiveIndex

# backend/ on path for the bare v1 import.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ingest as v1  # noqa: E402

# Folder name -> content_type, highest specificity first so a 'notes' default never
# pre-empts a recognised paper folder. Covers legacy numeric + new corpus names.
_FOLDER_CONTENT_TYPE = [
    ("03_MARK_SCHEMES", "mark_scheme"),
    ("Mark Schemes", "mark_scheme"),
    ("01_SPECIMEN_PAPERS", "specimen"),
    ("Specimen Papers", "specimen"),
    ("02_PAST_PAPERS", "past_paper"),
    ("Past Papers", "past_paper"),
    ("04_NOTES", "notes"),
    ("Notes", "notes"),
]


class GenericPDFAdapter(BaseAdapter):
    source_family = "generic_pdf"

    def matches(self, path: Path) -> bool:
        # Registered LAST, so this only sees PDFs no other adapter claimed.
        return path.suffix.lower() == ".pdf"

    @staticmethod
    def content_type_for(path: Path) -> str:
        parts = set(path.parts)
        for folder, ctype in _FOLDER_CONTENT_TYPE:
            if folder in parts:
                return ctype
        return "notes"  # default for unrecognised folders (Textbooks, etc.)

    def extract(self, path: Path, manifest: SubjectManifest,
                objective_index: ObjectiveIndex) -> Iterable[IngestRecord]:
        chash = sha256_file(path)
        subject_id = manifest.subject_id
        content_type = self.content_type_for(path)

        for page, text in v1.extract_pdf_pages(path):
            for seq, raw_chunk in enumerate(v1.chunk_page(text)):
                chunk = raw_chunk.strip()
                if not chunk:
                    # v1 skips empty-after-strip chunks but the enumerate index still
                    # advances -- carrying chunk_seq=seq keeps chunk_ids aligned.
                    continue
                oid, _score = objective_index.resolve_by_keyword(chunk)
                if oid is None:
                    yield IngestRecord(
                        objective_id="REVIEW", subject_id=subject_id,
                        source_family=self.source_family, content_type=content_type,
                        source_file=str(path), content_hash=chash, page=page,
                        chunk_text=chunk, mcq_payload=None,
                        confidence="review",
                        review_reason="no_objective_match_via_keywords",
                        chunk_seq=seq,
                    )
                else:
                    yield IngestRecord(
                        objective_id=oid, subject_id=subject_id,
                        source_family=self.source_family, content_type=content_type,
                        source_file=str(path), content_hash=chash, page=page,
                        chunk_text=chunk, mcq_payload=None,
                        confidence="medium", review_reason=None,
                        chunk_seq=seq,
                    )
