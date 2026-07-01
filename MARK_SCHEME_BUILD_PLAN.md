# CSEC AI Study Partner — Mark Scheme Extraction Build Plan

**Goal:** Give Rylee real, official-CXC grading on Paper 2 past-paper questions across all 7 subjects, so when she answers a question she gets pointed feedback on what she missed — not just "answer key not available."

**Source:** The CXC syllabus PDFs themselves embed the official specimen Paper 2 mark scheme (confirmed for Economics, pages 90–128 of `csec-economics-syllabus-revised-2017.pdf`). The format is structured per-part, with explicit mark allocations and Specific Objective (S.O.) references that map directly to existing `objective_id` values in the database.

**Why not CXC Subject Reports:** Reports contain examiner narrative commentary on exemplar responses, but the exemplars themselves are embedded as scanned images PyMuPDF cannot read, and the prose around them describes what good answers did rather than stating the actual mark points. Workable as a fallback but materially lower quality than the syllabus-embedded mark scheme. Deferred to Stage 3, possibly never needed.

---

## Design rules (extend, don't break, CLAUDE.md's existing rules)

- **Rule 1 still holds:** every mark point must resolve to a real `objectives.objective_id`. No exception for syllabus-bundle-derived points.
- **Rule 2 still holds:** the LLM does not decide grading. It can help during *extraction* (turning ambiguous prose into discrete mark points where formatting is unclear), but the grading runtime continues to use the existing schema-constrained boolean point-matching against the stored `mark_points` rows. The new build only adds rows; it does not change how grading runs against them.
- **Subject-matter verification standard (per memory):** Ricky cannot verify whether an extracted mark point is *correct* CXC content. Every extracted mark scheme must be reviewed against the source PDF before being marked `verified = 1` and made available to the quiz, the same discipline used for syllabus locks.

---

## What's already in place vs. what gets added

**Already in the schema:**
- `mark_points` table — `mark_point_id`, `objective_id`, `question_id`, `doc_id`, `point_text`, `marks_value`, `point_order`
- `chunks.question_num` column (often NULL on existing past-paper chunks — see Stage 0 fix)
- Existing grading flow (`grade.py` → schema-constrained boolean per mark point → Python `compute_score()`)

**What this build adds:**
- A parser that reads the embedded mark scheme pages from syllabus PDFs
- A reviewable interim CSV per subject so Ricky can verify before locking
- A lock script that promotes verified mark points into the `mark_points` table
- A quiz-picker query change so past-paper questions with verified mark schemes become gradeable

**What this build does NOT touch:**
- `grade.py` itself (already correct — once `mark_points` rows exist, grading just works)
- The Leitner scheduler (already correct — weakness writes are downstream of grading)
- Any other subject's lessons, retrieval, or visual pages

---

## Pipeline (per subject)

Mirrors the existing 3-gate model (syllabus → ingestion → lessons), so the same sequencing discipline applies:

```
GATE 1: extract        — parse syllabus PDF, produce reviewable CSV
GATE 2: verify         — Ricky reviews CSV against source PDF, corrects errors
GATE 3: lock           — verified CSV writes rows into mark_points; syllabus-bundle source marked as verified for that subject
GATE 4: wire           — quiz picker query updated to include questions backed by verified mark schemes
```

No subject gets quiz grading enabled until all four gates pass for that subject.

---

## Verification protocol — preconditions for advancing between stages

This section exists because the Economics build went through eight rounds of after-the-fact correction — each one finding a real problem the previous stage had declared "complete." The pattern was not random. Every layer verified the layer above it through internal-consistency checks (row counts reconcile, orphan tallies hit zero, tests pass) without ever comparing the data to its literal source. Internal consistency is necessary but does not demonstrate correctness — it demonstrates that whatever errors exist are applied uniformly. The protocol below is what catches the errors themselves, not just their distribution.

Three rules govern stage advancement. Every subject's pipeline must satisfy all three at every stage boundary before the next stage begins. These are preconditions, not best-effort guidelines. A stage that has not satisfied them is not complete, regardless of what its tally says.

**Rule 1 — Schema-level checks and content-level checks are independent and both must pass.**

Internal-consistency checks (row counts, orphan tallies, sum reconciliations, unit tests) confirm that the data is sorted, classified, and counted correctly relative to itself. They do not confirm that the data matches the real source. A separate content check must be performed before each stage is declared complete: a random sample of N rows from the just-completed stage, where N is at least 5 and at least 2% of the affected row set, is displayed side-by-side with the corresponding literal page content of the source PDF. The check is performed by the human reviewer (the builder), not by Claude Code or any LLM, and the comparison is character-for-character against the PDF page, not against a summary or paraphrase of it. Discrepancies — even small ones — are treated as evidence that the rest of the stage's output may share the same defect, and trigger a wider audit before advancing.

Cost: roughly one minute per row. Five rows takes five minutes. The Economics build cost approximately six hours of redo work that would have been prevented by twenty minutes of sampled content checks spread across the preceding stages. The asymmetry is the point.

This rule applies even when the data was generated by a process that has worked correctly on a previous subject. Each subject's source PDFs have different structures, different conventions, and different failure modes; one subject's clean pipeline run is not evidence that the next subject's will produce clean output.

**Rule 2 — No row may exist whose source cannot be cited.**

