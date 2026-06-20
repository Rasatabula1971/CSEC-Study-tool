"""
tests/test_ingest_v2/test_pob_parity.py
=======================================
MANUAL parity gate (skipped by default). Re-ingests the live POB corpus through the
v2 orchestrator on a TEMP COPY of the live DB and diffs the resulting chunks against
the v1 chunks already in the DB.

Goal: zero meaningful diffs apart from the new chunks.source_family column. This is
the check that confirms v2 is non-breaking before the live DB is ever touched.

Un-skip and run explicitly (after removing the @pytest.mark.skip line):
    set PARITY_DB_PATH=C:\tmp\csec_temp.sqlite   &&  ^
        pytest tests/test_ingest_v2/test_pob_parity.py -v -s

Source DB: PARITY_DB_PATH if set (point it at YOUR temp copy that already has m018
applied), else DB_PATH from .env. The source DB is copied internally and is NEVER
mutated -- both the v1 baseline and the v2 re-ingest run on private temp copies, so
you can re-run the gate and the live/temp DB is never modified.

Requires: the POB corpus (manifest source_root) present on this machine.
"""

import os
import shutil
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


def _chunks_by_id(db) -> dict:
    rows = db.execute(
        f"SELECT chunk_id, {', '.join(CORE_COLS)} FROM chunks WHERE subject_id = ?",
        (POB,),
    ).fetchall()
    return {r["chunk_id"]: tuple(r[c] for c in CORE_COLS) for r in rows}


@pytest.mark.skip(reason="manual parity check -- run explicitly against the live POB DB")
def test_pob_parity_chunks(tmp_path):
    # PARITY_DB_PATH (your temp copy) wins over DB_PATH; the source is only read.
    live = os.getenv("PARITY_DB_PATH") or os.getenv("DB_PATH")
    assert live and Path(live).exists(), "no source DB (set PARITY_DB_PATH or DB_PATH)"
    assert POB_MANIFEST.is_file()

    # --- baseline: the v1 chunks already in the live DB --------------------
    base_path = tmp_path / "baseline.sqlite"
    shutil.copy2(live, base_path)
    base_db = open_db(str(base_path))
    baseline = _chunks_by_id(base_db)
    base_db.close()
    assert baseline, "no POB chunks in the live DB to compare against"

    # --- fresh: same DB with corpus tables cleared, re-ingested via v2 -----
    fresh_path = tmp_path / "fresh.sqlite"
    shutil.copy2(live, fresh_path)
    fdb = open_db(str(fresh_path))
    # Clear corpus-derived tables (FK-safe order); keep subjects/objectives/syllabus.
    for stmt in ("DELETE FROM ingest_review_queue", "DELETE FROM mark_points",
                 "DELETE FROM chunks", "DELETE FROM documents",
                 "DELETE FROM vec_notes", "DELETE FROM vec_past_papers",
                 "DELETE FROM vec_mark_schemes"):
        try:
            fdb.execute(stmt)
        except Exception:
            pass
    fdb.commit()
    apply_migration(fdb, "m018_mcq_questions")
    wire_adapters()
    summary = IngestOrchestrator(POB_MANIFEST, fdb, embed_fn=stub_embed).run()
    print("\n" + summary.render())
    fresh = _chunks_by_id(fdb)
    fdb.close()

    # --- diff --------------------------------------------------------------
    base_keys, fresh_keys = set(baseline), set(fresh)
    inter = base_keys & fresh_keys
    mismatches = [k for k in inter if baseline[k] != fresh[k]]
    v2_only = fresh_keys - base_keys
    v1_only = base_keys - fresh_keys

    print(f"\nPOB parity: baseline={len(baseline)} fresh={len(fresh)} "
          f"intersection={len(inter)} mismatches={len(mismatches)} "
          f"v2_only={len(v2_only)} v1_only={len(v1_only)}")
    if mismatches:
        k = mismatches[0]
        print("first mismatch", k, "\n  v1:", baseline[k], "\n  v2:", fresh[k])
    if v1_only:
        print("sample v1_only (likely non-folder source files):", list(v1_only)[:5])

    # Meaningful-diff assertions: every reproduced chunk is identical on the core
    # columns (source_family excluded -- that IS the allowed new column), and v2
    # never invents a chunk v1 did not have.
    assert not mismatches, f"{len(mismatches)} chunk(s) differ on core columns"
    assert not v2_only, f"v2 produced {len(v2_only)} chunks absent from v1"
    # v1_only is reported (chunks from sources outside the walked corpus root, e.g.
    # paste-ingested notes) for human judgement, not asserted to zero.
