# Subject Onboarding Playbook

This is the procedure for onboarding subjects 2 through 7 through
ingest_v2. It encodes everything learned getting Economics through the
pipeline, so future subjects don't rediscover the same problems.

## The three-role model — read this first

This process has three participants, and confusing their jobs is the
single biggest risk of automating this badly:

1. **Claude Code** (this session) — runs commands, writes files, reports
   data. Does NOT make judgment calls about whether content is correct,
   complete, or pedagogically sound.

2. **The orchestrating Claude** (Anthropic's Claude in the chat
   interface, working with the project owner) — does the actual
   technical verification: reading source documents, cross-referencing
   ambiguous content against canonical syllabus text, resolving mapping
   disputes. This role exists because the project owner is explicitly
   not a subject-matter expert and cannot personally verify economics,
   math, or science content.

3. **The project owner** — gives go/no-go decisions based on a
   plain-language summary from the orchestrating Claude. Is NOT expected
   to read a CXC syllabus PDF, verify objective wording, or adjudicate
   topic-mapping disputes personally. Their approval is a business
   decision ("yes, proceed" / "no, stop"), not a technical review.

**Practical effect:** when this playbook says "requires human approval,"
it means the orchestrating Claude has already done the verification and
is asking the project owner to confirm direction — not asking them to
personally check the content. Claude Code should produce reports that
the orchestrating Claude can act on, not reports aimed at the project
owner directly doing technical review.

## Phase 0 — Pre-flight (5 minutes, mostly automated)

Before starting any subject, get these answers on record. Most are
answerable by Claude Code alone from existing data; one needs an
explicit decision.

