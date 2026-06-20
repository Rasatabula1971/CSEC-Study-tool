# PHASE: build
"""
backend/ingest_v2/__main__.py
=============================
CLI entry for the ingest_v2 framework.

    python -m backend.ingest_v2 --subject Economics
    python -m backend.ingest_v2 --subject Economics --dry-run
    python -m backend.ingest_v2 --subject Economics --adapter caribbean_ai

A real (non-dry) run is destructive (writes chunks/mcq/mark_points), so it follows
the Stage-14 safety pattern: take a backup first, then ensure the m018 schema
migration is applied, then ingest. A --dry-run does neither (it writes nothing).
The subject's manifest is resolved as manifests/{subject_id.lower()}.yaml.
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

# backend/ and backend/db on path for the bare helper imports below.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
import ingest as v1  # noqa: E402  -- reuse v1.open_db

from backend.ingest_v2.orchestrator import IngestOrchestrator, OrchestratorError
from backend.ingest_v2.manifest import ManifestError
from backend.ingest_v2.registry import wire_adapters

MANIFESTS_DIR = Path(__file__).resolve().parent / "manifests"
M018_VERSION = "m018_mcq_questions"


def _manifest_path_for(subject: str) -> Path:
    return MANIFESTS_DIR / f"{subject.lower()}.yaml"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ingest_v2: source-family-aware ingestion across CSEC subjects."
    )
    ap.add_argument("--subject", required=True, help="e.g. Economics")
    ap.add_argument("--dry-run", action="store_true",
                    help="walk + classify but write nothing.")
    ap.add_argument("--adapter",
                    help="run a single adapter family in isolation "
                         "(caribbean_ai | moe_slms | kerwin_mcq | generic_pdf).")
    args = ap.parse_args()

    manifest_path = _manifest_path_for(args.subject)
    if not manifest_path.is_file():
        sys.exit(f"ERROR: no manifest for '{args.subject}' at {manifest_path}")

    db_path = os.getenv("DB_PATH")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}.")

    wire_adapters()
    db = v1.open_db(db_path)
    try:
        if not args.dry_run:
            # Stage-14 safety: back up first, then ensure m018 is present.
            from db.backup import backup_database  # lazy: build-only dependency
            from backend.db.migrations.runner import apply_migration
            backup_database("pre_ingest_v2")
            state = apply_migration(db, M018_VERSION)
            print(f"migration {M018_VERSION}: {state}")

        orch = IngestOrchestrator(manifest_path, db, dry_run=args.dry_run,
                                  adapter_filter=args.adapter)
        summary = orch.run()
        print(summary.render())
    except (OrchestratorError, ManifestError) as e:
        sys.exit(f"ERROR: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
