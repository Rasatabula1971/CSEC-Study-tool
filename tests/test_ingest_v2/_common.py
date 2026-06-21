"""
tests/test_ingest_v2/_common.py
===============================
Shared builders for ingest_v2 tests. Underscore-prefixed so pytest does not collect
it as a test module. No Ollama: embeddings are stubbed.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import sqlite_vec  # noqa: E402
from backend.db.migrations.runner import apply_migration  # noqa: E402

SCHEMA_PATH = ROOT / "backend" / "db" / "schema.sql"
EMBED_DIM = 768

# Economics objectives the tests bind to (ids only need to exist + be locked).
OBJECTIVES = [
    ("ECON-1.1", "1.1", "Define the concept of economics and the basic economic problem"),
    ("ECON-1.2", "1.2", "Distinguish the branches microeconomics and macroeconomics"),
    ("ECON-1.5", "1.5", "Explain scarcity opportunity cost and choice"),
    ("ECON-3.3", "3.3", "Explain how demand and supply determine market equilibrium price"),
    ("ECON-3.9", "3.9", "Explain price elasticity of demand and supply"),
]


def stub_embed(_text: str) -> list[float]:
    """Deterministic non-Ollama embedding: a fixed 768-float zero vector."""
    return [0.0] * EMBED_DIM


def open_db(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def make_locked_db(path: str = ":memory:", subject: str = "Economics",
                   locked: bool = True) -> sqlite3.Connection:
    """A DB with schema + m018 + a (locked) Economics subject and OBJECTIVES."""
    db = open_db(path)
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    db.commit()
    apply_migration(db, "m018_mcq_questions")
    db.execute(
        "INSERT INTO subjects (subject_id, display_name, syllabus_locked) VALUES (?,?,?)",
        (subject, subject.replace("_", " "), 1 if locked else 0),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?,?,?,?)", ("ECON-S1", subject, "Intro", "1"),
    )
    db.execute(
        "INSERT INTO syllabus_sections (section_id, subject_id, title, section_num) "
        "VALUES (?,?,?,?)", ("ECON-S3", subject, "Markets", "3"),
    )
    for oid, num, stmt in OBJECTIVES:
        section = "ECON-S1" if num.startswith("1.") else "ECON-S3"
        db.execute(
            "INSERT INTO objectives (objective_id, section_id, subject_id, "
            "objective_num, content_stmt) VALUES (?,?,?,?,?)",
            (oid, section, subject, num, stmt),
        )
    db.commit()
    return db


def write_manifest(tmp_path: Path, source_root: Path, *, subject: str = "Economics",
                   topic_map: str | None = None) -> Path:
    """Write a temp manifest + its referenced placeholder CSV and MCQ map, all under
    tmp_path, pointing source_root at the synthetic corpus."""
    csv = tmp_path / "syll.csv"
    csv.write_text("section_id,section_num,section_title,objective_id,objective_num,"
                   "content_stmt\n", encoding="utf-8")
    mcq = tmp_path / "mcq.yaml"
    mcq.write_text(topic_map or "topic_map: {}\nunmapped_objective: REVIEW\n",
                   encoding="utf-8")
    man = tmp_path / f"{subject.lower()}.yaml"
    man.write_text(
        f"subject_id: {subject}\n"
        f"display_name: {subject}\n"
        f"source_root: {_yaml_quote(source_root)}\n"
        f"syllabus_csv: {_yaml_quote(csv)}\n"
        f"mcq_topic_map: {_yaml_quote(mcq)}\n"
        f"skip_patterns: ['_Review Needed', '__pycache__']\n",
        encoding="utf-8",
    )
    return man


def _yaml_quote(p: Path) -> str:
    """Single-quote a Windows path for YAML (backslashes are literal in single quotes)."""
    return "'" + str(p) + "'"


# --- synthetic corpus file builders -----------------------------------------
def make_pdf(path: Path, pages: list[str]) -> None:
    """Write a simple text PDF (one page per string) via PyMuPDF."""
    import fitz
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    doc.save(str(path))
    doc.close()


def make_docx(path: Path, paragraphs: list[str]) -> None:
    import docx
    path.parent.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(str(path))
