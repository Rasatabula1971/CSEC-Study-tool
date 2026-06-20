"""
tests/test_ingest_v2/test_pob_parity.py
=======================================
MANUAL parity gate (skipped by default). Re-ingests the live POB corpus through the
v2 orchestrator on a TEMP COPY of the live DB and diffs the resulting chunks against
the BULK-ingested v1 chunks in the DB.

Scope of the gate: v2's bulk corpus walk is only responsible for reproducing chunks
that were themselves bulk-ingested. Two other provenances in the live DB are out of
scope by construction and are excluded:

  1. UPLOAD-PROVENANCE -- chunks from the upload feature (ingest_document with Gemini
     preferred_objectives steering). Marker: documents.doc_id present in
     upload_staging.ingested_doc_id. Captured once from the source and excluded from
     BOTH the baseline AND the fresh re-ingest -- symmetric, because the same file
     yields the same hash-based doc_id on both sides (filtering only the baseline would
     let the re-ingested upload PDFs reappear as v2_only).
  2. PASTE-PROVENANCE -- chunks whose documents.source_file is a SENTINEL_SOURCE_FILES
     value ('pasted_notes' / 'uploaded_file'), i.e. pasted/filename-less content with no
     backing file. A file-walking adapter can never reproduce these.

A third residual class is TOLERATED but VERIFIED, not excluded: FILE-RELOCATION. A bulk
file moved on disk (e.g. the 31 mark schemes relocated from a since-deleted D: drive
path to the E: 03_MARK_SCHEMES folder) yields a new hash-based doc_id, so its old chunks
are v1_only and its new chunks v2_only. The verification step requires every residual
v1_only chunk's source-file basename to have a matching v2_only chunk AND the v1_only
file to no longer exist on disk; anything else fails the gate.

Pass condition:
  * mismatches == 0 on the precise column set (HARD).
  * every residual v1_only chunk is EXPLAINED: its source-file basename has a matching
    v2_only chunk (same basename) AND the v1_only source file no longer exists on disk
    (the file-relocation case). Any v1_only that lacks a basename match, or whose file
    still exists, is a genuine new problem and fails the gate (HARD).
  * v2_only chunks without a v1_only basename match are REPORTED (new coverage), not
    auto-failed.

Un-skip and run explicitly (after removing the @pytest.mark.skip line), PowerShell:
    $env:PARITY_DB_PATH = "C:\\tmp\\csec_temp.sqlite"
    python -m pytest tests/test_ingest_v2/test_pob_parity.py -v -s

Source DB: PARITY_DB_PATH if set (your temp copy), else DB_PATH from .env. The source
is read READ-ONLY for the baseline and copied once for the v2 re-ingest; never mutated.
The copy's documents table MUST be cleared for POB before re-ingest or the orchestrator's
content-hash dedup makes the gate vacuous -- _truncate_pob handles this and a hard assert
guards it.
"""

import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from _common import stub_embed, open_db

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(dotenv_path=ROOT / ".env")

from backend.db.migrations.runner import apply_migration  # noqa: E402
from backend.ingest_v2.registry import wire_adapters  # noqa: E402
from backend.ingest_v2.orchestrator import IngestOrchestrator  # noqa: E402

POB = "Principles_of_Business"
POB_MANIFEST = ROOT / "backend" / "ingest_v2" / "manifests" / "principles_of_business.yaml"
CORE_COLS = ("doc_id", "objective_id", "subject_id", "chunk_text", "page", "question_num")

# Paste-provenance sentinels: documents.source_file values that are NOT file paths.
# The /api/notes endpoint (app.py) and notes.save_notes write these for pasted text /
# filename-less uploads. Content with no backing file cannot be reproduced by a
# file-walking adapter, so it is out of scope by construction. Both are excluded even
# though only 'pasted_notes' appears in POB today -- 'uploaded_file' is a live code path
# (a filename-less notes upload) that a future subject could hit.
SENTINEL_SOURCE_FILES = ("pasted_notes", "uploaded_file")


