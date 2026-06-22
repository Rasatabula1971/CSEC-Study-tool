# PHASE: build
"""
backend/ingest_v2/orchestrator.py
=================================
The ingest_v2 orchestrator: the single component that owns the DB. It walks a
subject's corpus, dispatches each file to the first matching adapter, and writes
the resulting IngestRecords -- chunks (+ vec index), mcq_questions, mark_points,
and ingest_review_queue -- with content-hash dedup at the file level.

Adapters are pure (they never touch the DB). All side effects live here, so the
review-queue policy, embedding, and v1-parity details (chunk_id / mark_point_id
schemes) are in one place.

Design decisions worth knowing:
  * REVIEW records are written to ingest_review_queue ONLY -- never also inserted.
    A prose chunk with no confident objective must not be indexed (Rule 1), and an
    MCQ whose objective is the 'REVIEW' sentinel would violate mcq_questions'
    NOT NULL FK. So "review" is terminal: queue and move on.
  * v1 parity: for the generic-PDF path, chunk_id = "{doc_id}-p{page}-c{idx}" and
    mark_point_id = "{obj}-{doc_id}-p{page}c{idx}-mpN", where idx is the per-page
    running position over EVERY record the adapter emits (matched or review), exactly
    as v1's enumerate(chunk_page(text)) index. That keeps re-ingesting POB through v2
    byte-equivalent to v1 apart from the new source_family column.
"""

import fnmatch
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# backend/ on path so the bare v1 imports resolve regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ingest as v1  # noqa: E402  -- reuse v1 helpers for parity
from ollama_client import ollama_embed  # noqa: E402

from backend.ingest_v2.manifest import load_manifest, SubjectManifest, ManifestError
from backend.ingest_v2.objective_index import ObjectiveIndex
from backend.ingest_v2.adapters.base import BaseAdapter, IngestRecord


class OrchestratorError(Exception):
    """Raised when the orchestrator refuses to run (unlocked subject, bad manifest)."""


# Adapter dispatch order. GenericPDFAdapter is the catch-all and MUST be last so a
# .pdf inside a specialised folder (T&T MoE SLMS) is claimed by its specific adapter
# first. Populated at CP3 once the adapter classes exist.
ADAPTER_ORDER: list[type[BaseAdapter]] = []


def register_adapters(*adapter_classes: type[BaseAdapter]) -> None:
    """Set the dispatch order (clears any previous registration). Called once at
    import time by the package __main__ / a small wiring module at CP3."""
    ADAPTER_ORDER.clear()
    ADAPTER_ORDER.extend(adapter_classes)


@dataclass
class IngestSummary:
    """Tally of one orchestrator run, printed at the end."""

    subject_id: str = ""
    dry_run: bool = False
    files_seen: int = 0
    files_processed: int = 0
    files_skipped_duplicate: int = 0
    files_skipped_no_adapter: int = 0
    files_no_records: int = 0
    records_by_source_family: dict = field(default_factory=dict)
    records_by_confidence: dict = field(default_factory=dict)
    records_by_content_type: dict = field(default_factory=dict)
    chunks_indexed: int = 0
    mcqs_inserted: int = 0
    review_queue_entries: int = 0

    @staticmethod
    def _bump(d: dict, key) -> None:
        d[key] = d.get(key, 0) + 1

    def tally_record(self, r: IngestRecord) -> None:
        self._bump(self.records_by_source_family, r.source_family)
        self._bump(self.records_by_confidence, r.confidence)
        self._bump(self.records_by_content_type, r.content_type)

    def render(self) -> str:
        mode = "DRY RUN (no writes)" if self.dry_run else "applied"
        lines = [
            "=" * 60,
            f"ingest_v2 summary  --  subject={self.subject_id}  [{mode}]",
            "=" * 60,
            f"  files seen                 : {self.files_seen}",
            f"  files processed            : {self.files_processed}",
            f"  files skipped (duplicate)  : {self.files_skipped_duplicate}",
            f"  files skipped (no adapter) : {self.files_skipped_no_adapter}",
            f"  files with no records      : {self.files_no_records}",
            "  records by source_family   :",
        ]
        lines += [f"      {k:<16} {v}" for k, v in sorted(self.records_by_source_family.items())] or ["      (none)"]
        lines.append("  records by confidence      :")
        lines += [f"      {k:<16} {v}" for k, v in sorted(self.records_by_confidence.items())] or ["      (none)"]
        lines.append("  records by content_type    :")
        lines += [f"      {k:<16} {v}" for k, v in sorted(self.records_by_content_type.items())] or ["      (none)"]
        lines += [
            f"  chunks indexed             : {self.chunks_indexed}",
            f"  MCQs inserted              : {self.mcqs_inserted}",
            f"  review-queue entries       : {self.review_queue_entries}",
        ]
        return "\n".join(lines)


