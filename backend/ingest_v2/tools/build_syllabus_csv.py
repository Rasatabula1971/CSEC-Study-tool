# PHASE: build
"""
backend/ingest_v2/tools/build_syllabus_csv.py
=============================================
Turn the machine-extracted official syllabus master map (one row per CXC
objective, any subject) into the CSV that ``backend/db/syllabus_parser.py``
loads. Generic over subject; run concretely per subject (Economics first).

INPUT  master map columns (see source_data/_official_syllabus_objective_master_map.csv):
    subject, context, objective_number, page, objective,
    top_confidence, top_score, top_resource, top_resource_path
  - ``context`` is either ``"General"`` (a teaching-methodology note, NOT a real
    objective -- excluded) or ``"SECTION {N}: {TITLE}"`` (a real section).

OUTPUT  columns -- EXACTLY the header ``syllabus_parser.py`` expects (its
REQUIRED_COLUMNS plus the three optional ones it also reads):
    section_id, section_num, section_title, objective_id, objective_num,
    content_stmt, skill_type, command_words, exam_weight

Formatting conventions are taken from the LIVE POB objectives in csec.sqlite, not
guessed:
  * content_stmt   -- trailing ';'/'.' stripped, first letter capitalized.
  * command_words  -- the DB stores a JSON array ('["Explain"]'). syllabus_parser's
                      command_words_to_json() builds that array by splitting the CSV
                      cell on ','/'|', so the CSV MUST carry a PIPE-DELIMITED list
                      of Title-Case verbs ("Calculate|Explain"). Writing a literal
                      JSON array into the cell would be mangled. The pipe form
                      round-trips to the exact live-DB '["Calculate", "Explain"]'.
  * skill_type     -- 'Knowledge' | 'Understanding' | 'Application' (Title-Case), or
                      the literal 'UNCLASSIFIED' when no seed verb is found (never a
                      guessed default).
  * exam_weight    -- the literal 'TBD'. Paper 1/Paper 2 weighting is a manual
                      cross-check against the CXC syllabus PDF; it is not inferred.

Data-quality handling (all loud, never silent):
  * Same section_num with a truncated vs full title across rows -> canonicalize to
    the LONGEST variant; every disagreeing section_num is reported.
  * "General" (and any non-SECTION) context rows are excluded and printed.
  * Duplicate (section_num, objective_number) within a subject RAISES -- a duplicate
    means a real extraction error or two objectives sharing a number; a human looks.

CLI:
    python -m backend.ingest_v2.tools.build_syllabus_csv \
        --subject Economics \
        --master-map backend/ingest_v2/source_data/_official_syllabus_objective_master_map.csv \
        --output backend/ingest_v2/syllabus_csvs/economics.csv

This is build-time tooling: no Ollama, no cloud, never touches the live DB.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from backend.ingest_v2.subject_prefix import prefix_for

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# "SECTION 2: PRODUCTION, ECONOMIC RESOURCES AND RESOURCE ALLOCATION"
# Section number widened from (\d+) to ([\d.]+) so dotted composite section
# numbers (e.g. Integrated Science's "SECTION 2.1:" = module.topic) parse too.
# Verified non-regressive for POB (sections 1-10) and Economics (1-8): plain
# integer section numbers match identically under both patterns.
SECTION_RE = re.compile(r"^SECTION\s+([\d.]+):\s*(.+)$")

# CXC command verbs, canonical Title-Case (matching the live DB). Scanned
# case-insensitively as whole words; output preserves order of first appearance.
COMMAND_VERBS = [
    "Define", "State", "List", "Identify", "Name", "Outline",
    "Explain", "Describe", "Distinguish", "Differentiate", "Illustrate", "Relate",
    "Use", "Apply", "Calculate", "Compute", "Discuss",
    "Analyse", "Analyze", "Evaluate", "Assess", "Examine", "Investigate",
    "Appraise", "Recommend", "Determine", "Compare",
    "Contrast", "Justify", "Draw", "Sketch", "Construct", "Interpret",
    # Mathematics-specific command words (verified against CXC skill bands):
    # Application: solve/simplify/factorise/factorize/prove/show/derive/convert/
    #              express/write/find/order (quantitative / procedural work)
    "Solve", "Simplify", "Factorise", "Factorize", "Prove", "Show",
    "Derive", "Convert", "Express", "Write", "Find", "Order", "Represent",
    "Divide", "Estimate", "Change", "Translate", "Substitute", "Rewrite",
    "Measure", "Locate", "Obtain", "Make",
    # English A-specific command words (verified against CXC 01/G/SYLL 25 skill bands):
    # Knowledge: extract (locate/retrieve information from text), recognise (British sp.)
    # Understanding: trace (follow development), deduce (infer), explore (examine ideas)
    # Application: create (produce written/oral work), communicate (convey meaning),
    #   collaborate (produce responses with peers), organise (structure content),
    #   present (deliver an argument), formulate (develop a position)
    "Extract", "Recognise",
    "Trace", "Deduce", "Explore",
    "Create", "Communicate", "Collaborate", "Organise", "Present", "Formulate",
]

# skill_type derivation. Precedence highest -> lowest: Application beats
# Understanding beats Knowledge when several verbs appear in one objective.
# Categories are aligned to the LIVE POB DB classifications (the real precedent),
# not the original spec's starting table: "discuss" and "interpret" sit in
# Understanding (POB stores them that way), and "differentiate" joins "distinguish"
# (functionally identical) in Understanding. "classify" was already Understanding.
# Integrated Science adds verbs absent from POB/Economics; these are placed by CXC's
# skill bands (the syllabus' "KC, UK and XS" abilities): "examine"/"investigate"
# (close inspection / experimental work) and "appraise"/"recommend"/"determine"
# (critical judgement / working a result out, like evaluate/assess) -> Application;
# "relate" (show relationships, comprehension) -> Understanding.
SKILL_APPLICATION = {
    "use", "apply", "calculate", "compute", "analyse", "analyze",
    "evaluate", "assess", "compare", "contrast", "justify", "draw", "sketch",
    "construct", "examine", "investigate", "appraise", "recommend", "determine",
    # Mathematics additions (procedural / proof-based work → Application)
    "solve", "simplify", "factorise", "factorize", "prove", "show",
    "derive", "convert", "express", "write", "find", "order", "represent",
    "divide", "estimate", "change", "substitute", "rewrite", "measure",
    "locate", "obtain",
    # English A additions (Evaluating and Creating band → Application)
    "create", "communicate", "collaborate", "organise", "present", "formulate",
}
SKILL_UNDERSTANDING = {
    "explain", "describe", "distinguish", "differentiate", "illustrate",
    "summarize", "classify", "discuss", "interpret", "relate",
    "translate",  # converting between representations — comprehension
    "make",       # "make inference(s)" — comprehension/interpretation
    # English A additions (Analysing band → Understanding)
    "trace", "deduce", "explore",
}
SKILL_KNOWLEDGE = {
    "define", "state", "list", "identify", "name", "outline",
    # English A additions (Understanding band: locating/retrieving info from text)
    "extract", "recognise",
}

UNCLASSIFIED = "UNCLASSIFIED"
EXAM_WEIGHT_PLACEHOLDER = "TBD"

# The exact header syllabus_parser.py consumes (REQUIRED_COLUMNS + optional three).
OUTPUT_COLUMNS = [
    "section_id", "section_num", "section_title", "objective_id",
    "objective_num", "content_stmt", "skill_type", "command_words", "exam_weight",
]


def _numeric_key(s: str) -> tuple[int, str]:
    """Sort key that parses a leading integer (string fallback) so '2.10' sorts
    after '2.9', and merged supplement rows land in numeric position."""
    m = re.match(r"(\d+)", s or "")
    return (int(m.group(1)) if m else 0, s or "")


# ---------------------------------------------------------------------------
# Field derivation
# ---------------------------------------------------------------------------

# Curated word-split rejoins: a non-word fragment + short continuation that the
# PDF wrapped WITHOUT a hyphen, so the de-hyphenation pass can't see it and a
# general "join short tokens" rule would wreck legitimate phrases ("drug use",
# "first aid", "the use"). Mirrors the hyphen fix's allowlist philosophy: only
# join confirmed artifacts, never guess. Applied case-insensitively, whole-token.
WORD_SPLIT_REJOINS = {
    "infectio us": "infectious",
}
_WORD_SPLIT_RES = {
    re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE): good
    for bad, good in WORD_SPLIT_REJOINS.items()
}

# Curated single-token spelling corrections for unambiguous source typos (the
# official PDF text itself is misspelled). Whole-word, case-insensitive; only
# confirmed, non-interpretive fixes belong here.
TYPO_FIXES = {
    "conditios": "conditions",
}
_TYPO_RES = {
    re.compile(rf"\b{re.escape(bad)}\b", re.IGNORECASE): good
    for bad, good in TYPO_FIXES.items()
}

# A leaked trailing list/section number: a completed sentence ("... word.") with a
# stray "<n>." appended (the next topic's number bled in during extraction, e.g.
# "... blood groups. 5."). Conservative: requires the preceding sentence period and
# a 1-2 digit number, so a genuine trailing number ("... base 10.") is untouched.
_LEAKED_TRAILING_NUM_RE = re.compile(r"\.\s+\d{1,2}\.?\s*$")

# Trailing CXC list-connector noise: ";", ".", ",", and a dangling "and"/"or" left
# from list-formatted objectives ("...active transport; and,"). Stripped iteratively
# in addition to the plain ';'/'.' terminators.
_TRAILING_PUNCT_RE = re.compile(r"[;.,\s]+$")
_TRAILING_CONNECTOR_RE = re.compile(r"\b(?:and|or)$", re.IGNORECASE)


def clean_content_stmt(raw: str) -> str:
    """Normalise an objective statement for the live-DB convention.

    Order: (1) rejoin known no-hyphen word-splits; (2) drop a leaked trailing
    section number; (3) iteratively strip trailing ';'/'.'/',' and dangling
    'and'/'or' connectors; (4) capitalize the first letter. Embedded quotes/commas
    and internal casing are preserved."""
    s = (raw or "").strip()

    # (1) word-split rejoin (2a) + curated single-token typo corrections
    for rx, good in _WORD_SPLIT_RES.items():
        s = rx.sub(good, s)
    for rx, good in _TYPO_RES.items():
        s = rx.sub(good, s)

    # (2) leaked trailing section/list number
    s = _LEAKED_TRAILING_NUM_RE.sub(".", s).strip()

    # (3) trailing punctuation + list connectors (2b), iterated to stability
    while True:
        before = s
        s = _TRAILING_PUNCT_RE.sub("", s)
        s = _TRAILING_CONNECTOR_RE.sub("", s).rstrip()
        if s == before:
            break

    if s:
        s = s[0].upper() + s[1:]
    return s


def extract_command_words(text: str) -> list[str]:
    """Whole-word, case-insensitive scan for seed verbs, in order of first
    appearance in the text. Returns canonical Title-Case verbs."""
    hits: list[tuple[int, str]] = []
    for verb in COMMAND_VERBS:
        m = re.search(rf"\b{re.escape(verb)}\b", text, re.IGNORECASE)
        if m:
            hits.append((m.start(), verb))
    hits.sort(key=lambda t: t[0])
    return [verb for _, verb in hits]


def derive_skill_type(text: str) -> str:
    """Highest-precedence category whose verb set appears in the text, else
    UNCLASSIFIED. Never guesses a default."""
    low = (text or "").lower()

    def has_any(verbs: set[str]) -> bool:
        return any(re.search(rf"\b{re.escape(v)}\b", low) for v in verbs)

    if has_any(SKILL_APPLICATION):
        return "Application"
    if has_any(SKILL_UNDERSTANDING):
        return "Understanding"
    if has_any(SKILL_KNOWLEDGE):
        return "Knowledge"
    return UNCLASSIFIED


# ---------------------------------------------------------------------------
# Master-map -> rows
# ---------------------------------------------------------------------------

def detect_tail_truncation(objective_records) -> list[dict]:
    """Flag sections whose highest-numbered objective looks mid-list, not final.

    CXC syllabi terminate a section's LAST specific objective with '.'; every
    earlier objective ends ';' or '; and,'. So if a section's highest-numbered
    objective's RAW text (before clean_content_stmt strips the terminator) ends
    with ';' or ',', the extraction very likely dropped that section's TAIL -- the
    exact silent failure that hid ECON-2.12/2.13 and ECON-5.7 (interior-gap checks
    can't see missing tail objectives). This is a WARNING, not a hard failure: an
    OCR quirk could leave a genuinely-last objective mid-sentence, so a human
    verifies against the source PDF rather than the build aborting.

    Returns one dict per suspect section: {section_num, objective_id, raw_text,
    terminal}."""
    last_per_section: dict[str, tuple[tuple[int, str], dict, str]] = {}
    for r, sec_num, _title, _supp in objective_records:
        obj_num = (r.get("objective_number") or "").strip()
        key = _numeric_key(obj_num)
        if sec_num not in last_per_section or key > last_per_section[sec_num][0]:
            last_per_section[sec_num] = (key, r, obj_num)

    warnings: list[dict] = []
    for sec_num in sorted(last_per_section, key=_numeric_key):
        _key, r, obj_num = last_per_section[sec_num]
        raw = (r.get("objective") or "").strip()
        if raw.endswith(";") or raw.endswith(","):
            warnings.append({
                "section_num": sec_num,
                "objective_id": None,  # prefix filled by caller
                "objective_num": obj_num,
                "raw_text": raw,
                "terminal": raw[-1],
            })
    return warnings


def load_master_map(path) -> list[dict]:
    """Read the master-map CSV into a list of dict rows (utf-8-sig tolerant)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def load_supplement(path) -> list[dict]:
    """Read a manual-supplement CSV into master-map-shaped records.

    The supplement holds objectives the upstream extraction MISSED but a human has
    confirmed against the official syllabus. Its columns are deliberately small:
        subject, section_num, section_title, objective_number, objective
    Each row is converted to the same shape as a master-map row (context built as
    ``SECTION {n}: {title}``) so it flows through build_syllabus_rows unchanged --
    identical derivation, canonicalization, and duplicate-guarding. Blank rows are
    skipped so the file can carry comment/spacer lines."""
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            subject = (r.get("subject") or "").strip()
            sec_num = (r.get("section_num") or "").strip()
            title = (r.get("section_title") or "").strip()
            obj_num = (r.get("objective_number") or "").strip()
            objective = (r.get("objective") or "").strip()
            if not (subject and sec_num and obj_num and objective):
                continue
            out.append({
                "subject": subject,
                "context": f"SECTION {sec_num}: {title}",
                "objective_number": obj_num,
                "objective": objective,
            })
    return out


def build_syllabus_rows(records: list[dict], subject: str,
                        supplement_records: list[dict] | None = None,
                        exam_weight: str = EXAM_WEIGHT_PLACEHOLDER,
                        ) -> tuple[list[dict], dict]:
    """Convert master-map records for ``subject`` into output rows + a report.

    ``supplement_records`` are human-confirmed objectives missed by the extraction;
    they are merged in and treated identically (same derivation + duplicate guard),
    and the report flags which output rows came from the supplement.

    ``exam_weight`` is applied verbatim to every row. It defaults to 'TBD' -- the
    Paper 1/Paper 2 split is a per-subject builder decision, never inferred from
    text. (Economics is set to 'Both': its syllabus makes every objective eligible
    for both Paper 01 and Paper 02, unlike POB's mixed P1/Both convention.)

    Raises ValueError on a duplicate (section_num, objective_number) within the
    subject (loud failure -- no silent dedupe) or when the subject has no rows.
    """
    prefix = prefix_for(subject)  # raises on unknown subject

    subj_records = [r for r in records if (r.get("subject") or "").strip() == subject]
    if not subj_records:
        raise ValueError(
            f"no rows for subject '{subject}' in the master map "
            f"(check the --subject value matches the master map's 'subject' column)"
        )
    supp_records = [r for r in (supplement_records or [])
                    if (r.get("subject") or "").strip() == subject]

    # Split SECTION objective rows from excluded ("General"/unmatched) rows. The
    # 4th tuple element flags supplement provenance so the report can surface it.
    objective_records: list[tuple[dict, str, str, bool]] = []
    excluded: list[tuple[str, str]] = []                 # (context, objective_text)
    for r, is_supp in ([(x, False) for x in subj_records]
                       + [(x, True) for x in supp_records]):
        context = (r.get("context") or "").strip()
        m = SECTION_RE.match(context)
        if m:
            objective_records.append((r, m.group(1), m.group(2).strip(), is_supp))
        elif not is_supp:
            excluded.append((context, (r.get("objective") or "").strip()))

    # Canonicalize section titles: one title per section_num = the longest variant.
    title_variants: dict[str, set[str]] = defaultdict(set)
    for _r, sec_num, title, _supp in objective_records:
        title_variants[sec_num].add(title)
    canonical_title: dict[str, str] = {}
    disagreements: dict[str, dict] = {}
    for sec_num, variants in title_variants.items():
        # longest wins; tie-break on string for determinism.
        best = max(variants, key=lambda t: (len(t), t))
        canonical_title[sec_num] = best
        if len(variants) > 1:
            disagreements[sec_num] = {"chosen": best, "variants": sorted(variants)}

    # Duplicate (section_num, objective_number) -> raise, listing every one.
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for r, sec_num, _title, _supp in objective_records:
        obj_num = (r.get("objective_number") or "").strip()
        key = (sec_num, obj_num)
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    if duplicates:
        listed = "\n  - ".join(
            f"section {s}, objective {o}  ->  {prefix}-{s}.{o}"
            for s, o in sorted(set(duplicates))
        )
        raise ValueError(
            f"duplicate (section_num, objective_number) pairs found for "
            f"'{subject}' -- refusing to write output. A duplicate means a real "
            f"extraction error or two objectives sharing a number; a human must "
            f"resolve it:\n  - {listed}"
        )

    # Build output rows.
    rows: list[dict] = []
    unclassified = 0
    supplement_ids: list[str] = []
    for r, sec_num, _title, is_supp in objective_records:
        obj_num = (r.get("objective_number") or "").strip()
        raw_obj = (r.get("objective") or "").strip()
        skill = derive_skill_type(raw_obj)
        if skill == UNCLASSIFIED:
            unclassified += 1
        objective_id = f"{prefix}-{sec_num}.{obj_num}"
        if is_supp:
            supplement_ids.append(objective_id)
        rows.append({
            "section_id": f"{prefix}-S{sec_num}",
            "section_num": sec_num,
            "section_title": canonical_title[sec_num],
            "objective_id": objective_id,
            "objective_num": obj_num,
            "content_stmt": clean_content_stmt(raw_obj),
            "skill_type": skill,
            "command_words": "|".join(extract_command_words(raw_obj)),
            "exam_weight": exam_weight,
        })

    # Deterministic numeric order by (section, objective) so merged supplement rows
    # land in their right place.
    rows.sort(key=lambda row: (_numeric_key(row["section_num"]),
                               _numeric_key(row["objective_num"])))

    # Tail-truncation guard: would have caught the dropped S2/S5 objectives.
    truncation_warnings = detect_tail_truncation(objective_records)
    for w in truncation_warnings:
        w["objective_id"] = f"{prefix}-{w['section_num']}.{w['objective_num']}"

    report = {
        "subject": subject,
        "prefix": prefix,
        "objectives_written": len(rows),
        "excluded": excluded,
        "excluded_count": len(excluded),
        "title_disagreements": disagreements,
        "section_count": len(canonical_title),
        "unclassified_count": unclassified,
        "exam_weight": exam_weight,
        "supplement_count": len(supplement_ids),
        "supplement_ids": sorted(
            supplement_ids,
            key=lambda i: (_numeric_key(i.split("-")[1].split(".")[0]),
                           _numeric_key(i.split(".")[-1]))),
        "truncation_warnings": truncation_warnings,
    }
    return rows, report


def write_csv(rows: list[dict], output_path) -> Path:
    """Write rows to ``output_path`` with the exact OUTPUT_COLUMNS header."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return output_path


# ---------------------------------------------------------------------------
# Reporting / CLI
# ---------------------------------------------------------------------------

def print_excluded(report: dict) -> None:
    """Print every excluded row to stderr so nothing disappears silently."""
    for context, text in report["excluded"]:
        label = context or "(empty context)"
        print(f"EXCLUDED [{label}]: {text}", file=sys.stderr)


def print_truncation_warnings(report: dict) -> None:
    """Print tail-truncation suspicions loudly to stderr (warning, not failure)."""
    for w in report.get("truncation_warnings", []):
        print(
            f"WARNING: possible tail truncation in section {w['section_num']} -- "
            f"its last objective {w['objective_id']} ends with '{w['terminal']}' "
            f"not '.', so the extraction may have dropped later objective(s). "
            f"Verify against the source PDF before locking.\n"
            f"         last objective text: {w['raw_text']!r}",
            file=sys.stderr,
        )


def print_summary(report: dict, output_path) -> None:
    print("\n" + "=" * 70)
    print(f"build_syllabus_csv -- {report['subject']} ({report['prefix']})")
    print("=" * 70)
    print(f"Objectives written : {report['objectives_written']}")
    print(f"Sections           : {report['section_count']}")
    print(f"Rows excluded       : {report['excluded_count']} "
          f"(General / non-SECTION context -- listed on stderr above)")

    disagreements = report["title_disagreements"]
    if disagreements:
        print(f"\nSection title disagreements resolved (longest kept): "
              f"{len(disagreements)}")
        for sec_num in sorted(disagreements, key=lambda s: (len(s), s)):
            info = disagreements[sec_num]
            print(f"  section {sec_num} -> chose: {info['chosen']!r}")
            for v in info["variants"]:
                if v != info["chosen"]:
                    print(f"      also seen (dropped): {v!r}")
    else:
        print("\nSection title disagreements: none")

    print(f"\nUNCLASSIFIED skill_type : {report['unclassified_count']} "
          f"(no seed command verb found -- not defaulted)")

    tw = report.get("truncation_warnings", [])
    if tw:
        print(f"\n*** POSSIBLE TAIL TRUNCATION : {len(tw)} section(s) "
              f"(see WARNING lines on stderr) ***")
        for w in tw:
            print(f"  ! section {w['section_num']} last objective {w['objective_id']} "
                  f"ends with '{w['terminal']}' -- verify against source PDF")
    else:
        print("\nTail-truncation check : OK (every section's last objective ends '.')")

    if report.get("supplement_count"):
        print(f"\nSupplement rows merged (human-confirmed, not from extraction): "
              f"{report['supplement_count']}")
        for oid in report["supplement_ids"]:
            print(f"  + {oid}")

    print(f"\nOutput written: {Path(output_path)}")
    weight = report.get("exam_weight", EXAM_WEIGHT_PLACEHOLDER)
    if weight == EXAM_WEIGHT_PLACEHOLDER:
        print("\nREMINDER: exam_weight is 'TBD' for every row. Complete Paper 1/Paper 2 "
              "weighting\n          by hand against the CXC syllabus PDF before locking "
              "the subject.")
    else:
        print(f"\nexam_weight set to '{weight}' for every row (builder decision, "
              "applied verbatim).")
    print("=" * 70)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Convert the official syllabus master map into the CSV "
                    "syllabus_parser.py expects (build-time; never touches the DB).",
    )
    ap.add_argument("--subject", required=True, help="e.g. Economics")
    ap.add_argument("--master-map", required=True,
                    help="path to _official_syllabus_objective_master_map.csv")
    ap.add_argument("--output", required=True,
                    help="output CSV path, e.g. backend/ingest_v2/syllabus_csvs/economics.csv")
    ap.add_argument("--supplement",
                    help="optional CSV of human-confirmed objectives the extraction "
                         "missed (columns: subject, section_num, section_title, "
                         "objective_number, objective); merged + derived identically")
    ap.add_argument("--exam-weight", default=EXAM_WEIGHT_PLACEHOLDER,
                    help="value applied verbatim to every row's exam_weight column "
                         "(default 'TBD'). Set explicitly per subject, e.g. 'Both' for "
                         "Economics. Never inferred from text.")
    args = ap.parse_args(argv)

    master_path = Path(args.master_map)
    if not master_path.is_file():
        sys.exit(f"ERROR: master map not found: {master_path}")

    records = load_master_map(master_path)

    supplement_records = None
    if args.supplement:
        supp_path = Path(args.supplement)
        if not supp_path.is_file():
            sys.exit(f"ERROR: supplement not found: {supp_path}")
        supplement_records = load_supplement(supp_path)

    rows, report = build_syllabus_rows(  # raises on duplicates
        records, args.subject, supplement_records=supplement_records,
        exam_weight=args.exam_weight)

    print_excluded(report)
    print_truncation_warnings(report)
    write_csv(rows, args.output)
    print_summary(report, args.output)


if __name__ == "__main__":
    main()
