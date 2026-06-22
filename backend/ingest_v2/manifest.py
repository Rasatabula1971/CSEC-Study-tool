# PHASE: build
"""
backend/ingest_v2/manifest.py
=============================
The per-subject ingestion manifest -- the one file a builder drops to onboard a
new subject. A manifest names the subject, where its corpus lives on disk, the
syllabus CSV and MCQ topic map that drive objective mapping, and the patterns to
skip while walking the corpus.

``SubjectManifest`` (Pydantic) validates the YAML on load and FAILS LOUDLY:
  * subject_id must be a known CSEC subject (has an objective-id prefix).
  * source_root, syllabus_csv, and mcq_topic_map must exist (check_paths=True).

Repo-relative paths (syllabus_csv, mcq_topic_map) resolve against the repo root;
source_root is an absolute path to the corpus on the data drive.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from backend.ingest_v2.subject_prefix import SUBJECT_PREFIX

# Repo root = .../CSEC-study-partner (this file is backend/ingest_v2/manifest.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class ManifestError(Exception):
    """Raised when a manifest is structurally invalid or points at missing paths."""


class SubjectManifest(BaseModel):
    """Validated representation of a subject manifest YAML.

    Type/shape validation happens on construction; filesystem-existence checks are
    applied by :func:`load_manifest` (so the model stays usable in tests with
    synthetic paths, while real loads fail loudly on a missing corpus)."""

    subject_id: str
    display_name: str
    source_root: str
    syllabus_csv: str
    mcq_topic_map: str
    paper_2_grading_enabled: bool = False
    # Opt-in OCR for the GenericPDFAdapter on image-only PDFs. Default False so a
    # subject that does not set it (e.g. POB) keeps the exact v1 PyMuPDF-only path
    # and stays byte-identical to v1 (the test_pob_parity gate).
    enable_ocr: bool = False
    # Opt-in: include GenericOfficeAdapter in this subject's dispatch (claims
    # .docx/.pptx/.pptm that MoESLMSAdapter didn't). Default False so a subject that
    # does not set it (e.g. POB) excludes the Office adapter entirely -- its loose
    # .docx/.pptx are left unclaimed exactly as today (preserves test_pob_parity).
    # Deliberately separate from extra_source_roots: a subject may want one without
    # the other.
    enable_office_adapter: bool = False
    # Additional corpus roots walked alongside source_root (e.g. purpose-built
    # Bridge/Supplemental notes staged outside the main corpus tree). Empty by
    # default so existing subjects walk only source_root, unchanged.
    extra_source_roots: list[str] = Field(default_factory=list)
    known_gaps: list[str] = Field(default_factory=list)
    skip_patterns: list[str] = Field(default_factory=list)

    @field_validator("subject_id")
    @classmethod
    def _known_subject(cls, v: str) -> str:
        if v not in SUBJECT_PREFIX:
            known = ", ".join(sorted(SUBJECT_PREFIX))
            raise ValueError(f"unknown subject_id '{v}'. Known: {known}")
        return v

    # --- resolved absolute paths ------------------------------------------
    @property
    def source_root_path(self) -> Path:
        return Path(self.source_root)

    @property
    def syllabus_csv_path(self) -> Path:
        return _resolve(self.syllabus_csv)

    @property
    def mcq_topic_map_path(self) -> Path:
        return _resolve(self.mcq_topic_map)

    @property
    def extra_source_root_paths(self) -> list[Path]:
        """Resolved extra corpus roots (absolute as-is; relative against repo root,
        like the other manifest paths)."""
        return [_resolve(p) for p in self.extra_source_roots]

    def check_paths(self) -> None:
        """Raise ManifestError unless every referenced path exists. The corpus
        root and every extra source root must be directories; the CSV and MCQ map
        must be files."""
        missing = []
        if not self.source_root_path.is_dir():
            missing.append(f"source_root (dir) not found: {self.source_root_path}")
        for p in self.extra_source_root_paths:
            if not p.is_dir():
                missing.append(f"extra_source_root (dir) not found: {p}")
        if not self.syllabus_csv_path.is_file():
            missing.append(f"syllabus_csv not found: {self.syllabus_csv_path}")
        if not self.mcq_topic_map_path.is_file():
            missing.append(f"mcq_topic_map not found: {self.mcq_topic_map_path}")
        if missing:
            raise ManifestError(
                "manifest path validation failed:\n  - " + "\n  - ".join(missing)
            )


def _resolve(path_str: str) -> Path:
    """Resolve a manifest path. Absolute paths are used as-is; relative paths are
    taken relative to the repo root (so a manifest can read either way)."""
    p = Path(path_str)
    return p if p.is_absolute() else (REPO_ROOT / p)


def load_manifest(path, check_paths: bool = True) -> SubjectManifest:
    """Load + validate a manifest YAML.

    Raises ManifestError on a missing/invalid file, a Pydantic ValidationError on
    a bad shape (e.g. unknown subject_id), and -- when check_paths is True --
    ManifestError if any referenced path is missing. Tests pass check_paths=False
    (or point at temp dirs) to validate structure without a real corpus."""
    path = Path(path)
    if not path.is_file():
        raise ManifestError(f"manifest file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"manifest is not valid YAML ({path}): {e}") from e
    if not isinstance(data, dict):
        raise ManifestError(f"manifest top level must be a mapping ({path})")

    manifest = SubjectManifest(**data)
    if check_paths:
        manifest.check_paths()
    return manifest
