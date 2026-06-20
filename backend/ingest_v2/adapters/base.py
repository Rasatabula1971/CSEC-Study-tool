# PHASE: build
"""
backend/ingest_v2/adapters/base.py
==================================
The adapter contract for ingest_v2.

A source family (Caribbean AI markdown, MoE SLMS office docs, Kerwin Springer MCQ
JSON, generic PDF) is captured by ONE adapter. Folder structure -- not subject
identity -- is the dispatch key: ``matches(path)`` decides whether an adapter owns
a file, ``extract(...)`` yields :class:`IngestRecord`s. Adapters are pure: they
read files and produce records, and MUST NOT touch the DB. The orchestrator owns
all writes, embedding, and review-queue routing.
"""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.objective_index import ObjectiveIndex


def sha256_file(path) -> str:
    """SHA256 of a file's bytes -- the documents.content_hash value. Matches
    v1 ingest.file_hash exactly (same algorithm, same block reads)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class IngestRecord:
    """One unit of extracted content bound (or flagged for binding) to an objective.

    A prose record carries ``chunk_text`` and ``mcq_payload=None`` (routed to chunks
    + the vec index). An MCQ record carries ``mcq_payload`` and ``chunk_text=None``
    (routed to mcq_questions). ``confidence == "review"`` means the orchestrator also
    writes the record to ingest_review_queue with ``review_reason``.
    """

    objective_id: str            # FK target -- must exist (or "REVIEW" sentinel)
    subject_id: str
    source_family: str           # chunks.source_family value
    content_type: str            # "notes" | "past_paper" | "mark_scheme" | "mcq" | "specimen"
    source_file: str             # absolute path
    content_hash: str            # SHA256 of the source file (documents.content_hash)
    page: Optional[int]
    chunk_text: Optional[str]    # None for MCQ records
    mcq_payload: Optional[dict]  # populated only for MCQ records, None otherwise
    confidence: str              # "high" | "medium" | "low" | "review"
    review_reason: Optional[str] = None  # set when confidence == "review"
    # v1-parity hook: the exact within-page chunk index to use in chunk_id /
    # mark_point_id. The GenericPDFAdapter sets this to enumerate(chunk_page)'s
    # index so re-ingesting POB stays byte-equivalent to v1 (which counts even
    # empty-after-strip positions). Left None by every other adapter, in which
    # case the orchestrator assigns a per-page running index.
    chunk_seq: Optional[int] = None

    @property
    def is_mcq(self) -> bool:
        return self.mcq_payload is not None

    @property
    def needs_review(self) -> bool:
        return self.confidence == "review"


class BaseAdapter(ABC):
    """One source family. Subclasses set ``source_family`` and implement the two
    methods below. Adapters never open the DB."""

    source_family: str  # class attribute, e.g. "caribbean_ai"

    @abstractmethod
    def matches(self, path: Path) -> bool:
        """Return True if this adapter handles this file."""

    @abstractmethod
    def extract(self, path: Path, manifest: SubjectManifest,
                objective_index: ObjectiveIndex) -> Iterable[IngestRecord]:
        """Yield IngestRecords for `path`. MUST NOT touch the DB."""
