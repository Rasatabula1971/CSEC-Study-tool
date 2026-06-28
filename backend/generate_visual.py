# PHASE: build
"""
backend/generate_visual.py
==========================
On-demand visual page generator. Converts a canonical lesson into a
standalone, interactive HTML visual using Gemini Flash.

Usage:
  - Called from GET /api/visual/{objective_id} (serve, generate if absent)
  - Also importable as a CLI:
      python backend/generate_visual.py --subject POB --objective POB-1.3
      python backend/generate_visual.py --subject POB  # all objectives with lessons

Cache: {SSD_ROOT}/05_VISUALS/{subject_id}/{objective_id}.html
DB:    visual_pages table (tracks generated_at, model_used, file_path, generation_ms)

Privacy note: lesson text is sent to Gemini Free Tier (same exception already
accepted for Gemini grading/classification calls -- see CLAUDE.md "Optional Cloud
Mode"). Never called at runtime; always build-phase or on-demand builder trigger.
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db.init_db import open_db  # noqa: E402
from db.backup import backup_first  # noqa: E402
from gemini_client import gemini_chat, is_gemini_available  # noqa: E402

logger = logging.getLogger(__name__)

SSD_ROOT = os.getenv("SSD_ROOT")  # no fallback — derive from env, never guess a drive letter
VISUALS_DIR = Path(SSD_ROOT) / "05_VISUALS" if SSD_ROOT else None
# Allow a different model for visual generation (higher output-token limit helps
# with completeness). Falls back to GEMINI_MODEL from gemini_client if unset.
VISUAL_GEMINI_MODEL = os.getenv("VISUAL_GEMINI_MODEL") or None

# ──────────────────────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────────────────────

VISUAL_SYSTEM = """You are an expert educational web designer.
You create beautiful, self-contained interactive HTML pages that explain CSEC (Caribbean Secondary Education Certificate) concepts visually.

