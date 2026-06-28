# PHASE: build — one-shot script to ingest English with keep_alive=300 for speed.
import os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.ollama_client import ollama_embed
from backend.ingest_v2.orchestrator import IngestOrchestrator
from backend.ingest_v2.manifest import load_manifest
from backend.ingest_v2.registry import wire_adapters
from backend.db.backup import backup_database
import backend.ingest as v1

MANIFEST = Path(__file__).resolve().parent / "manifests" / "english.yaml"

def fast_embed(text: str) -> list[float]:
    return ollama_embed(text, keep_alive=300)

db_path = os.getenv("DB_PATH")
if not db_path or not Path(db_path).exists():
    sys.exit(f"ERROR: DB not found at {db_path}")

flags = load_manifest(MANIFEST, check_paths=False)
wire_adapters(enable_office_adapter=flags.enable_office_adapter,
              enable_markdown_adapter=flags.enable_markdown_adapter)

db = v1.open_db(db_path)
backup_database("pre_english_ingest")

orch = IngestOrchestrator(MANIFEST, db, dry_run=False, embed_fn=fast_embed)
summary = orch.run()
print(summary.render())
db.close()