Every row written to chunks, mark_points, or any table whose contents are served to the student or used in grading must carry a non-null source_page (or equivalent traceability field) referencing a specific page of a specific ingested document. Rows whose source_page is NULL, whose source cannot be located in any ingested document, or whose content was generated by inference, paraphrase, reconstruction, or LLM-assisted plausibility-matching against other rows are forbidden. The lock script, the ingest pipeline, and the quiz picker must each reject such rows at write time and at read time — not warn, reject.

The Economics qb1-qb6 stem fabrication (21 chunks written from inferred question text, with source_page = NULL on every one, live in the quiz picker before being caught) is the specific failure this rule prevents. The check is structural and runs without human intervention; it requires only that the relevant write paths assert the source_page is populated and the relevant read paths skip rows where it is not. Implementation cost is single-digit lines per affected script.

This rule does not preclude the use of LLM extraction during Stage 1's mark-point parsing — that use is constrained, schema-bounded, and produces rows whose source_page is the page the LLM was asked to read. It precludes using an LLM to generate content for a row whose corresponding source page does not contain that content.

**Rule 3 — Block-boundary sanity check is a Stage 1 exit gate, not a discovery made later.**

After every Stage 1 extraction completes, before any Stage 2 review work begins, an automated check runs: for each question_block_id in the produced CSV, report whether all rows with that block_id come from contiguous source pages (gap ≤ 1 page between consecutive page values when sorted). Any block_id whose rows span non-contiguous pages — for example pages 90, 95, 100, 107, 114 all sharing block_id=7 — is reported as a parser overrun candidate and Stage 1 does not exit until each such block is either re-extracted with corrected boundary detection or explicitly accepted as a known multi-document block with documented reason.

The Economics build absorbed three separate documents into block 7 (the 2016 Q6 mark scheme on page 97, exam booklet boilerplate on pages 98-105, the 2005 specimen question paper on pages 107-111, the 2006 Paper 03/2 content on pages 114-116) and the resulting structural mislabeling was not detected until Stage 4 stem ingestion six stages later, by which point ~316 mark_points rows had been written under wrong block identifiers and the fanout/lock/contamination work all had to be re-evaluated against the corrected block structure. The check that would have surfaced this on day one is two queries: GROUP BY question_block_id, then check max(page) - min(page) against COUNT(DISTINCT page) for each group.

This rule extends naturally to subjects whose mark schemes are interleaved with question papers in the same PDF, which is most of them. The contiguous-page heuristic catches the common parser failure mode (running past a document boundary) without requiring the extractor to understand what a document boundary is.

These three rules apply retroactively to subjects already in progress. The Economics build's current state must be audited against Rules 1, 2, and 3 before continuing — the audit is part of the remaining Economics work queue, not a separate task. Subsequent subjects (Mathematics, Principles of Accounts, Integrated Science, Information Technology, English, and any others) must satisfy all three rules at every stage boundary from their first extraction onward. Per-subject status tables in this document should add a column showing each rule's verification status per stage, so the audit state is visible at a glance and cannot be lost between sessions.

---

## Stage 0 — Prerequisite: `question_num` backfill (one-time, all subjects)

**Problem:** `chunks.question_num` is NULL for most past-paper chunks because `ingest.py`'s generic chunker doesn't reliably parse question boundaries. Without `question_num` populated, the quiz picker can't join past-paper questions to mark scheme rows.

**Fix:**

1. Create `tools/backfill_question_num.py`:
   - Iterates `documents` where `content_type = 'past_paper'`
   - Re-opens each PDF with PyMuPDF
   - For each chunk belonging to that doc, finds which page the chunk text appears on, then searches that page for the nearest `Question \d+` header above the chunk start
   - Also extracts the part label `(a)`, `(a)(i)`, `(b)`, etc. when present and stores as a separate `chunks.question_part` column (add this column via a new migration)
   - Updates `chunks.question_num` and `chunks.question_part` in place. Idempotent — re-runs are safe.
   - Outputs a CSV of any chunk where the question/part could not be determined, for manual review.

2. Migration `m020_chunks_question_part.sql`:
   ```sql
   ALTER TABLE chunks ADD COLUMN question_part TEXT;
   ```

3. Test in `tests/test_backfill_question_num.py`:
   - Mocked PyMuPDF returning known page text patterns
   - Asserts a chunk on a page containing "Question 3" is assigned `question_num = '3'`
   - Asserts a chunk near "(b)(ii)" is assigned `question_part = '(b)(ii)'`
   - Asserts an unmatched chunk goes to the review CSV, not silently set to NULL

This stage runs once across all 7 subjects and is shared infrastructure for everything that follows.

---

## Stage 1 — Extract (Economics first, all subjects after)

**Goal:** Read the syllabus PDF's embedded mark scheme pages, produce a reviewable CSV. Write nothing to `mark_points` yet.

### Step 1.1 — Identify mark scheme pages per subject

For each subject, the syllabus PDF embeds its specimen mark scheme on a specific page range. Economics is confirmed at pages 90–128. The other six need confirmation by inspection — they are not guaranteed to follow the exact same layout.

Create `tools/locate_mark_scheme_pages.py`:
- Takes a syllabus PDF path
- Scans page-by-page for the marker `Keys and Mark Scheme` (or close variants: `Mark Scheme`, `Keys / Mark Scheme`)
- Returns the page range from the first marker to the next major section header (e.g. `Recommended Readings`, end of PDF, or an explicit `End of Mark Scheme` marker)
- Prints the detected range and the first 200 chars of each page in the range for visual confirmation
- Outputs to console only — does not write anything yet