HARD RULES — every one must be followed:
1. Output ONLY a single complete HTML document, starting with <!DOCTYPE html> and ending with </html>. No prose, no markdown fences (no ```html).
2. Self-contained: all CSS and JavaScript must be inline (no <link>, no <script src>, no CDN).
3. Dark theme: background #13151a, card surfaces #1c1f27, accent blue #5b8def, accent green #3ddc84, accent red #e24b4a, text #e8eaed.
4. Font: system-ui / -apple-system / Segoe UI — no Google Fonts.
5. FULL VIEWPORT LAYOUT — this page lives inside a full-screen iframe:
   - Set html, body { margin: 0; padding: 0; width: 100%; min-height: 100vh; }
   - Never set a max-width on body or any top-level wrapper.
   - Use a single scrollable column with 24–40 px side padding. A simple two-column split is fine for a comparison, but do not create complex multi-section layouts.
6. ONE primary interactive element — choose the single most useful interaction for this concept (e.g. a labelled diagram you click to reveal details, a before/after slider, a set of 3–4 tabbed explanation cards). Keep the JavaScript under 60 lines. Do NOT add multiple separate widgets.
7. KEEP THE OUTPUT SHORT. Target 200–280 lines of HTML total (CSS + JS + content combined). Write concise CSS; avoid verbose comments. A focused page that loads completely is better than an elaborate page that gets cut off.
8. CSEC level: clear language (age 14–16), no university jargon. Define every technical term when first used.
9. Do not include a quiz or test section. This is explanation only.
10. Include a "← Close" button (position: fixed; top: 16px; left: 16px; z-index: 999) that calls: window.parent.postMessage('close', '*'); — do NOT use window.close().
11. Cover the objective fully using the lesson text as your primary source.

OUTPUT: one complete HTML document, no fences, ends with </html>."""

VISUAL_SCHEMA = None  # Free-form HTML — no JSON schema; we receive raw text


def _build_prompt(
    objective_id: str,
    content_stmt: str,
    section_title: str,
    subject_display: str,
    lesson_text: str,
) -> str:
    return (
        f"Subject: {subject_display} (CSEC)\n"
        f"Section: {section_title}\n"
        f"Objective ID: {objective_id}\n"
        f"Objective: {content_stmt}\n\n"
        f"--- LESSON TEXT (your primary source) ---\n"
        f"{lesson_text.strip()}\n"
        f"--- END LESSON TEXT ---\n\n"
        f"Generate a self-contained interactive HTML visual page that teaches this objective. "
        f"Follow all rules in your system prompt exactly."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Core generation function
# ──────────────────────────────────────────────────────────────────────────────

def generate_visual(
    db: sqlite3.Connection,
    objective_id: str,
    *,
    force: bool = False,
) -> dict:
    """Generate (or serve cached) the visual HTML for one objective.

    Returns:
        {
          "ok":          bool,
          "file_path":   str | None,   # absolute path on SSD
          "cached":      bool,
          "error":       str | None,
        }
    """
    # 1. Check cache unless --force
    if not force:
        row = db.execute(
            "SELECT file_path FROM visual_pages WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if row:
            fp = Path(row["file_path"])
            if fp.exists():
                # Auto-heal: regenerate if the file has markdown fences (buggy earlier run)
                # or is truncated (hit max_output_tokens before </html>).
                content = fp.read_text(encoding="utf-8", errors="ignore")
                head = content[:10]
                tail = content[-50:]
                if head.strip().startswith("```"):
                    logger.warning("Cached visual for %s has markdown fences; regenerating", objective_id)
                elif "</html>" not in tail.lower():
                    logger.warning("Cached visual for %s appears truncated (no </html>); regenerating", objective_id)
                else:
                    return {"ok": True, "file_path": str(fp), "cached": True, "error": None}
            else:
                # File missing from SSD but DB record exists — regenerate
                logger.warning("visual_pages record exists but file missing; regenerating %s", objective_id)

    # 2. Fetch lesson + objective metadata
    obj_row = db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.subject_id,
               s.title AS section_title,
               sub.display_name AS subject_display,
               ol.lesson_text
        FROM   objectives o
        JOIN   syllabus_sections s   ON s.section_id   = o.section_id
        JOIN   subjects sub          ON sub.subject_id  = o.subject_id
        LEFT JOIN objective_lessons ol ON ol.objective_id = o.objective_id
        WHERE  o.objective_id = ?
        """,
        (objective_id,),
    ).fetchone()

    if not obj_row:
        return {"ok": False, "file_path": None, "cached": False, "error": "unknown objective_id"}

    lesson_text = obj_row["lesson_text"] or ""
    if not lesson_text:
        return {"ok": False, "file_path": None, "cached": False,
                "error": "no canonical lesson yet — run ingest_lessons.py first"}

    if not is_gemini_available():
        return {"ok": False, "file_path": None, "cached": False,
                "error": "GEMINI_API_KEY not set — visual generation requires Gemini"}

    # 3. Prepare output path
    if VISUALS_DIR is None:
        return {"ok": False, "file_path": None, "cached": False,
                "error": "SSD_ROOT is not set — cannot locate the visuals cache. "
                         "Set SSD_ROOT in .env (e.g. SSD_ROOT=E:\\CSEC_AI_STUDY_PARTNER)."}
    subject_id = obj_row["subject_id"]
    out_dir = VISUALS_DIR / subject_id
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "file_path": None, "cached": False,
                "error": f"cannot create visuals directory: {exc}"}

    safe_oid = objective_id.replace("/", "_").replace("\\", "_")
    out_path = out_dir / f"{safe_oid}.html"

    # 4. Call Gemini
    prompt = _build_prompt(
        objective_id=objective_id,
        content_stmt=obj_row["content_stmt"],
        section_title=obj_row["section_title"],
        subject_display=obj_row["subject_display"],
        lesson_text=lesson_text,
    )

    t0 = time.monotonic()
    try:
        html = gemini_chat(
            messages=[{"role": "user", "content": prompt}],
            system=VISUAL_SYSTEM,
            schema=None,          # free-form HTML, not JSON
            thinking_budget=0,    # disable thinking — all 8192 output tokens go to HTML
            model=VISUAL_GEMINI_MODEL,  # None = use GEMINI_MODEL default
        )
    except Exception as exc:
        return {"ok": False, "file_path": None, "cached": False, "error": str(exc)}

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # 5. Strip markdown fences (Gemini wraps free-form output in ```html...```)
    #    then validate the result looks like HTML.
    stripped = html.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:html|HTML)?\n?", "", stripped, count=1)
        stripped = re.sub(r"\n?```\s*$", "", stripped).strip()
    if not stripped.lower().startswith("<!doctype") and "<html" not in stripped.lower():
        return {"ok": False, "file_path": None, "cached": False,
                "error": f"model did not return HTML (got: {stripped[:120]!r})"}

    # 6. Write to SSD
    try:
        out_path.write_text(stripped, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "file_path": None, "cached": False,
                "error": f"could not write visual to SSD: {exc}"}

    # 7. Record in DB (upsert)
    db.execute(
        """
        INSERT INTO visual_pages (objective_id, subject_id, generated_at, model_used, file_path, generation_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(objective_id) DO UPDATE SET
            subject_id    = excluded.subject_id,
            generated_at  = excluded.generated_at,
            model_used    = excluded.model_used,
            file_path     = excluded.file_path,
            generation_ms = excluded.generation_ms
        """,
        (
            objective_id,
            subject_id,
            datetime.now(timezone.utc).isoformat(),
            "gemini",
            str(out_path),
            elapsed_ms,
        ),
    )
    db.commit()

    logger.info("Generated visual for %s in %dms → %s", objective_id, elapsed_ms, out_path)
    return {"ok": True, "file_path": str(out_path), "cached": False, "error": None}


def generate_all_for_subject(
    db: sqlite3.Connection,
    subject_id: str,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Generate visuals for every objective in subject_id that has a canonical lesson.

    Returns a summary dict {total, generated, cached, skipped_no_lesson, failed}.
    """
    rows = db.execute(
        """
        SELECT o.objective_id
        FROM   objectives o
        JOIN   subjects s ON s.subject_id = o.subject_id
        WHERE  o.subject_id = ?
          AND  s.syllabus_locked = 1
        ORDER  BY o.objective_id
        """,
        (subject_id,),
    ).fetchall()

    summary = {"total": len(rows), "generated": 0, "cached": 0, "skipped_no_lesson": 0, "failed": 0, "errors": []}

    for r in rows:
        oid = r["objective_id"]
        if dry_run:
            has_lesson = db.execute(
                "SELECT 1 FROM objective_lessons WHERE objective_id = ?", (oid,)
            ).fetchone()
            if has_lesson:
                summary["generated"] += 1
            else:
                summary["skipped_no_lesson"] += 1
            continue

        result = generate_visual(db, oid, force=force)
        if result["cached"]:
            summary["cached"] += 1
        elif result["ok"]:
            summary["generated"] += 1
        else:
            err = result["error"] or ""
            if "no canonical lesson" in err:
                summary["skipped_no_lesson"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].append(f"{oid}: {err}")

    return summary


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

@backup_first("pre_generate_visual")
def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Generate visual HTML pages for CSEC lessons")
    parser.add_argument("--subject",   required=True, help="Subject ID (e.g. Principles_of_Business)")
    parser.add_argument("--objective", help="Single objective ID to (re-)generate")
    parser.add_argument("--force",     action="store_true", help="Regenerate even if cached")
    parser.add_argument("--dry-run",   action="store_true", help="Count eligible objectives without generating")
    args = parser.parse_args()

    db = open_db()

    if args.objective:
        result = generate_visual(db, args.objective, force=args.force)
        if result["ok"]:
            print(f"{'CACHED' if result['cached'] else 'GENERATED'}: {result['file_path']}")
        else:
            print(f"FAILED: {result['error']}")
            sys.exit(1)
    else:
        summary = generate_all_for_subject(
            db, args.subject, force=args.force, dry_run=args.dry_run
        )
        prefix = "DRY RUN — " if args.dry_run else ""
        print(f"\n{prefix}Subject: {args.subject}")
        print(f"  Total objectives  : {summary['total']}")
        print(f"  Generated         : {summary['generated']}")
        print(f"  Cached (skipped)  : {summary['cached']}")
        print(f"  No lesson yet     : {summary['skipped_no_lesson']}")
        print(f"  Failed            : {summary['failed']}")
        for e in summary["errors"]:
            print(f"    ✗ {e}")


if __name__ == "__main__":
    main()
