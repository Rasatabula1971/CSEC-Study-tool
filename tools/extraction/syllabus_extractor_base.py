# PHASE: build — called only during ingestion prep (syllabus extraction)
"""
Shared CSEC syllabus-extraction primitives.

Canonical implementation of the two-column PDF extraction techniques that were
hardened on the Mathematics build. Every subject's `extract_<subject>_objectives.py`
script should IMPORT these rather than copy them, so a fix here protects every
future subject (English, etc.) at once.

This module guards against two concrete bug classes that block-bbox extraction
falls into on CXC's two-column (Specific Objectives | Explanatory Notes) layout:

  1. **Column-bbox bleed.** PyMuPDF's "blocks" mode merges text across the column
     gutter — a left-margin "(i)" marker glued to a right-column note becomes one
     block spanning x0≈68→x1≈532, so an `x0 < threshold` filter LEAKS note text
     into the left column (and a wide-but-legitimate objective block is dropped).
     Fixed by filtering at the WORD level (centroid vs. a per-page gutter) — see
     `_detect_gutter` / `_left_text`.

  2. **Eager-termination truncation.** A statement assembler that latches on the
     first ';' and then drops any line not starting with "(" loses line-wrapped
     tails such as "…vertices of / solids; and, / (e) classes of solids". With a
     clean left column the statement is simply every line up to the next stop
     signal; the caller's `_extract_obj_statement` no longer needs that heuristic.

`validate_objectives` is the layout-independent QA net that surfaces any residual
truncation / note-bleed / garble BEFORE the CSV is loaded and lessons composed.

Pure helpers: `page` is any object exposing `.get_text("words")` (a PyMuPDF
Page). No fitz import is needed here, so this module stays import-light.
"""

import re

# Column boundary is detected PER PAGE (see _detect_gutter), not hardcoded.
# CXC syllabi place the Specific-Objectives / Explanatory-Notes gutter at
# different x on different pages. DEFAULT_GUTTER is the sanity fallback only.
DEFAULT_GUTTER = 282.0
# The gutter is the widest empty vertical strip in this x band — the whitespace
# channel between the two columns. Detecting it by projection (rather than by the
# right column's min-x0) guarantees the gutter sits at/after the left column's
# right edge, so a right-aligned left-column word is never clipped.
_GUTTER_BAND = (230, 320)

# Trailing connector/preposition ⇒ the statement was cut off mid-clause.
_TRUNC_TAIL_RE = re.compile(
    r"(?:\b(?:of|the|a|an|to|for|and|or|in|on|with|that|which|from|by|as)|[:,])\s*$",
    re.IGNORECASE,
)
# Roman-numeral enumerators are EXPLANATORY-NOTE markers in CXC syllabi
# (objective sub-items use (a)/(b)/(c)…), so their presence signals note bleed.
_NOTE_ROMAN_RE = re.compile(r"\(\s*(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\s*\)", re.IGNORECASE)
# Garbled typeset-math residue: empty parens, isolated single-letter runs,
# or a lone superscript digit stranded between spaces (e.g. "( ) a x h k + +").
_GARBLE_RE = re.compile(r"\(\s*\)|(?:\b[a-zA-Z]\s+){3,}[a-zA-Z]\b|\s\d\s*[+)]")


def _detect_gutter(page) -> float:
    """
    Find this page's column gutter = the midpoint of the widest uncovered (empty)
    vertical strip within _GUTTER_BAND. Words are atomic (none straddles the
    gutter), so the union of all word x-intervals leaves a clean channel between
    the columns. Falls back to DEFAULT_GUTTER on a single-column page (no gap).
    """
    words = page.get_text("words")
    if not words:
        return DEFAULT_GUTTER
    lo, hi = _GUTTER_BAND
    covered = bytearray(hi - lo + 1)
    for w in words:
        a = max(lo, int(w[0]))
        b = min(hi, int(w[2]) + 1)
        for x in range(a, b):
            covered[x - lo] = 1
    best_len, best_mid = 0, DEFAULT_GUTTER
    run_start = None
    for i in range(len(covered) + 1):
        empty = i < len(covered) and covered[i] == 0
        if empty and run_start is None:
            run_start = i
        elif not empty and run_start is not None:
            if i - run_start > best_len:
                best_len = i - run_start
                best_mid = lo + (run_start + i) / 2.0
            run_start = None
    if best_len < 2:        # no clear column gap → treat as single column
        return DEFAULT_GUTTER
    return best_mid


def _left_text(page, gutter: float) -> str:
    """
    Reconstruct the left-column text as newline-separated visual lines, keeping
    only words whose horizontal centre is left of `gutter`. Word-level (not
    block-level) so cross-gutter merged blocks can't leak notes in or drop wide
    objective lines.
    """
    words = [w for w in page.get_text("words") if (w[0] + w[2]) / 2.0 < gutter]
    words.sort(key=lambda w: (w[1], w[0]))  # reading order: top→bottom, left→right
    lines: list[str] = []
    current: list = []
    current_y = None
    for w in words:
        if current_y is None or abs(w[1] - current_y) <= 3.0:
            current.append(w)
            if current_y is None:
                current_y = w[1]
        else:
            lines.append(" ".join(t[4] for t in sorted(current, key=lambda z: z[0])))
            current = [w]
            current_y = w[1]
    if current:
        lines.append(" ".join(t[4] for t in sorted(current, key=lambda z: z[0])))
    return "\n".join(lines)


def validate_objectives(objectives: list[dict]) -> list[tuple[str, str, str]]:
    """
    Layout-independent QA pass. Returns [(objective_id, reason, statement)] for
    every suspicious row. Does NOT drop rows — it surfaces statements that are
    likely truncated, note-contaminated, or garbled so a human reviews them
    BEFORE the CSV is loaded and lessons are composed.

    Each objective dict must expose "objective" (the statement) and "_obj_id".
    """
    flags = []
    for o in objectives:
        stmt = o["objective"]
        oid = o["_obj_id"]
        if _TRUNC_TAIL_RE.search(stmt):
            flags.append((oid, "truncated (ends on a connector/preposition)", stmt))
        if _NOTE_ROMAN_RE.search(stmt):
            flags.append((oid, "note bleed (contains a roman-numeral enumerator)", stmt))
        if _GARBLE_RE.search(stmt):
            flags.append((oid, "garbled math/typesetting residue", stmt))
        if len(stmt) < 15:
            flags.append((oid, "suspiciously short", stmt))
    return flags