class IngestOrchestrator:
    def __init__(self, manifest_path, db: sqlite3.Connection,
                 dry_run: bool = False, embed_fn=ollama_embed,
                 adapter_filter: str | None = None):
        self.manifest_path = Path(manifest_path)
        self.db = db
        self.dry_run = dry_run
        self.embed_fn = embed_fn
        # Restrict to a single adapter family by its source_family name (debug aid).
        self.adapter_filter = adapter_filter

    # --- public entry -----------------------------------------------------
    def run(self) -> IngestSummary:
        manifest = load_manifest(self.manifest_path)  # validates shape + paths
        self._assert_subject_locked(manifest.subject_id)
        oindex = ObjectiveIndex(self.db, manifest.subject_id)
        if len(oindex) == 0:
            raise OrchestratorError(
                f"no objectives loaded for '{manifest.subject_id}'. "
                "Load + lock its syllabus first."
            )
        adapters = self._select_adapters()
        summary = IngestSummary(subject_id=manifest.subject_id, dry_run=self.dry_run)

        for path in self._walk(manifest):
            summary.files_seen += 1
            adapter = self._first_match(adapters, path)
            if adapter is None:
                summary.files_skipped_no_adapter += 1
                continue

            chash = v1.file_hash(path)
            if v1.already_ingested(self.db, chash):
                summary.files_skipped_duplicate += 1
                continue

            records = list(adapter.extract(path, manifest, oindex))
            if not records:
                summary.files_no_records += 1
                summary.files_processed += 1
                continue
            for r in records:
                summary.tally_record(r)

            if self.dry_run:
                summary.files_processed += 1
                continue

            self._write_file(path, chash, records, summary)
            summary.files_processed += 1

        return summary

    # --- adapter selection / walking --------------------------------------
    def _select_adapters(self) -> list[BaseAdapter]:
        classes = ADAPTER_ORDER
        if self.adapter_filter:
            classes = [c for c in ADAPTER_ORDER if c.source_family == self.adapter_filter]
            if not classes:
                known = ", ".join(c.source_family for c in ADAPTER_ORDER) or "(none registered)"
                raise OrchestratorError(
                    f"unknown --adapter '{self.adapter_filter}'. Known: {known}"
                )
        return [c() for c in classes]

    def _walk(self, manifest: SubjectManifest):
        """Yield every file under source_root AND each extra_source_root, skipping any
        whose path contains a component matching a skip pattern (fnmatch, per path
        component). De-duplicated by resolved absolute path, so a file reachable from
        more than one root (e.g. an extra root nested under source_root) is walked
        once. The same skip_patterns apply uniformly to every root."""
        patterns = manifest.skip_patterns
        seen = set()
        for root in [manifest.source_root_path, *manifest.extra_source_root_paths]:
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(root).parts
                if any(fnmatch.fnmatch(part, pat)
                       for part in rel_parts for pat in patterns):
                    continue
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                yield p

    @staticmethod
    def _first_match(adapters: list[BaseAdapter], path: Path) -> BaseAdapter | None:
        for a in adapters:
            if a.matches(path):
                return a
        return None

    # --- writing ----------------------------------------------------------
    def _assert_subject_locked(self, subject_id: str) -> None:
        row = self.db.execute(
            "SELECT syllabus_locked FROM subjects WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if row is None:
            raise OrchestratorError(
                f"subject '{subject_id}' is not in the database. Lock its syllabus first."
            )
        if row["syllabus_locked"] != 1:
            raise OrchestratorError(
                f"subject '{subject_id}' is not locked (syllabus_locked != 1). "
                "Ingestion is blocked until the syllabus is signed off."
            )

    def _write_file(self, path: Path, chash: str, records: list[IngestRecord],
                    summary: IngestSummary) -> None:
        """Create the documents row for the file, then route every record. One
        commit per file (matching v1's per-file commit cadence)."""
        content_type = records[0].content_type
        doc_id = f"{content_type}-{chash[:12]}"
        paper_str, year_int = (None, None)
        if content_type == "past_paper":
            paper_str, year_int = v1.parse_past_paper_filename(path.name)
        self.db.execute(
            "INSERT INTO documents (doc_id, subject_id, content_type, paper, year, "
            "source_file, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (doc_id, records[0].subject_id, content_type, paper_str, year_int,
             str(path), chash),
        )

        page_counter: dict = {}
        for r in records:
            # v1 parity: a record may carry the exact within-page index it wants
            # (GenericPDFAdapter does). Otherwise assign a per-page running index.
            if r.chunk_seq is not None:
                idx = r.chunk_seq
            else:
                idx = page_counter.get(r.page, 0)
                page_counter[r.page] = idx + 1
            if r.needs_review:
                self._write_review(r, doc_id)
                summary.review_queue_entries += 1
            elif r.is_mcq:
                if self._write_mcq(r):
                    summary.mcqs_inserted += 1
            else:
                self._write_chunk(r, doc_id, idx, summary)
        self.db.commit()

    def _write_review(self, r: IngestRecord, doc_id: str) -> None:
        """Write a review record to ingest_review_queue. chunk_text carries a JSON
        blob of the record (the column is NOT NULL and MCQ records have no prose),
        so a later manual-mapping pass has the full context."""
        blob = json.dumps({
            "source_family": r.source_family,
            "content_type": r.content_type,
            "objective_id": r.objective_id,
            "page": r.page,
            "chunk_text": r.chunk_text,
            "mcq_payload": r.mcq_payload,
            "confidence": r.confidence,
            "review_reason": r.review_reason,
        }, ensure_ascii=False)
        # objective_id column: only store a real FK-valid id, never the 'REVIEW'
        # sentinel (the column has no FK but keeping it clean avoids confusion).
        oid = r.objective_id if r.objective_id and r.objective_id != "REVIEW" else None
        self.db.execute(
            "INSERT INTO ingest_review_queue (source_file, chunk_text, reason, "
            "objective_id, doc_id) VALUES (?, ?, ?, ?, ?)",
            (r.source_file, blob, r.review_reason, oid, doc_id),
        )

    def _write_mcq(self, r: IngestRecord) -> bool:
        """Insert one MCQ into mcq_questions. Idempotent on the deterministic mcq_id
        (INSERT OR IGNORE), so re-running a bank never duplicates. Returns True if a
        row was inserted. The mcq_payload contract (keys) is set by KerwinMCQAdapter."""
        p = r.mcq_payload
        cur = self.db.execute(
            "INSERT OR IGNORE INTO mcq_questions (mcq_id, objective_id, subject_id, "
            "source, source_topic, source_subtopic, difficulty, stem, options_json, "
            "correct_option, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["mcq_id"], r.objective_id, r.subject_id,
                p.get("source", r.source_family),
                p.get("source_topic"), p.get("source_subtopic"), p.get("difficulty"),
                p["stem"], json.dumps(p["options"], ensure_ascii=False),
                p["correct_option"], p.get("explanation"),
            ),
        )
        return cur.rowcount > 0

    def _write_chunk(self, r: IngestRecord, doc_id: str, idx: int,
                     summary: IngestSummary) -> None:
        """Insert one prose chunk, embed + index it, and -- for mark schemes --
        extract mark points exactly as v1 does (parity)."""
        question_num = (
            v1.detect_question_num(r.chunk_text) if r.content_type == "past_paper" else None
        )
        chunk_id = f"{doc_id}-p{r.page}-c{idx}"
        cur = self.db.execute(
            "INSERT INTO chunks (doc_id, objective_id, subject_id, chunk_text, page, "
            "question_num, chunk_id, source_family) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, r.objective_id, r.subject_id, r.chunk_text, r.page,
             question_num, chunk_id, r.source_family),
        )
        rowid = cur.lastrowid
        table = v1.VEC_TABLE[r.content_type]
        v1.index_chunk(self.db, rowid, self.embed_fn(r.chunk_text), table)
        summary.chunks_indexed += 1

        if r.content_type == "mark_scheme":
            self._extract_mark_points(r, doc_id, idx)

    def _extract_mark_points(self, r: IngestRecord, doc_id: str, idx: int) -> None:
        """Replicate v1's mark-point extraction for a mark-scheme chunk: parse award
        points and insert them, or queue 'markscheme_no_points' when none parse."""
        points = v1.parse_mark_points(r.chunk_text)
        if points:
            for order, pt in enumerate(points, 1):
                mp_id = f"{r.objective_id}-{doc_id}-p{r.page}c{idx}-mp{order}"
                self.db.execute(
                    "INSERT OR IGNORE INTO mark_points (mark_point_id, objective_id, "
                    "question_id, doc_id, point_text, marks_value, point_order) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (mp_id, r.objective_id, None, doc_id, pt, 1, order),
                )
        else:
            self.db.execute(
                "INSERT INTO ingest_review_queue (source_file, chunk_text, reason) "
                "VALUES (?, ?, ?)",
                (r.source_file, r.chunk_text, "markscheme_no_points"),
            )
