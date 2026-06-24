# PHASE: build — called only during ingestion prep (read-only comparison)
"""
Diff two candidate CSEC syllabus editions to confirm which is canonical before
any objectives are built.

The two CSEC Integrated Science PDFs look almost identical by size but carry
different "Effective for examinations from ..." years. This compares them
section-by-section (TOPIC headers) and, more importantly, surfaces any
objective-like statement that exists in one edition but not the other — the
thing that would actually break objective-building if the wrong file is picked.

Read-only: extracts text with PyMuPDF, writes ONE diff report, touches no other
file and no database.

  python tools/diff_syllabus_editions.py \
      --file-a "...effectiveforexamsfrom2027.pdf" \
      --file-b "...amendedoct2025.pdf"

Console = summary (counts + flagged objective differences).
Full unified diff -> {REPORTS_ROOT}\\Integrated_Science_syllabus_edition_diff.txt
"""

import argparse
import difflib
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("ERROR: PyMuPDF (fitz) is not installed. Run: pip install pymupdf")

load_dotenv()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# A real body section header, e.g. "TOPIC 2:  REPRODUCTION AND GROWTH IN PLANTS".
# Table-of-contents lines share the prefix but trail dotted leaders + a page no.,
# so a line containing a run of dots is treated as TOC and skipped.
TOPIC_RE = re.compile(r"^\s*TOPIC\s+\d+\s*:\s*(.+)$", re.IGNORECASE)
TOC_LEADER_RE = re.compile(r"\.{4,}")
CONT_RE = re.compile(r"\(cont.?d\)", re.IGNORECASE)

# CSEC specific-objective command verbs. A sentence opening with one of these is
# treated as an objective-like statement. (Explanatory-note prose also uses these
# verbs, but that noise is identical across editions and cancels in set-difference.)
COMMAND_VERBS = [
    "analyse", "analyze", "apply", "assess", "calculate", "classify", "compare",
    "construct", "contrast", "deduce", "define", "demonstrate", "derive",
    "describe", "determine", "discuss", "distinguish", "draw", "evaluate",
    "examine", "explain", "identify", "illustrate", "interpret", "investigate",
    "label", "list", "measure", "name", "observe", "outline", "predict",
    "recall", "recognise", "recognize", "record", "relate", "state", "suggest",
    "summarise", "summarize", "use",
]
VERB_ALT = "|".join(COMMAND_VERBS)
# Optional leading objective number (e.g. "1.2 ") then a command verb, captured to
# the next sentence terminator (; or .). Matched on whitespace-flattened text.
STATEMENT_RE = re.compile(
    rf"(?:\b\d+\.\d+\s+)?\b(?:{VERB_ALT})\b[^.;]*[.;]", re.IGNORECASE)


def extract_pages(path: Path) -> list[str]:
    doc = fitz.open(path)
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


def normalise_title(raw: str) -> str:
    raw = TOC_LEADER_RE.sub(" ", raw)          # kill dotted leaders
    raw = re.sub(r"\d+\s*$", "", raw)          # trailing page number
    raw = CONT_RE.sub("", raw)                 # (cont'd)
    return re.sub(r"\s+", " ", raw).strip().upper()


def split_sections(pages: list[str]) -> list[tuple[str, str]]:
    """Return [(title, body_text)] for real body TOPIC sections (TOC skipped).

    Titles can repeat across the three form-groups, so a repeated title is
    suffixed with an occurrence index to keep section keys unique."""
    lines: list[str] = []
    for text in pages:
        lines.extend(text.splitlines())

    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = TOPIC_RE.match(line)
        if not m:
            continue
        if TOC_LEADER_RE.search(line):        # a table-of-contents entry
            continue
        title = normalise_title(m.group(1))
        if title:
            starts.append((i, title))

    sections: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    for idx, (line_no, title) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        body = "\n".join(lines[line_no + 1:end])
        seen[title] = seen.get(title, 0) + 1
        key = title if seen[title] == 1 else f"{title} #{seen[title]}"
        sections.append((key, body))
    return sections


def detected_headers(pages: list[str]) -> list[str]:
    return [key for key, _ in split_sections(pages)]


def objective_statements(text: str) -> set[str]:
    flat = re.sub(r"\s+", " ", text)
    out = set()
    for m in STATEMENT_RE.finditer(flat):
        stmt = m.group(0).strip()
        norm = re.sub(r"^\d+\.\d+\s+", "", stmt).strip().rstrip(".;").lower()
        norm = re.sub(r"\s+", " ", norm)
        if len(norm) >= 12:                   # drop trivial fragments
            out.add(norm)
    return out