- [ ] **Corpus location.** Confirm `Organized_CSEC_2027\{Subject}\`
  exists and follows the standard skeleton (Syllabus / Notes / Past
  Papers / Mark Schemes / Specimen Papers / Practice Questions / SBA).
  Flag any structural deviation immediately — don't discover it three
  steps later.
- [ ] **Master map coverage.** Filter
  `_official_syllabus_objective_master_map.csv` to the subject. Report
  row count and section count. This is informational only — it is NOT
  the final objective count (see Phase 1).
- [ ] **MCQ bank presence.** Does
  `Practice Questions\Kerwin Springer\*.json` exist for this subject?
  If yes, report its topic list and total question count. If no, note
  that Phase 4 (MCQ mapping) doesn't apply to this subject.
- [ ] **Exam structure — REQUIRES ORCHESTRATING CLAUDE INPUT, NOT
  AUTOMATED.** Before any exam_weight values get set, the orchestrating
  Claude must check the actual subject syllabus PDF for its exam
  structure: does this subject have a genuine per-objective Paper
  1-only / Paper 2-only split (like POB), or is every objective eligible
  for both papers (like Economics)? Claude Code should NOT default
  exam_weight to "Both" or "TBD" without this being explicitly answered
  first. Surface this as the first question to the orchestrating Claude,
  before touching the converter.

## Phase 1 — Syllabus CSV conversion (mostly automated)

**Fully automated, no approval needed:**
- Run `build_syllabus_csv.py --subject {Subject} --master-map ...`
- The tail-truncation check (`detect_tail_truncation()`) runs
  automatically and prints a loud warning for any section whose
  highest-numbered objective doesn't end in `.`
- skill_type / command_words derivation runs automatically from the
  seed verb table (already calibrated against POB + Economics; revise
  the table only if a new subject's verbs systematically don't fit —
  flag this to the orchestrating Claude if UNCLASSIFIED count is above
  ~5% of objectives)

**Requires orchestrating Claude verification (not project-owner
review):**
- ANY tail-truncation warning. Do not proceed past this without
  resolving it. The fix pattern that works:
  1. Try `cxc.org` directly via web_fetch first — it is usually
     robots-blocked, expect this to fail.
  2. Search for the same syllabus document number (format:
     "CXC NN/G/SYLL YY") hosted by a Caribbean ministry-of-education
     mirror (e.g. education.gov.gy has worked before). These mirrors
     are not robots-blocked and typically have the complete document
     with the same page/section structure.
  3. Locate the missing objective(s) by section and number, extract
     verbatim wording, add to a `{subject}_supplement.csv` file
     (pattern: see `economics_supplement.csv`), regenerate.
  4. Cross-check the corrected section's total count against what the
     canonical document shows for that section — don't just patch the
     one flagged gap; confirm the WHOLE section length is now right.
     (Economics had THREE gaps in one section that surfaced one at a
     time across multiple passes — check the full section count once,
     not gap-by-gap.)
- The exam_weight decision from Phase 0, once the orchestrating Claude
  has actually checked the syllabus's exam structure.
- Any section-title disagreement the converter logs (multiple title
  variants for one section_num) — usually resolves itself by picking
  the longest variant, but worth a one-line confirmation it's not
  picking a wrong truncation.

## Phase 2 — The manual lock gate (REQUIRED, not automatable)

This is Design Rule 11 from the PDR and is not eligible for automation.
It is the architectural substitute for subject-matter verification the
project owner cannot personally provide. What changes across subjects
is how much work happens BEFORE reaching this gate (ideally: very
little, if Phase 1 ran clean) — not whether the gate itself happens.

Manual steps (run by the project owner, in PowerShell):
```powershell
launch\backup.bat
python backend\db\syllabus_parser.py --subject {Subject} --csv-file "backend\ingest_v2\syllabus_csvs\{subject}.csv"
python backend\db\export_for_review.py --subject {Subject}
```
Then: the orchestrating Claude reviews the generated Excel file's
content against a quick cross-check of the canonical syllabus text
(NOT the project owner doing this personally) and reports a plain-
language "looks right" or "found an issue" back to the project owner.
Only then:
```powershell
python backend\db\lock_subject.py --subject {Subject}
```

## Phase 3 — MCQ topic mapping (if Phase 0 found a bank)

**Automated — Claude Code does this without approval:**
- Extract every unique (topic, subtopic) pair + count from the bank
  JSON, write to a CSV file (not chat output — chat-pasted tables
  truncate unreliably above ~50 rows, this has bitten this project
  before).
- Extract the full locked objectives list (objective_id, section_id,
  AND content_stmt) to a CSV file, same reason.

**Requires orchestrating Claude verification, not automated, not
project-owner review:**
- The actual topic→objective mapping judgment calls. CRITICAL LESSON
  FROM ECONOMICS: do this by fetching the FULL canonical syllabus text
  (the detailed CONTENT column, not just the short content_stmt
  headline stored in the DB) BEFORE attempting any mapping — not as a
  fallback after Claude Code reports "uncertain." Of 20 items Economics
  flagged uncertain on a content_stmt-only pass, 18 resolved instantly
  once the orchestrating Claude checked the syllabus's full CONTENT
  text. Front-load this step; don't treat full-text lookup as an
  escalation path.
- ANY objective_id a mapping proposes must be checked against the
  ACTUAL locked objectives list before being written to the YAML —
  Economics had two proposed mappings point at objective_ids that
  didn't exist (a downstream symptom of the Phase 1 tail-truncation gap
  not being fully caught). Claude Code should always run an existence
  check before writing YAML, never trust a citation without verifying
  it against the live DB.

**Then, automated again:**
- Write the YAML, run `--dry-run --adapter kerwin_mcq`, report
  confidence distribution. Target: 0 review-queue entries, since every
  pair should now resolve. If anything still routes to review after
  the full-text mapping pass, that's worth a second look, not an
  automatic accept.

## Phase 4 — Real ingestion (automated, with one report-back)

```powershell
launch\backup.bat
curl http://localhost:11434/api/tags -UseBasicParsing
python -m backend.ingest_v2 --subject {Subject}
```
Fully automated. Report the summary. Triage the review-queue and
no-adapter counts against these accepted categories (no orchestrating
Claude review needed if they fit a known pattern; flag anything that
doesn't):

| Category | Accept without review if... |
|---|---|
| `no_objective_match_via_keywords` | Same mechanism as POB's existing backlog — accept as-is |
| Image-only scanned PDFs, 0 extractable text | Note as an OCR backlog item, don't block on it |
| Index/coverage/resource-map `.md`/`.txt` files (e.g. `_subject_objective_coverage.md`) | Always benign, never real content |
| Single placeholder notes pointing at an official CXC bundle | Benign, no content to extract |
| A new, unfamiliar review-queue reason string | STOP — surface to orchestrating Claude, don't assume it's the same as a known category |
| A no-adapter file that isn't an index/placeholder type | STOP — may indicate a real adapter gap, not benign |

## Known issues carried over from Economics (don't rediscover these)

- `open_db()` requires an explicit `db_path` argument — CLAUDE.md's
  documented sqlite-vec pattern shows it as zero-arg; the live code has
  drifted from the doc. Use
  `open_db(os.getenv("DB_PATH"))`.
- PowerShell one-liners with nested quotes are unreliable on this
  machine. Prefer here-strings (`@' ... '@ | Out-File`) for anything
  beyond a single simple command, or write a throwaway `.py` file and
  run it.
- `lock_subject.py` does not set `objectives.verified` — it never has.
  That flag is set manually, out-of-band, and only matters for the
  Excel review display column. If new objectives get added to an
  already-locked subject later, their `verified` flag needs a manual
  UPDATE; nothing currently does this automatically.
- A subject's chunks/MCQs can have a real structural gap hiding behind
  an "uncertain mapping" symptom — always check whether a flagged
  ambiguity traces back to a missing/incomplete syllabus before treating
  it as a pure judgment call.