Run this script once per subject. Record the confirmed page range for each in `tools/mark_scheme_page_ranges.json`:
```json
{
  "Economics":              {"pdf": "...csec-economics-syllabus-revised-2017.pdf", "pages": [90, 128]},
  "Principles_of_Business": {"pdf": "...",                                          "pages": [null, null]},
  "Mathematics":            {"pdf": "...",                                          "pages": [null, null]},
  ...
}
```

Subjects whose syllabus PDF does NOT embed a mark scheme fall back to Stage 3 (Subject Reports). Record those explicitly with a `"source": "subject_reports_pending"` note.

#### Document structure within the page range

A single detected page range can contain **multiple distinct embedded documents**, not one continuous mark scheme. The structural parser treats "Question N" headers as continuous across page boundaries, so a single inflated `question_block_id` can silently span multiple unrelated source documents within the same page range — and absorb non-mark-scheme content (exam cover instructions, calculator notices, booklet formatting rules) as if it were gradeable mark-scheme text.

For Economics (pages 90–128), the actual internal structure was:

| Pages | Content |
|---|---|
| 90–97 | Specimen 1 mark scheme (Q1–Q6) |
| 98–105 | Paper 03/2 question + answer booklet (unrelated assessment type) |
| 106–116 | Contains genuine mark-scheme content from multiple embedded specimen papers, not questions-only as originally assumed here — manual Stage 2 review verified pages 107–111 alone hold 79 real, verified mark points across multiple repeated Q6 occurrences (Section B essay alternatives) |
| 117–126 | 2005 specimen mark scheme (Q1–Q8) |
| 127–128 | Duplicate of Paper 03/2 mark scheme |

None of these internal boundaries were detected automatically. The parser produced a single continuous extraction with `question_block_id` counting all "Question N" headers in order across all five embedded documents, binding mark points from the 2005 specimen (blocks 8–15) to the same per-question accounting as Specimen 1 (blocks 1–6). This produced `ECON-6.9` with 116 mark points — nearly half the entire Economics extraction — for a single objective.

**Required checklist item for every subject:** after running `locate_mark_scheme_pages.py` and **before** running `extract_mark_scheme.py`:

1. Open the PDF and skim the full detected page range visually.
2. Look for document-boundary markers: paper codes (e.g. `01216032`), differing header/footer conventions, a gap where question numbering restarts from 1, or a change in layout style.
3. For each distinct embedded document found, decide **before extraction** whether it belongs in this subject's mark scheme lock or should be excluded by `excluded_reason`.

Do not wait until Stage 2 review to discover a structural overrun — by then, the extractor has generated rows bound to incorrect `question_block_id` ranges, and the per-row cleanup (setting `excluded_reason` on ~60 rows per embedded document) is significantly more work than a 5-minute pre-extraction skim.

#### `question_block_id` is a parsing artifact, not a content identifier (confirmed on Economics, 2026-07-01)

Even after the block-boundary contiguity check (Rule 3) passes and a subject's blocks look internally consistent, `question_block_id` is still only a counter over "Question N" headers encountered during structural parsing — it is not, by itself, evidence of which real CXC question (or part) a mark-scheme section actually answers. The Economics stem-locking work (`tools/lock_econ_specimen_stems.py`) surfaced a case the earlier block-realignment fix (`tools/fix_econ_q6_block_realignment.py`) had not fully resolved: a chunk had been inserted under the id `ECON-qb6(d)v1-stem` on the assumption (from block-ordering / page-proximity at extraction time) that it belonged to Question 6(d), when cross-checking against the real question-paper text (extracted separately by `tools/ingest_econ_specimen_questions.py`) showed it was actually Question 5(d) content. The id was corrected by rename, not by re-deriving it from the block structure again.

**Lesson for every subject, not just Economics:** block-id/page-proximity/ordering assumptions made at parse time are a starting hypothesis, not a verified fact. Before locking, cross-check mark-scheme content against the literal question-paper prompt text for the same question/part — the same discipline the Verification protocol's Rule 1 (content-level sampling against the source PDF) already requires, applied specifically to the block→question mapping itself. Whoever runs Stage 1 extraction on the next subject (Principles_of_Accounts, Mathematics, etc.) should expect this failure mode and budget time for it, not treat a passing block-boundary check as confirmation that block-to-question assignment is correct.

### Step 1.2 — Parse mark scheme structure

Create `tools/extract_mark_scheme.py`:

Input: a syllabus PDF + page range from `mark_scheme_page_ranges.json`
Output: a CSV at `04_REPORTS/{subject}_mark_scheme_review.csv` with these columns:

```
question_num   — "1", "2", "3", "4", "5"
question_part  — "(a)(i)", "(a)(ii)", "(b)", "(c)", "(d)", etc.
so_codes       — "1.6,1.8" (the S.O. references from the question header)
point_text     — the mark-point statement, plain text
marks_value    — integer (1, 2, 3, etc. — parsed from "1 mark", "2 marks", etc.)
point_order    — integer position within (question_num, question_part)
profile        — "KC" | "IA" | "APP" if present, else NULL
source_page    — the PDF page number this point came from
raw_excerpt    — the surrounding 200 chars of source text, for review
verified       — 0 (default; Ricky flips to 1 after manual review)
```

Parsing approach, in this order — try each, fall back to the next if the format diverges:

1. **Structural parse first** (deterministic, no LLM). For the Economics format documented in the diagnostic report:
   - `Question \d+` and `S.O: [\d.,\s]+` headers split the document into per-question sections.
   - `\([a-z]\)(\([ivx]+\))?` patterns identify parts and sub-parts.
   - Lines ending in `\d+ marks?` are mark allocations.
   - Bullet points (`•` or `-` or `\d+\.`) within a part are individual mark points.
   - Numeric answers like `"0 tons of sugar  1 mark"` are single mark points.

2. **LLM-assisted parse fallback** for sections where the structural parser yields fewer than expected mark points (e.g. a question allocating 6 marks but only 1 bullet detected). Use the existing pattern from `backend/db/extract_prose_markpoints.py` — send the section text to `ollama_chat` (or `gemini` if `CLOUD_MODE=1`) with a schema-constrained request to enumerate the mark points. The LLM **assists extraction only** — every output still goes into the CSV for manual review, never directly into `mark_points`.

3. **Flagged-for-review** if neither approach yields mark points equal to the question's total marks. Write a row with `point_text = "[REVIEW NEEDED: section parsed only N of M marks]"` so it surfaces in the CSV rather than silently dropping content.

#### mark_point_id formula — confirmed via Economics

The originally planned formula `{subject_prefix}-{objective_num}-q{question_num}{question_part}-mp{point_order}` produces collisions in two cases discovered during Economics extraction:

**(a) Same question_num reused across multiple embedded documents.** When a page range contains more than one specimen paper (see §1.1 above), "Question 1" appears once per document. Using `question_num` alone, all "Q1(a)(i) mp1" rows across documents share the same key regardless of source document.

**(b) Same part label repeated within one block.** CXC Section B essay questions present several alternatives under one "Question" header (e.g. Q6(a)/(b)/(c) for Alternative I and Q6(a)/(b)/(c) again for Alternative II). The part label `(a)` appears twice in the same block.

**Final confirmed formula:**

```
{subject_prefix}-{objective_num}-qb{question_block_id}{question_part}v{part_occurrence}-mp{point_order}
```

Where:
- `question_block_id` — globally-incrementing counter per "Question N" opener across the entire page range in document order (not reset per document)
- `part_occurrence` — counter scoped to `(question_block_id, question_part)` that increments on each repeat of the same part label within that block

Example: `ECON-1.6-qb1(a)(i)v1-mp2`, `ECON-6.9-qb6(a)v2-mp1`

Verified zero collisions against the real 324-row Economics extraction.

#### point_group_id — fanout deduplication key

A source row whose `mapped_objective_id` contains **multiple comma-separated objectives** (a mark point genuinely testing several objectives, as occurs in Section B essay blocks) must **fan out** into one `mark_points` row per objective. All fanned rows share one `point_group_id` so the grader can identify them as siblings.

`point_group_id` formula — positional fields only, no per-objective prefix or `objective_num`:

```
{subject_prefix}-qb{question_block_id}{question_part}v{part_occurrence}-mp{point_order}
```

This key must be derived deterministically from position alone — never a hash or "first sibling" reference, which does not survive re-locks where the objective list order or count may differ.

**Critical downstream requirement (grade.py):** `fetch_mark_points` must deduplicate by `point_group_id` before computing `total_marks` — one representative row per group feeds `compute_score` — then fan the judged result back out to **all** sibling `objective_id`s in `log_weakness`. Without deduplication, fanout inflates `total_marks` by N× (where N = number of co-tested objectives) on every multi-objective mark point. Confirmed would have produced ~4× inflated totals on every Section B essay question against the real Economics dataset.

The earlier `_first_obj()` approach (keeping only the first-listed objective, silently dropping the rest) is **wrong** — it starves every objective except the first in a multi-objective group of all grading evidence. Verified zero collisions and correct fanout against the real 552-row locked Economics dataset after the fix.

#### --dry-run behaviour requirement

`extract_mark_scheme.py` must accept a `--dry-run` flag that makes **zero live API calls** — including the LLM-fallback path. Under `--dry-run`, any section that would invoke the LLM fallback must instead print `[dry-run: skipping LLM fallback for Q{n}{part}]` and fall straight to a REVIEW NEEDED placeholder row. The first implementation called Gemini live under `--dry-run`; this was a bug.

Additional extraction behaviour requirements:
- Every section processed in live mode prints `Processing Q{n}{part} ({marks} marks)...` **before** any API call, so a long-running extraction is visibly progressing and distinguishable from a hang.
- LLM fallback calls are capped at **1 retry** with a **60-second timeout** enforced by a watchdog thread. On timeout or failure, fall through to a REVIEW NEEDED row; do not retry indefinitely.

### Step 1.3 — Map S.O. codes to objective_id

The mark scheme references S.O. codes like `1.6, 1.8`. The DB has `objective_id` values like `ECON-1.6`. The mapping is straightforward but should be confirmed, not assumed:

- For each S.O. code in the extracted CSV, look up the matching row in `objectives` where `subject_id = 'Economics'` and `objective_num = '1.6'`.
- If found, write the matched `objective_id` to a new CSV column `mapped_objective_id`.
- If multiple S.O. codes are listed for one question, write all of them as a comma-separated list (a question can test multiple objectives — that's normal).
- If no match is found, leave `mapped_objective_id` empty and flag in the review CSV.

**Cross-edition S.O. code references:** a specimen mark scheme may reference S.O. codes from an **older syllabus edition's section numbering**, not the currently-loaded syllabus. For Economics, codes `1.8, 2.17, 4.10, 6.15, 6.16` referenced the pre-2017-restructuring numbering scheme; the 2017 syllabus (8 sections) renumbered these and does not contain those objective numbers at all.

This is **not** a DB gap and must not be "fixed" by inventing new `objective_id` values. Before concluding a code is genuinely unresolvable:
1. Read the syllabus PDF body's specific-objectives listing directly (the section before the embedded mark scheme, not the DB) and confirm the number is absent.
2. Check whether the row's **other** valid S.O. codes (if any) still produce a valid `mapped_objective_id` — the unresolved code is simply dropped, not treated as a blocking error.

When an S.O. code genuinely refers to a retired objective, leave `mapped_objective_id` set from any other valid codes in the same row, note the dropped code in the CSV, and do not block Stage 2 verification on it.

### Row classification scheme

Every row in the review CSV must fall into **exactly one** of four mutually exclusive states. These are collectively exhaustive — any orphan (a row matching none) indicates a data error.

| State | `verified` | `parser_artifact` | `excluded_reason` | `needs_manual_entry` | `mapped_objective_id` |
|---|---|---|---|---|---|
| Ready for lock | `1` | `0` | empty | `0` | set |
| Parser artifact | `0` | `1` | empty | `0` | **must be empty** |
| Excluded | `0` | `0` | set | `0` | **must be empty** |
| Needs manual entry | `0` | `0` | empty | `1` | may be set |

**Ready for lock (`verified=1`):** content-verified rows. Stage 3 will promote these into `mark_points`.

**Parser artifact (`parser_artifact=1`):** structural rubric labels that the parser captured as if they were content — e.g. the word "Total", a bracket placeholder `[1]`, or "each" on its own line. `mapped_objective_id` must always be empty; these rows are excluded from the lock by design, not by content decision.

**Excluded (`excluded_reason` set, non-empty):** rows that are out of scope for this lock pass for a reason other than parser noise. Values used in Economics (each `excluded_reason` is a string stored verbatim — use these exact tokens for consistency):
- `out_of_scope_paper_03_2` — content belongs to a different paper or assessment type entirely
- `duplicate_of_block_8-15` — same source content captured under a different block due to a document-structure overrun (see §1.1)
- `contaminated_exam_instructions` — exam-cover boilerplate (e.g. "Answer ALL the questions", "Silent electronic calculators may be used", "Number each answer in your booklet correctly") absorbed by the parser as if it were mark-scheme content due to a document-structure overrun; confirmed produced **128 contaminated `mark_points` rows** in Economics before being caught by a targeted content audit
- `unanswerable_question_restatement` — text is real mark-scheme-adjacent content from the correct source page but only restates the question without supplying valid answers (e.g. "State a second consequence of inflation"); an LLM examiner cannot grade against it without a reference answer list

`mapped_objective_id` must always be **empty** when `excluded_reason` is set. The two fields are mutually exclusive: a row cannot simultaneously have a scope exclusion and a valid objective mapping.

**Needs manual entry (`needs_manual_entry=1`):** the parser under-captured marks, OR content was bundled into one oversized paragraph covering multiple distinct mark points that should eventually be split into separate gradeable rows. Unlike the other non-ready states, `mapped_objective_id` **may** be populated here — the objective assignment can be correct even when the point text is bundled. This state means "correct content, needs restructuring before grading splits credit correctly," not "wrong or missing content."

**Pre-lock completeness check:** before declaring Stage 2 review complete for any subject, verify:

```
verified=1 count
+ parser_artifact=1 count
+ excluded_reason-set count
+ needs_manual_entry=1 (verified=0) count
= total rows

orphans (rows matching none of the above) = 0
```

If orphans > 0, there are genuinely unreviewed rows that block Stage 3. Resolve them before proceeding.

### Step 1.4 — Reviewable Excel output

Create `tools/export_mark_scheme_review.py`:
- Reads the CSV from Step 1.2
- Exports to `04_REPORTS/{subject}_mark_scheme_review.xlsx`
- Header row bold, top row frozen, conditional formatting:
  - Rows where `verified = 0` highlighted yellow
  - Rows where `mapped_objective_id` is empty highlighted red
  - Rows with `[REVIEW NEEDED:` text in `point_text` highlighted orange
- Same pattern as the existing `export_for_review.py` for syllabus locks

### Step 1.5 — Tests

`tests/test_extract_mark_scheme.py`:
- A small fixture PDF (or mocked PyMuPDF returning canned text matching the Economics format)
- Asserts the structural parser correctly splits Q1 into 5 parts with correct mark allocations
- Asserts S.O. code parsing returns `["1.6", "1.8"]` from the header `S.O: 1.6, 1.8`
- Asserts a mismatched marks total triggers a flagged-for-review row, not a silent drop
- Asserts `mapped_objective_id` correctly resolves `1.6` → `ECON-1.6` given a populated `objectives` table

---

## Stage 2 — Verify (Ricky, manual)

Open `04_REPORTS/{subject}_mark_scheme_review.xlsx`. For each row:

- Open the source page of the syllabus PDF (CSV provides `source_page`).
- Compare `point_text` against the actual mark scheme on that page.
- Flag any of: misparsed text, wrong S.O. mapping, missed mark points, hallucinated points (especially from the LLM fallback in Step 1.2).
- Correct directly in the Excel sheet.
- Flip `verified` to `1` once a row is correct.
- Lock the subject only when **every row is `verified = 1`**, the same discipline as syllabus locks.

### Reviewer action guide

Each row in the review Excel corresponds to exactly one of the four states (see §1.2 Row classification scheme). Reviewer actions per state:

- **Ready for lock (`verified=1`):** you opened the source PDF at `source_page` and confirmed the `point_text` is a genuine mark-scheme answer point — not a question, rubric header, or exam-cover instruction. Only flip `verified` to `1` after this literal read.
- **Parser artifact (`parser_artifact=1`):** structural noise already flagged by the extractor. No action required unless the label is wrong (re-classify if the text is actually substantive).
- **Excluded (`excluded_reason` set):** confirm the reason is appropriate. Clear `mapped_objective_id` if it was accidentally populated — the two fields are mutually exclusive.
- **Needs manual entry (`needs_manual_entry=1`):** bundled content; leave deferred unless you have time to split and re-enter individual rows.

**CRITICAL LESSON (Economics build):** `verified=1` must mean a human actually read the `point_text` against the literal source PDF page and confirmed it is genuine mark-scheme content — **not** merely "this row's columns are internally consistent with the other three states." A bulk verification pass that sets `verified=1` based on classification logic alone ("has `mapped_objective_id`, not flagged artifact/excluded/manual — so mark verified") can and did let exam-booklet boilerplate through into locked `mark_points` with real `objective_id`s and `marks_value`s. **128 contaminated `mark_points` rows** (exam cover instructions, calculator notices) reached the live grading table this way before a targeted content audit caught them.

Before declaring Stage 2 complete, verify **two separate things**:

1. *(Necessary)* The four-state counts sum to total rows with zero orphans: `verified + parser_artifact + excluded + needs_manual_entry(verified=0) = total`.
2. *(Sufficient)* A sample of `verified=1` rows were actually read against source pages, not just classified. Passing check 1 does not imply check 2.

This stage is identical in spirit to the existing syllabus verification workflow. No new tooling required.

---

## Stage 3 — Lock

Create `tools/lock_mark_scheme.py`:

- Takes `--subject` CLI arg
- Reads the verified `04_REPORTS/{subject}_mark_scheme_review.xlsx` (re-import from Excel back to CSV/in-memory)
- Asserts every row has `verified = 1` — refuses to proceed otherwise
- Asserts every row has a non-empty `mapped_objective_id` — refuses to proceed otherwise
- Generates a stable `mark_point_id` for each row using the formula:
  `{subject_prefix}-{objective_num}-qb{question_block_id}{question_part}v{part_occurrence}-mp{point_order}`
  (e.g. `ECON-1.6-qb1(b)(i)v1-mp1`, `ECON-6.9-qb6(a)v2-mp1`)

  **Why this formula, not the simpler `q{num}{part}-mp{order}`:** the Economics
  specimen (pages 90–128) embeds two separate specimen papers back-to-back, and
  Section B presents multiple essay alternatives that repeat part labels (a)/(b)/(c)
  inside a single "Question 6" header. Without `question_block_id` (a globally-
  incrementing counter per "Question N" opener in document order) and
  `part_occurrence` (a per-block counter for repeated part labels within the same
  block), the simpler formula produces 16 collision keys — some mark_point_ids
  generated up to 11 times, with INSERT OR REPLACE silently keeping only the last.
  The full formula reduces collisions to zero.
- Inserts rows into `mark_points` with `INSERT OR REPLACE` (idempotent — re-running with corrections updates rather than duplicates)
- Records the lock event in a new `mark_scheme_locks` table (or reuses an existing audit table if one exists):
  ```sql
  CREATE TABLE IF NOT EXISTS mark_scheme_locks (
      subject_id   TEXT PRIMARY KEY REFERENCES subjects(subject_id),
      source_pdf   TEXT NOT NULL,
      page_range   TEXT NOT NULL,
      locked_at    TEXT DEFAULT (datetime('now')),
      row_count    INTEGER NOT NULL
  );
  ```
- Prints a final count of mark points inserted and confirms which `objective_id`s now have grading coverage

### Lock script robustness requirements (learned from Economics)

**(a) Atomic re-lock.** `lock_subject` must **DELETE** all existing `mark_points` for the subject before inserting the fresh batch, with the delete and all inserts inside the **same transaction** (rollback together on failure). `INSERT OR REPLACE` alone is insufficient: if `mark_point_id`'s formula changes between lock runs (it changed three times during Economics development), old-format rows become orphaned stale duplicates that `REPLACE` can never reach on a re-lock, silently inflating counts. Confirmed: an intermediate lock run left **164 stale orphaned rows** after a formula change, invisible until directly queried.

**(b) Migration timing — call `apply_runtime_migrations` both before AND after the lock write.** Any CLI script that opens the DB independently (outside `app.py`'s lifespan) must call `apply_runtime_migrations` at script start to normalise pre-existing rows. But this is insufficient: `lock_subject` performs a full DELETE+reinsert, so the fresh rows are in whatever state `build_question_id` produces — which does **not** include the `-stem` suffix the quiz picker requires. The Layer 2 `-stem` backfill in `apply_runtime_migrations` (`UPDATE mark_points SET question_id = question_id || '-stem' WHERE question_id NOT LIKE '%-stem'`) ran once before the lock, normalised the pre-existing rows, then the DELETE destroyed them and the reinsert created un-normalised rows that were never touched again. Confirmed: **all 552 Economics question_ids lacked `-stem`** on a real lock run until a second `apply_runtime_migrations(db)` call was added immediately after `lock_subject` in `main()`.

General principle: do not re-implement or inline DB connection/migration logic per script — import the canonical `apply_runtime_migrations` from `app.py`, and call it at every point where schema-dependent assumptions could be violated by that script's own writes, not just at script start.

---

## Stage 4 — Wire (quiz picker + grading)

Two small changes — the heavy lifting is already done elsewhere.

### Prerequisite: question stem chunks must exist before questions are visible

A locked `mark_points` row is **invisible to the quiz picker** until a matching `-stem` chunk exists in the `chunks` table with `chunk_id = mark_points.question_id`. The picker query joins on this exact equality — locking the mark scheme (Stage 3) does not make questions appear in the quiz UI; stem ingestion is a separate, required step that happens afterwards.

Question **stem text** (the prompt Rylee reads and answers) and mark scheme **answer text** (stored in `mark_points.point_text`) typically live on **different page ranges** within the same source PDF. Confirm both ranges explicitly per subject before starting extraction — do not assume they are adjacent or even in the same file. Confirmed for Economics: stem pages 106–116 (2005 specimen questions only), answer pages 90–97 (Specimen 1) and 117–126 (2005 specimen mark scheme).

Check this explicitly when declaring Stage 4 complete: a subject can have all `mark_points` fully locked and verified while having **zero visible quiz questions** if stems have not been ingested. Do not infer Stage 4 readiness from Stage 3 completion alone.

### Step 4.1 — Quiz picker query

In whichever route serves the quiz paper/year/question dropdown (likely `backend/app.py` near `/api/quiz/questions` or similar — confirm path during build), update the query so a past-paper question appears in the picker if **either** condition is true:

1. A `-stem` chunk exists for it (the existing POB path), **OR**
2. At least one `mark_points` row exists for the `(subject_id, paper, year, question_num, question_part)` combination via the existing FK join.

Both conditions producing a gradeable result means past-paper questions backed by the new specimen mark scheme become selectable for grading without requiring worked-solution PDFs.

### Step 4.2 — Grader: graceful "answer key partial" path

When Rylee submits an answer:
- If `mark_points` exist for the question's `(year, question_num, question_part)`, grade as usual.
- If `mark_points` exist for the `(year, question_num)` but not the specific `question_part`, grade against the question-level points and surface a note: "Graded against the question's overall mark scheme — part-specific guidance not yet available."
- If no `mark_points` at all exist, return the existing `{"error": "no_mark_scheme"}` response. (This path remains for questions outside any locked specimen.)

This means partial coverage degrades gracefully rather than going from "fully gradeable" to "completely unusable."

---

## Stage 5 — Roll out (subjects 2–7)

Run Stages 0–4 for each remaining subject one at a time, same order discipline as the existing rollout playbook. One subject's verification and lock completes before the next starts.

If any subject's syllabus PDF turns out NOT to embed a mark scheme (Step 1.1 finds no `Keys and Mark Scheme` marker), it falls to Stage 6.

---

## Stage 6 — Subject Reports fallback (deferred, only if Stage 5 leaves real gaps)

The first diagnostic report's plan (parse CXC Subject Reports for examiner-commentary-derived mark points) becomes Stage 6, executed **only if**:

- A subject's syllabus PDF has no embedded mark scheme, OR
- The specimen-only coverage from Stage 1 leaves objectives Rylee actually needs to study uncovered

Defer the design until Stage 5's results are in. The Subject Reports' lower quality (narrative prose, image-embedded exemplars, examiner-style variation) means this stage gets built only when its cost is justified by a real gap, not pre-emptively.

---

## Acceptance test (end-to-end, per subject)

The subject is considered ready when:

1. `mark_scheme_locks` has a row for that subject with `row_count > 0`
2. `mark_points` query for that subject returns rows linked to real `objectives.objective_id` values
3. Quiz picker shows at least one past-paper question for that subject (not just POB)
4. Rylee can submit an answer to that question and receive a point-by-point grading response, with the missed-points list referencing specific `mark_point_id` values
5. Submitting an obviously-wrong answer correctly returns a low score with the missed points enumerated
6. Submitting an obviously-correct answer matching the mark scheme returns a high score

The same VAL-05 (Point Grading) test from the original PDR applies — it just gets a second subject covered.

---

## What this build does NOT promise

- **Not full mark-scheme coverage.** Specimen mark scheme covers one year per subject. Past papers from other years rely on Stage 6 (Subject Reports) or remain ungradeable in Stage 1. The empty-state message change ("Specimen-year grading available; other years coming soon") may still be appropriate for some past-paper years.
- **Not zero-error grading.** Verification (Stage 2) is human review of LLM-assisted extraction. Errors will be caught only as well as Ricky reads the CSV against the source PDF. Same caveat as syllabus verification.
- **Not Paper 1 (MCQ) grading.** This build is Paper 2 only. The Kerwin Springer MCQ JSON found in the diagnostic (229 KB, full Paper 1 bank) is a separate, future build — it has its own clean structure and doesn't need this pipeline.

---

## Per-subject extraction results

### Economics

**Source:** `csec-economics-syllabus-revised-2017.pdf`, pages 90–128

**Internal document structure (discovered during Stage 2 review):**

| Pages | Content | Treatment |
|---|---|---|
| 90–97 | Specimen 1 mark scheme (Q1–Q6) | Locked |
| 98–105 | Paper 03/2 question + answer booklet | Excluded (`excluded_reason`) |
| 106–116 | 2005 specimen exam questions only — no answers | No mark points extractable |
| 117–126 | 2005 specimen mark scheme (Q1–Q8) | Locked |
| 127–128 | Duplicate of Paper 03/2 mark scheme | Excluded (`excluded_reason`) |

**Extraction:** 324 raw rows. Collapsed to 321 after splitting 2 bundled rows into single-point rows:
- Q7(b)(i): 6 parser fragments for "globalisation" + "trade liberalisation" definitions → 2 clean rows
- Q8(i): bundled CIF/FOB + nominal/real content → 2 separate rows

**Stage 2 initial state** (after splitting 2 bundled rows but before contamination audit):

| Category | Count |
|---|---|
| `verified=1` | 267 |
| `parser_artifact=1` | 34 |
| `excluded_reason` set | 12 |
| `needs_manual_entry=1` | 8 |
| **Total** | **321** |

**Contamination audit (discovered after initial lock):** the initial bulk-verification pass set `verified=1` on 115 rows in `question_block_id=7` (the large Section B essay block). A targeted content audit against the source PDF revealed that block 7 overlapped the document-structure boundary into pages 98–116 (the 2005 specimen exam-questions-only section and the Paper 03/2 booklet), producing two classes of contamination:

- `contaminated_exam_instructions` — exam-cover boilerplate lines ("Answer ALL the questions", "Silent electronic calculators…", etc.) treated as mark-scheme content
- `unanswerable_question_restatement` — question re-statements with no answer text

32 CSV rows were reclassified to `excluded_reason`, yielding the corrected final state. (The 32 CSV rows fanned out to **128 contaminated `mark_points` rows** in the live DB before the cleanup re-lock.)

**Stage 2 final state** (after contamination cleanup):

| Category | Count |
|---|---|
| `verified=1` (locked into `mark_points`) | 235 |
| `parser_artifact=1` (rubric labels, noise) | 34 |
| `excluded_reason` set (out-of-scope / contaminated) | 44 |
| `needs_manual_entry=1` (bundled, deferred) | 8 |
| **Total** | **321** |

The 8 `needs_manual_entry` rows are correctly-mapped but bundled multi-point answers (WTO roles, Central Bank instruments, PPC diagrams, macro effects, supply shift, development measures, circular flow, injections/withdrawals). Content is correct; splitting into individual gradeable rows is deferred to a future pass.

**Stage 3:** 235 eligible rows locked → **552 `mark_points` rows** (via multi-objective fanout; 235 source rows × average ~2.35 objectives per row), covering **33 distinct objectives**. `mark_scheme_locks` row recorded (`pages=90-128`). All 552 `question_id` values carry the `-stem` suffix (confirmed: `WITHOUT -stem: 0`). This is the current trusted state in the live DB as of 2026-06-30.

The initial lock (before contamination cleanup) wrote 680 mark_points including the 128 contaminated rows; the cleanup re-lock reduced this to 552 genuine rows and zero orphans.

**Stage 4 status:**

| Group | question_ids | Status |
|---|---|---|
| qb1–qb6 (Specimen 1, Q1–Q6) | 21 | ✓ Live in quiz picker (`-stem` chunks exist) |
| qb8–qb15 (2005 specimen, Q1–Q8) | 19 | Locked, awaiting stem ingestion (pages 106–116) |
| qb7(c)v7 order 1 (one salvaged point) | 1 | Locked, awaiting stem ingestion |
| 2016 Specimen Q6 (3 qids) | 3 | Blocked — stems in a separate document not present in this PDF |
| `needs_manual_entry` rows | 8 | Deferred to a future split-and-relock pass |

The 19 ready-for-stem-ingestion question_ids (qb8–qb15) have verified mark points in `mark_points` and will become gradeable as soon as `tools/ingest_econ_specimen_stems.py` is run against the 2005 specimen pages (106–116). The 3 blocked qids require sourcing the 2016 Specimen question paper separately before they can be wired.

---

## Build tracker

To be added to CLAUDE.md as a new section, separate from the Build Stage Tracker and Bootstrap Layer Tracker:

```
## Mark Scheme Layer Tracker

| # | Task | Status |
|---|---|---|
| MS-0 | Stage 0 — question_num backfill (all subjects) | ⬜ Not started |
| MS-1 | Stage 1 — Economics extract + review CSV | ✅ Complete |
| MS-2 | Stage 2 — Economics manual verify | ✅ Complete |
| MS-3 | Stage 3 — Economics lock + mark_points insert | ✅ Complete |
| MS-4 | Stage 4 — quiz picker + grader wire-up (Economics first) | ⚠️ Partial |
| MS-5 | Stage 5 — rollout to remaining 6 subjects | ⬜ Not started |
| MS-6 | Stage 6 — Subject Reports fallback (deferred) | ⬜ Not started |

**MS-0 flag:** checked whether the Stage 0 backfill actually ran for Economics —
`tools/backfill_question_num.py` does not exist anywhere in the repo, and no
`tests/test_backfill_question_num.py` exists either, so the dedicated Stage 0
script was never built or run. Some `chunks.question_num` values are populated
for Economics past-paper chunks regardless (70/191 non-NULL), but that appears
to come from another ingestion path, not a confirmed Stage 0 pass. Left as Not
started rather than marked complete on assumption.

**MS-3 note:** 316 rows reviewed, 224 eligible, 529 mark_points locked.

**MS-4 note:** Rule 2 NULL-page guard live in quiz picker and lock script.
However all 21 live Economics -stem chunks still have page=NULL, so 0 questions
currently surface in the quiz picker. Stem page backfill still required before
Economics grading is actually usable.
```