def whitespace_normalised(text: str) -> list[str]:
    """Lines with internal whitespace collapsed + blank lines dropped, so the
    unified diff ignores pure-whitespace/layout differences."""
    out = []
    for line in text.splitlines():
        collapsed = re.sub(r"\s+", " ", line).strip()
        if collapsed:
            out.append(collapsed)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Diff two CSEC syllabus editions.")
    parser.add_argument("--file-a", required=True, help="2027-effective syllabus PDF")
    parser.add_argument("--file-b", required=True, help="amended/2025 syllabus PDF")
    parser.add_argument("--reports-root", default=os.getenv("REPORTS_ROOT"))
    args = parser.parse_args()

    path_a, path_b = Path(args.file_a), Path(args.file_b)
    for p in (path_a, path_b):
        if not p.exists():
            sys.exit(f"ERROR: file not found: {p}")
    if not args.reports_root:
        sys.exit("ERROR: no reports root. Set REPORTS_ROOT in .env or pass --reports-root.")

    pages_a, pages_b = extract_pages(path_a), extract_pages(path_b)
    sections_a, sections_b = split_sections(pages_a), split_sections(pages_b)
    map_a = dict(sections_a)
    map_b = dict(sections_b)

    # --- header sample (confirm the split works before trusting the diff) ---
    print("=" * 78)
    print("SYLLABUS EDITION DIFF")
    print(f"  file-a: {path_a}")
    print(f"  file-b: {path_b}")
    print("=" * 78)
    print("\nSample detected section headers (first 12 of each) — confirm split:")
    print("  file-a:")
    for h in detected_headers(pages_a)[:12]:
        print(f"    - {h}")
    print("  file-b:")
    for h in detected_headers(pages_b)[:12]:
        print(f"    - {h}")

    # --- counts ---
    stmts_a = objective_statements("\n".join(pages_a))
    stmts_b = objective_statements("\n".join(pages_b))
    print("\n--- COUNTS ---")
    print(f"  sections detected:            file-a {len(sections_a):>4}   file-b {len(sections_b):>4}")
    print(f"  objective-like statements:    file-a {len(stmts_a):>4}   file-b {len(stmts_b):>4}")
    print(f"  pages:                        file-a {len(pages_a):>4}   file-b {len(pages_b):>4}")

    titles_a, titles_b = set(map_a), set(map_b)
    only_titles_a = sorted(titles_a - titles_b)
    only_titles_b = sorted(titles_b - titles_a)
    print(f"\n  section titles only in file-a ({len(only_titles_a)}):")
    for t in only_titles_a:
        print(f"    - {t}")
    print(f"  section titles only in file-b ({len(only_titles_b)}):")
    for t in only_titles_b:
        print(f"    - {t}")

    # --- flagged objective differences (the headline result) ---
    in_b_not_a = sorted(stmts_b - stmts_a)
    in_a_not_b = sorted(stmts_a - stmts_b)

    def _dump(title: str, items: list[str], cap: int = 60) -> None:
        print(f"\n### {title} ({len(items)})")
        if not items:
            print("    (none)")
            return
        for s in items[:cap]:
            print(f"    • {s}")
        if len(items) > cap:
            print(f"    ... +{len(items) - cap} more (see report file)")

    print("\n" + "#" * 78)
    print("# FLAGGED OBJECTIVE-LIKE DIFFERENCES")
    print("#" * 78)
    _dump("(a) in file-b but NOT file-a  [would be LOST if file-a is chosen]", in_b_not_a)
    _dump("(b) in file-a but NOT file-b  [would be LOST if file-b is chosen]", in_a_not_b)

    # --- full section-aware unified diff -> report file ---
    report_path = Path(args.reports_root) / "Integrated_Science_syllabus_edition_diff.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("CSEC Integrated Science — syllabus edition diff (whitespace-insensitive)\n")
        fh.write(f"file-a: {path_a}\nfile-b: {path_b}\n")
        fh.write("=" * 78 + "\n\n")

        fh.write("DETECTED HEADERS — file-a:\n")
        for h in detected_headers(pages_a):
            fh.write(f"  - {h}\n")
        fh.write("\nDETECTED HEADERS — file-b:\n")
        for h in detected_headers(pages_b):
            fh.write(f"  - {h}\n")
        fh.write("\n" + "=" * 78 + "\n")

        fh.write("\nOBJECTIVE-LIKE STATEMENTS in file-b but NOT file-a:\n")
        for s in in_b_not_a:
            fh.write(f"  + {s}\n")
        fh.write("\nOBJECTIVE-LIKE STATEMENTS in file-a but NOT file-b:\n")
        for s in in_a_not_b:
            fh.write(f"  - {s}\n")
        fh.write("\n" + "=" * 78 + "\n")

        # per-matched-section unified diff
        common = [k for k, _ in sections_a if k in map_b]
        changed = 0
        for key in common:
            a_lines = whitespace_normalised(map_a[key])
            b_lines = whitespace_normalised(map_b[key])
            if a_lines == b_lines:
                continue
            changed += 1
            diff = difflib.unified_diff(
                a_lines, b_lines, fromfile=f"A::{key}", tofile=f"B::{key}", lineterm="")
            fh.write("\n" + "-" * 78 + "\n")
            fh.write("\n".join(diff) + "\n")

        # sections present in only one edition
        for key in (k for k, _ in sections_a if k not in map_b):
            fh.write(f"\n[SECTION ONLY IN file-a] {key}\n")
        for key in (k for k, _ in sections_b if k not in map_a):
            fh.write(f"\n[SECTION ONLY IN file-b] {key}\n")

        fh.write(f"\n{'=' * 78}\nMatched sections with text differences: {changed}\n")

    matched = len([k for k, _ in sections_a if k in map_b])
    n_changed = sum(
        1 for k in (k for k, _ in sections_a if k in map_b)
        if whitespace_normalised(map_a[k]) != whitespace_normalised(map_b[k]))
    print("\n--- SECTION DIFF SUMMARY ---")
    print(f"  matched sections (title in both): {matched}")
    print(f"  matched sections that differ:     {n_changed}")
    print(f"\nFull unified diff written to:\n  {report_path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
