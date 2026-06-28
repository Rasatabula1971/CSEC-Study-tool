# PHASE: build
"""
backend/load_video_links.py
===========================
Stage V1 — load pre-qualified YouTube video links from the video pipeline CSVs
into the objective_videos table.

Source files (SSD-only, never committed):
  D:\\GPT Folder CSEC\\Organized_CSEC_2027\\_video_pipeline\\*_final_review.csv

Each CSV row has:
  matched_content_stmt  -- the objective text used for matching
  url                   -- YouTube URL
  video_title           -- display title
  channel               -- channel name
  duration              -- HH:MM or M:SS string
  flag                  -- OK | VIDEO_REUSED_Nx | (blank = skip)

ID resolution: CSV uses pipeline-internal sequential IDs (POA-001). Real DB IDs
are POA-1.1, IT-1.1, etc. Resolution is via content_stmt join, three passes:
  1. Exact match (lowercase strip)
  2. Strip trailing "; and" or ", and" suffix, then exact match
  3. Prefix match on first 40 characters

Unresolvable rows are logged to stderr and skipped — they do not block the load.
Mathematics rows are skipped until syllabus_locked = 1 for that subject.

Run:
    python backend/load_video_links.py --dry-run
    python backend/load_video_links.py
    python backend/load_video_links.py --subject Economics --dry-run
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from db.backup import backup_first  # noqa: E402

DEFAULT_PIPELINE_DIR = (
    Path(os.getenv("VIDEO_PIPELINE_DIR"))
    if os.getenv("VIDEO_PIPELINE_DIR")
    else None
)  # set VIDEO_PIPELINE_DIR in .env or pass --video-pipeline-dir; no D:\ fallback

# Short prefix in the CSV filenames -> subject_id in the DB.
SUBJECT_MAP: dict[str, str] = {
    "poa": "Principles_of_Accounts",
    "it":  "Information_Technology",
    "eco": "Economics",
    "is":  "Integrated_Science",
    "mat": "Mathematics",
}

# Rows with these flag values are loaded; anything else is skipped.
# VIDEO_REUSED_Nx means the same URL covers multiple objectives — still valid.
_LOAD_FLAGS = {"OK"}
_REUSED_PREFIX = "VIDEO_REUSED"


def _is_loadable(flag: str) -> bool:
    f = flag.strip().upper()
    return f in _LOAD_FLAGS or f.startswith(_REUSED_PREFIX)


def _build_stmt_map(db: sqlite3.Connection, subject_id: str) -> dict[str, str]:
    """Return {content_stmt_lower: objective_id} for all objectives in subject."""
    rows = db.execute(
        "SELECT objective_id, content_stmt FROM objectives WHERE subject_id = ?",
        (subject_id,),
    ).fetchall()
    return {r[1].strip().lower(): r[0] for r in rows}


_TRAILING_AND_RE = re.compile(r"[;,]\s+and\s*$", re.IGNORECASE)


def resolve_objective_id(content_stmt: str, stmt_map: dict[str, str]) -> str | None:
    """Resolve a CSV content_stmt to a real objective_id via three passes."""
    key = content_stmt.strip().lower()

    # Pass 1: exact match
    if key in stmt_map:
        return stmt_map[key]

    # Pass 2: strip trailing "; and" / ", and" then exact
    stripped = _TRAILING_AND_RE.sub("", key).strip()
    if stripped != key and stripped in stmt_map:
        return stmt_map[stripped]

    # Pass 3: prefix match on first 40 chars
    prefix = key[:40]
    for stmt, oid in stmt_map.items():
        if stmt.startswith(prefix):
            return oid

    return None


def _ensure_table(db: sqlite3.Connection) -> None:
    """Create objective_videos if the FastAPI app hasn't run m019 yet."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS objective_videos (
            video_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
            subject_id   TEXT NOT NULL REFERENCES subjects(subject_id),
            url          TEXT NOT NULL,
            title        TEXT NOT NULL,
            channel      TEXT,
            duration_str TEXT,
            source_file  TEXT NOT NULL,
            added_at     TEXT DEFAULT (datetime('now')),
            UNIQUE(objective_id, url)
        )
    """)
    db.commit()


def load_videos(
    db: sqlite3.Connection,
    pipeline_dir: Path,
    subject_filter: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Load OK/VIDEO_REUSED rows from *_final_review.csv into objective_videos.

    Returns a stats dict:
      {subject: {loaded, skipped_flag, unresolved, already_present, csv_missing}}
    """
    if not dry_run:
        _ensure_table(db)

    stats: dict[str, dict] = {}

    subjects = (
        {k: v for k, v in SUBJECT_MAP.items() if v == subject_filter}
        if subject_filter
        else SUBJECT_MAP
    )

    for prefix, subject_id in subjects.items():
        stat = {
            "loaded": 0,
            "skipped_flag": 0,
            "unresolved": 0,
            "already_present": 0,
            "csv_missing": False,
        }
        stats[subject_id] = stat

        csv_path = pipeline_dir / f"{prefix}_final_review.csv"
        if not csv_path.exists():
            stat["csv_missing"] = True
            print(f"  {subject_id}: CSV not found at {csv_path}", file=sys.stderr)
            continue

        # Skip subjects whose syllabus isn't locked — videos need a real objective FK.
        locked = db.execute(
            "SELECT syllabus_locked FROM subjects WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        if not locked or not locked[0]:
            print(
                f"  {subject_id}: syllabus not locked — skipping (load after locking)",
                file=sys.stderr,
            )
            continue

        stmt_map = _build_stmt_map(db, subject_id)

        with open(csv_path, encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                flag = row.get("flag", "").strip()
                if not _is_loadable(flag):
                    stat["skipped_flag"] += 1
                    continue

                content_stmt = row.get("matched_content_stmt", "").strip()
                objective_id = resolve_objective_id(content_stmt, stmt_map)
                if objective_id is None:
                    stat["unresolved"] += 1
                    print(
                        f"  UNRESOLVED [{subject_id}]: {content_stmt[:70]}",
                        file=sys.stderr,
                    )
                    continue

                url = row.get("url", "").strip()
                title = row.get("video_title", "").strip()
                channel = row.get("channel", "").strip() or None
                duration_str = row.get("duration", "").strip() or None

                if dry_run:
                    stat["loaded"] += 1
                    continue

                try:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO objective_videos
                            (objective_id, subject_id, url, title, channel,
                             duration_str, source_file)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (objective_id, subject_id, url, title, channel,
                         duration_str, str(csv_path)),
                    )
                    if db.execute("SELECT changes()").fetchone()[0]:
                        stat["loaded"] += 1
                    else:
                        stat["already_present"] += 1
                except sqlite3.IntegrityError as exc:
                    print(f"  INSERT error [{objective_id}]: {exc}", file=sys.stderr)
                    stat["unresolved"] += 1

        if not dry_run:
            db.commit()

    return stats


def _print_summary(stats: dict, dry_run: bool) -> None:
    label = "[DRY RUN] " if dry_run else ""
    print(f"\n{label}Video link load summary")
    print("=" * 60)
    print(f"{'Subject':<35} {'Loaded':>7} {'Skipped':>8} {'Unres':>6} {'Dup':>5}")
    print("-" * 60)
    total_loaded = 0
    for subject_id, s in stats.items():
        if s["csv_missing"]:
            print(f"  {subject_id:<33} CSV MISSING")
            continue
        print(
            f"  {subject_id:<33} {s['loaded']:>7} {s['skipped_flag']:>8}"
            f" {s['unresolved']:>6} {s['already_present']:>5}"
        )
        total_loaded += s["loaded"]
    print("-" * 60)
    print(f"  {'TOTAL':<33} {total_loaded:>7}")
    print()


@backup_first("pre_video_load")
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load pre-qualified YouTube video links into objective_videos."
    )
    parser.add_argument(
        "--subject",
        help="Load only this subject (e.g. Economics). Omit to load all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be loaded without writing anything.",
    )
    parser.add_argument(
        "--video-pipeline-dir",
        type=Path,
        default=DEFAULT_PIPELINE_DIR,
        required=(DEFAULT_PIPELINE_DIR is None),
        help=(
            "Directory containing *_final_review.csv files. "
            "Defaults to VIDEO_PIPELINE_DIR env var; pass explicitly if unset."
        ),
    )
    args = parser.parse_args()

    try:
        import sqlite_vec

        db_path = os.getenv("DB_PATH")
        if not db_path:
            sys.exit("ERROR: DB_PATH not set in .env")
        db = sqlite3.connect(db_path)
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("PRAGMA foreign_keys = ON")
    except Exception as exc:
        sys.exit(f"ERROR opening DB: {exc}")

    if args.subject and args.subject not in SUBJECT_MAP.values():
        sys.exit(
            f"ERROR: unknown subject '{args.subject}'. "
            f"Valid: {', '.join(SUBJECT_MAP.values())}"
        )

    stats = load_videos(
        db,
        pipeline_dir=args.video_pipeline_dir,
        subject_filter=args.subject,
        dry_run=args.dry_run,
    )
    _print_summary(stats, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