def _table_exists(db, name: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _upload_doc_ids(db) -> set:
    """The doc_ids ingested via the upload feature (provenance marker). Empty set if
    upload_staging does not exist (a pre-m016 DB)."""
    if not _table_exists(db, "upload_staging"):
        return set()
    return {r[0] for r in db.execute(
        "SELECT ingested_doc_id FROM upload_staging WHERE ingested_doc_id IS NOT NULL"
    ).fetchall()}


def _read_pob_chunks(db, exclude_doc_ids: set) -> tuple[dict, dict]:
    """(core_by_chunk_id, source_file_by_chunk_id) for the BULK POB chunks in scope.

    Excludes two out-of-scope provenances: upload-feature chunks (doc_id in
    exclude_doc_ids) and paste-provenance chunks (source_file a SENTINEL_SOURCE_FILES
    value, i.e. no backing file). Identity is keyed on chunk_id and compared on
    CORE_COLS; source_file is carried for the basename-correspondence verification."""
    rows = db.execute(
        f"SELECT c.chunk_id, {', '.join('c.' + col for col in CORE_COLS)}, "
        f"d.source_file FROM chunks c "
        f"LEFT JOIN documents d ON d.doc_id = c.doc_id WHERE c.subject_id = ?",
        (POB,),
    ).fetchall()
    core, src = {}, {}
    for r in rows:
        if r["doc_id"] in exclude_doc_ids:
            continue
        if r["source_file"] in SENTINEL_SOURCE_FILES:
            continue
        core[r["chunk_id"]] = tuple(r[col] for col in CORE_COLS)
        src[r["chunk_id"]] = r["source_file"]
    return core, src


def _truncate_pob(db) -> None:
    """Clear every POB-derived row that feeds re-ingestion so the orchestrator treats
    the corpus as new. FK-safe order under foreign_keys=ON; no silent try/except."""
    pob_docs = "(SELECT doc_id FROM documents WHERE subject_id = ?)"
    ids = [r[0] for r in db.execute(
        "SELECT id FROM chunks WHERE subject_id = ?", (POB,)).fetchall()]
    if ids:
        qmarks = ",".join("?" * len(ids))
        for tbl in ("vec_notes", "vec_past_papers", "vec_mark_schemes"):
            db.execute(f"DELETE FROM {tbl} WHERE rowid IN ({qmarks})", ids)
    db.execute(f"DELETE FROM mark_points WHERE doc_id IN {pob_docs}", (POB,))
    db.execute(f"DELETE FROM ingest_review_queue WHERE doc_id IN {pob_docs}", (POB,))
    db.execute("DELETE FROM chunks WHERE subject_id = ?", (POB,))
    if _table_exists(db, "upload_staging"):
        db.execute(
            f"UPDATE upload_staging SET ingested_doc_id = NULL "
            f"WHERE ingested_doc_id IN {pob_docs}", (POB,))
    db.execute("DELETE FROM documents WHERE subject_id = ?", (POB,))
    db.commit()


@pytest.mark.skip(reason="manual parity check -- run explicitly against the live POB DB")
def test_pob_parity_chunks(tmp_path):
    src = os.getenv("PARITY_DB_PATH") or os.getenv("DB_PATH")
    assert src and Path(src).exists(), "no source DB (set PARITY_DB_PATH or DB_PATH)"
    assert POB_MANIFEST.is_file()

    # --- baseline: BULK v1 chunks, read READ-ONLY (upload-provenance excluded) --
    bdb = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    bdb.row_factory = sqlite3.Row
    exclude = _upload_doc_ids(bdb)                       # captured once, used on both sides
    baseline, baseline_src = _read_pob_chunks(bdb, exclude)
    bdb.close()
    assert baseline, "no bulk POB chunks in the source DB to compare against"

    # --- fresh: copy source, clear POB corpus tables, re-ingest via v2 ----------
    fresh_path = tmp_path / "fresh.sqlite"
    shutil.copy2(src, fresh_path)
    fdb = open_db(str(fresh_path))
    _truncate_pob(fdb)
    remaining = fdb.execute(
        "SELECT COUNT(*) FROM documents WHERE subject_id = ?", (POB,)).fetchone()[0]
    assert remaining == 0, f"POB documents not cleared ({remaining}); dedup would skip"

    apply_migration(fdb, "m018_mcq_questions")
    wire_adapters()
    summary = IngestOrchestrator(POB_MANIFEST, fdb, embed_fn=stub_embed).run()
    print("\n" + summary.render())
    assert summary.chunks_indexed > 0, "v2 ingested 0 chunks -- parity check is vacuous"
    # Exclude the SAME upload doc_ids from the fresh side (same file -> same hash-based
    # doc_id), so re-ingested upload-origin PDFs do not masquerade as v2_only.
    fresh, fresh_src = _read_pob_chunks(fdb, exclude)
    fdb.close()
    assert fresh, "v2 produced no POB chunks"

    # --- diff on the precise column set (source_family + autoincrement id excluded) -
    base_keys, fresh_keys = set(baseline), set(fresh)
    inter = base_keys & fresh_keys
    mismatches = [k for k in inter if baseline[k] != fresh[k]]
    v2_only = fresh_keys - base_keys
    v1_only = base_keys - fresh_keys
    print(f"\nPOB parity (bulk-only): baseline={len(baseline)} fresh={len(fresh)} "
          f"upload_docs_excluded={len(exclude)} intersection={len(inter)} "
          f"mismatches={len(mismatches)} v2_only={len(v2_only)} v1_only={len(v1_only)}")
    if mismatches:
        k = mismatches[0]
        print("first mismatch", k, "\n  v1:", baseline[k], "\n  v2:", fresh[k])

    # --- verification: every residual v1_only must be an explained relocation ---
    v2_basenames = {Path(fresh_src[k]).name for k in v2_only if fresh_src.get(k)}
    by_file: dict = {}
    for k in v1_only:
        by_file.setdefault(baseline_src.get(k), 0)
        by_file[baseline_src.get(k)] += 1
    violations = []
    print(f"\nv1_only residual ({len(v1_only)} chunks across {len(by_file)} file(s)) -- verification:")
    for sf, n in sorted(by_file.items(), key=lambda x: (x[0] or "")):
        base = Path(sf).name if sf else "(null)"
        has_match = base in v2_basenames
        on_disk = bool(sf) and Path(sf).exists()
        ok = has_match and not on_disk
        print(f"  [{'OK ' if ok else 'BAD'}] {n:4} chunk(s)  basename_match={has_match} "
              f"still_on_disk={on_disk}  {sf}")
        if not ok:
            violations.append((sf, n, has_match, on_disk))

    v2_only_files = sorted({fresh_src.get(k) for k in v2_only})
    unmatched_v2 = sorted(f for f in v2_only_files
                          if f and Path(f).name not in
                          {Path(baseline_src[k]).name for k in v1_only if baseline_src.get(k)})
    print(f"\nv2_only ({len(v2_only)} chunks across {len(v2_only_files)} file(s)); "
          f"{len(unmatched_v2)} file(s) with NO v1_only basename counterpart (new coverage):")
    for f in unmatched_v2:
        print("   +", f)

    if violations:
        print(f"\nVERIFICATION FAILED: {len(violations)} v1_only file(s) not explained "
              f"by relocation (basename match + missing-on-disk):")
        for sf, n, hm, od in violations:
            print(f"   - {sf}  ({n} chunks, basename_match={hm}, still_on_disk={od})")

    # --- gate ------------------------------------------------------------------
    assert not mismatches, f"{len(mismatches)} chunk(s) differ on core columns"
    assert not violations, (
        f"{len(violations)} residual v1_only file(s) not explained by relocation")
