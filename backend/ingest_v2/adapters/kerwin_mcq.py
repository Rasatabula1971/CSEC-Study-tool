# PHASE: build
"""
backend/ingest_v2/adapters/kerwin_mcq.py
========================================
Adapter for Kerwin Springer MCQ banks (.json) under a subject's
``Practice Questions\\Kerwin Springer`` folder.

Each question is resolved to an objective via the subject's MCQ topic map
(manifest.mcq_topic_map) -- subtopic override first (HIGH), then the topic's
default (MEDIUM), then the ``unmapped_objective`` sentinel "REVIEW" (review). Every
question becomes one MCQ record with ``mcq_payload`` populated and ``chunk_text=None``;
the orchestrator routes these to ``mcq_questions``, never to chunks / vec.

The bank schema is fixed (confirmed against the live CSEC Economics bank):
    top level : subjectId, subjectName, color, icon, greeting, topics[], questions[]
    question  : id, topic, subtopic, difficulty, stem, options{LETTER:text},
                answer(LETTER), explanation, distractors, pubId
The correct answer is a LETTER keyed into ``options`` (e.g. "B") -- there is no
numeric-index or answer-text form in this corpus, so resolution is letter-only.
``mcq_id`` is ``{PREFIX}-{question.id}`` (e.g. ECON-eco21-001).
"""

import json
import re
from pathlib import Path
from typing import Iterable, Optional

import yaml

from backend.ingest_v2.adapters.base import BaseAdapter, IngestRecord, sha256_file
from backend.ingest_v2.manifest import SubjectManifest
from backend.ingest_v2.objective_index import ObjectiveIndex
from backend.ingest_v2.subject_prefix import prefix_for


def _slug(value: str) -> str:
    """Compact lower-case alphanumeric slug for an mcq_id fallback bank segment."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower()) or "bank"


def _normalize_options(raw) -> dict:
    """Coerce the options object to a {LETTER: text} dict. The Kerwin schema already
    stores a dict; anything else yields {} (the question then routes to review)."""
    if isinstance(raw, dict):
        return {str(k).strip().upper(): str(v) for k, v in raw.items()}
    return {}


def _correct_letter(answer, options: dict) -> Optional[str]:
    """The answer is a LETTER keyed into options (e.g. "B"). Returns the upper-cased
    letter if it is a real option, else None (-> the question routes to review)."""
    if answer is None:
        return None
    letter = str(answer).strip().upper()
    return letter if letter in options else None


class KerwinMCQAdapter(BaseAdapter):
    source_family = "kerwin_mcq"

    def matches(self, path: Path) -> bool:
        if path.suffix.lower() != ".json":
            return False
        return "Kerwin Springer" in set(path.parts)

    def extract(self, path: Path, manifest: SubjectManifest,
                objective_index: ObjectiveIndex) -> Iterable[IngestRecord]:
        chash = sha256_file(path)
        subject_id = manifest.subject_id
        prefix = prefix_for(subject_id)

        def _bad_file(reason: str) -> IngestRecord:
            return IngestRecord(
                objective_id="REVIEW", subject_id=subject_id,
                source_family=self.source_family, content_type="mcq",
                source_file=str(path), content_hash=chash, page=None,
                chunk_text=None, mcq_payload=None,
                confidence="review", review_reason=reason,
            )

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            yield _bad_file(f"kerwin_json_parse_error: {e}")
            return

        topic_map, unmapped = self._load_topic_map(manifest)
        questions = data.get("questions") or []
        if not isinstance(questions, list) or not questions:
            yield _bad_file("kerwin_no_questions")
            return

        bank = _slug(str(data.get("subjectId") or path.stem))
        valid_ids = objective_index.all_objective_ids()

        for i, q in enumerate(questions, 1):
            if not isinstance(q, dict):
                continue
            qid = q.get("id") or f"{bank}-{i:03d}"
            mcq_id = f"{prefix}-{qid}"
            topic = q.get("topic") or ""
            subtopic = q.get("subtopic") or ""
            oid, confidence, reason = self._resolve(topic, subtopic, topic_map,
                                                    unmapped, valid_ids)

            options = _normalize_options(q.get("options"))
            correct = _correct_letter(q.get("answer"), options)
            stem = str(q.get("stem") or "").strip()

            payload = {
                "mcq_id": mcq_id,
                "stem": stem,
                "options": options,
                "correct_option": correct,
                "explanation": q.get("explanation"),
                "source": "kerwin_springer",
                "source_topic": topic or None,
                "source_subtopic": subtopic or None,
                "difficulty": q.get("difficulty"),
            }

            # A malformed question (no stem / no options / no resolvable answer letter)
            # cannot be a valid mcq_questions row -> review regardless of mapping.
            if not stem or not options or correct is None:
                yield IngestRecord(
                    objective_id="REVIEW", subject_id=subject_id,
                    source_family=self.source_family, content_type="mcq",
                    source_file=str(path), content_hash=chash, page=None,
                    chunk_text=None, mcq_payload=payload,
                    confidence="review", review_reason="kerwin_question_incomplete",
                )
                continue

            yield IngestRecord(
                objective_id=oid, subject_id=subject_id,
                source_family=self.source_family, content_type="mcq",
                source_file=str(path), content_hash=chash, page=None,
                chunk_text=None, mcq_payload=payload,
                confidence=confidence, review_reason=reason,
            )

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _load_topic_map(manifest: SubjectManifest) -> tuple[dict, str]:
        raw = yaml.safe_load(manifest.mcq_topic_map_path.read_text(encoding="utf-8")) or {}
        return raw.get("topic_map") or {}, str(raw.get("unmapped_objective") or "REVIEW")

    @staticmethod
    def _resolve(topic: str, subtopic: str, topic_map: dict, unmapped: str,
                 valid_ids: set) -> tuple[str, str, Optional[str]]:
        """(objective_id, confidence, review_reason). subtopic override -> HIGH,
        topic default -> MEDIUM, else the sentinel -> review. An objective the map
        points at that is not in the locked syllabus is downgraded to review."""
        entry = topic_map.get(topic) if topic else None
        if entry:
            overrides = entry.get("subtopic_overrides") or {}
            if subtopic and subtopic in overrides:
                oid = overrides[subtopic]
                if oid in valid_ids:
                    return oid, "high", None
                return "REVIEW", "review", "mcq_mapped_objective_not_in_syllabus"
            default = entry.get("default_objective")
            if default:
                if default in valid_ids:
                    return default, "medium", None
                return "REVIEW", "review", "mcq_mapped_objective_not_in_syllabus"
        return "REVIEW", "review", "mcq_unmapped_topic"
