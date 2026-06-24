# Lesson Generation Playbook

Procedure for generating lessons for subjects 2 through 7 via
`backend/ingest_lessons.py`. Encodes what was learned getting Economics'
77 objectives through the pipeline. Same three-role model as
`PLAYBOOK.md` (Claude Code executes and reports; the orchestrating
Claude does technical/quality judgment; the project owner gives
go/no-go on a plain-language summary, not on raw output).

## Before you start — this is NOT free to test

Unlike ingestion's `--dry-run`, lesson generation's `--dry-run` flag
only suppresses DB writes — it still calls Sonnet and bills for every
non-skipped objective. There is no free preview mode. Budget for real
spend starting with the very first validation call. (Cost is low —
roughly $0.02–0.03/objective — but it is not zero, and a careless
re-run across many objectives adds up.)

## Phase 0 — Pre-flight (read-only, no Sonnet calls)

- [ ] Confirm `ANTHROPIC_API_KEY` is set in `.env` (report yes/no only,
  never the value) and report the configured `ANTHROPIC_MODEL` (or
  confirm it falls back to the code default).
- [ ] Confirm Ollama is running (`curl http://localhost:11434/api/tags`)
  — retrieval still embeds queries via Ollama even though composition
  uses Sonnet. Both must be up.
- [ ] Confirm the subject is syllabus-locked (`syllabus_locked = 1`).
  `ingest_lessons.py` should refuse otherwise, but verify before
  spending anything.
- [ ] Spot-check chunk coverage for 3–5 objectives spanning different
  sections, including any objective flagged as a thin spot in the
  subject's manifest `known_gaps`. **Do not use chunk count as a
  predictor of success** — Economics proved chunk count and
  generation outcome are uncorrelated (a 40-chunk objective failed,
  a 16-chunk objective succeeded). This step is just to confirm
  retrieval is returning *something* for a range of objectives, not
  to forecast the write rate.
- [ ] **`insufficient_source` should mostly NOT recur for this subject
  if `extra_source_roots` was populated correctly at ingestion.** The
  bulk of the Economics `insufficient_source` effort traced to its
  Bridge/Supplemental `.docx` notes never being ingested (they live in
  `App_Upload_Staging`, outside the default walk). The ingest-side fix
  — `enable_office_adapter: true` + `extra_source_roots` in the manifest
  (see ingest PLAYBOOK Phase 0) — pulls that content into `chunks` so
  retrieval can reach it. Before assuming a subject is genuinely
  source-thin, confirm those folders were wired during ingestion; if
  they weren't, fix the manifest and re-ingest rather than burning Sonnet
  calls against a corpus that's missing its best material.
- [ ] **If gaps still appear after that, check retrieval RANKING before
  concluding the source is missing.** Economics' second-biggest cause was
  NOT absent content but `NOTES_K` being too small: the teaching chunk
  existed but ranked below syllabus-restatement / intro / assessment
  chunks and fell outside the top-`k` cutoff. `NOTES_K` is now 15 (was 5;
  fixed in `retrieval.py`). The diagnostic when an objective refuses
  despite plausibly-present notes: pull the top-`k` chunks for that
  objective's content_stmt and read what actually ranked — if good
  teaching chunks are present but below the cutoff, it's a ranking/`k`
  problem, not a missing-source problem. Do this BEFORE chasing the
  source as absent.

## Phase 1 — Small validation batch (billed, ~5 objectives)

Pick 5 objectives deliberately, not randomly: 2 from well-covered
sections, 1 from a different skill_type/command-word profile than the
others (tests prompt range), and 1–2 from any manifest-flagged thin
spot. This mix is what actually surfaces problems — a same-section
sample of 5 strong objectives proves nothing.

```powershell
python backend/ingest_lessons.py --subject {Subject} --objectives {id1,id2,id3,id4,id5}
```

**Requires orchestrating Claude review before proceeding:**
- Print the full `lesson_text` of every objective that wrote
  successfully (not just one) and check against the formatting rule
  below.
- Read every `insufficient_source` reason string and assess whether it
  is a *credible* refusal (the source genuinely lacks teachable
  content) or looks like a retrieval problem (the right chunks exist
  but weren't pulled). Sonnet's own stated reason is the most reliable
  signal here — read it, don't just count the outcome.
- Check quality against the POB bar: 350–650 words, **bold** headers,
  prose or plain lists, Caribbean examples drawn from source, single
  trailing `Q:` recall line, no meta-language.

## Formatting rule — already fixed, just verify it's holding

`prompts/lesson_structurer.txt` Rule 9 (PLAIN-TEXT FORMATTING) already
forbids `#` headers, `---` rules, and `|` tables, because the study UI
renders `lesson_text` as plain pre-wrapped text with no Markdown
engine. **This fix is permanent and in the prompt file — it does not
need to be rediscovered or re-applied per subject.** The only thing to
do each time is a mechanical compliance check on freshly generated
text (grep for `#`, `---`, `|` across all written lessons in a batch),
since this is cheap and catches any future prompt drift immediately.
If a violation ever appears again, that means something changed in the
prompt or the model's behavior — investigate before scaling, don't
patch around it per-subject.

## Phase 2 — Decide: proceed, or pause and investigate

After Phase 1, the orchestrating Claude reports a plain-language
recommendation to the project owner:

| Signal | Recommendation |
|---|---|
| Formatting clean, `insufficient_source` reasons all read as credible content gaps, lessons match the POB quality bar | Proceed to the medium batch (Phase 3) |
| Formatting violations found | STOP. This is a prompt regression — fix before spending more. |
| `insufficient_source` reasons look like retrieval misses, not real content gaps (e.g. citing missing content that you can independently confirm exists in the corpus) | STOP. Worth checking retrieval (top-k, embedding match) before assuming the corpus itself is thin. |
| A `quality_check_failed` (e.g. malformed recall question) on an otherwise well-composed lesson | Just retry that one objective with `--regenerate` once. This has been non-deterministic noise every time it's occurred — not a content signal. If it fails the SAME way twice, that's worth a closer look. |

## Phase 3 — Medium batch (~15–20% of the subject)

Run a real batch covering a meaningfully different chunk of the
syllabus than Phase 1 (different sections, not a continuation of the
same ones). This is what actually tests whether quality/write-rate
holds across the subject's range, not just the hand-picked validation
set.

```powershell
python backend/ingest_lessons.py --subject {Subject} --objectives {next ~15 ids}
```

Report, in full (not a sample):
- Per-objective table: status, word count, tokens in/out, cost
- Running cost total for the subject so far
- 2 freshly spot-checked lessons from sections not yet seen
- Formatting-violation count across ALL written lessons in the batch
  (should be zero; if not, stop)
- Section-by-section write rate so far, to see if it's trending evenly
  or concentrated in particular sections

## Phase 4 — The backlog-size judgment call (requires orchestrating
Claude + project owner, not automatable)

POB shipped with exactly 1 objective on an older (non-Sonnet) lesson,
due to a genuine, narrow content gap (careers in business — not
covered by the source notes at all). That is the precedent for
"acceptable."

Before running the remainder of any new subject, the orchestrating
Claude should look at the *cumulative* queued list so far (not just
batch-by-batch) and make a judgment: is this subject's
`insufficient_source` rate in the same ballpark as POB (a small,
individually-explainable handful), or is it shaping up to be a
double-digit percentage of the whole subject?

- **Small, individually-credible backlog** → proceed, document as
  accepted backlog (same pattern as the ingestion review-queue).
- **Large backlog, or many similarly-themed gaps clustering in one
  section** → this is worth surfacing to the project owner as a real
  decision point: accept a notably incomplete lesson set for this
  subject, or pause lesson generation and improve that section's
  source corpus first (more Caribbean AI lesson coverage, Supplemental
  Web Sources, etc.) before continuing. This is a content/curriculum
  decision, not a technical one — Claude Code should never decide this
  unilaterally, and the orchestrating Claude should frame it as a
  clear choice for the project owner rather than picking silently.

## Phase 5 — Full run on the remainder

Once Phase 4's judgment is made and the project owner has said
"proceed":

```powershell
python backend/ingest_lessons.py --subject {Subject}
```

Idempotent — already-written objectives are skipped automatically, so
this is safe to run even if Phase 1/3 already covered some objectives.

Report the final tally: total written vs. queued across the whole
subject, cost total for the full lesson-generation phase, and the
complete list of every queued objective_id with its reason (not a
sample — the full list is what Phase 4's judgment call depends on for
future subjects too).

## Known issues / context carried over from Economics

- `ingest_lessons.py`'s `--dry-run` does not prevent Sonnet billing —
  see "Before you start" above. Don't treat it as a free check.
- Token usage (`resp.usage.input_tokens` / `output_tokens`) is now
  captured and logged per call (fixed during Economics generation) —
  confirm this is still in place; if a future refactor of
  `anthropic_client.py` accidentally drops it, cost reporting silently
  goes blind again.
- No prompt caching is currently implemented on the system prompt,
  despite it being identical across every call and every subject.
  Estimated lifetime savings across all 7 subjects: roughly $10–15 —
  real but small. This is a deliberately deferred optimization, not
  forgotten; worth doing as its own isolated change at some point, not
  bundled into a content-generation run.
- Chunk count is not a reliable predictor of generation success.
  Don't use it to pre-screen objectives or to second-guess a
  `insufficient_source` result — Sonnet's own content judgment is the
  real (and only useful) signal.
- A `quality_check_failed` on the recall-question format USUALLY
  resolves cleanly on a single retry (treat a one-off as noise) — BUT
  there is one systematic exception that will NOT clear no matter how
  many times you retry: see the validator gap below. If the same
  objective fails the recall check 2–3 times running, stop spinning
  (each retry is a billed Sonnet call) and check whether it is the
  scenario-first calculation case.

- **Recall-question validator gap — scenario-first calculation prompts
  (latent rollout blocker).** `_validate_lesson_quality` in
  `ingest_lessons.py` accepts a recall question only when it *ends in
  `?`* OR *starts with a CSEC command word* (`_RECALL_COMMAND_WORDS`).
  That rule wrongly rejects a perfectly valid calculation prompt where
  the command word appears MID-sentence after a numeric scenario. Real
  example caught on Economics **ECON-3.14** ("Calculate price elasticity
  of supply"), which Sonnet composed correctly (`status: ok`) with the
  recall question:
    > "A firm's quantity supplied increases from 200 units to 260 units
    > when the price rises from $5.00 to $10.00. Using the simple PES
    > formula, calculate the Price Elasticity of Supply and state whether
    > supply is elastic or inelastic."
  This ends in a period (not `?`) and starts with "A firm's…" (the
  scenario), so the gate rejects it — even though `calculate` and
  `state` are present and correct. Every retry fails identically because
  Sonnet keeps (rightly) writing scenario-first numeric prompts; this is
  NOT noise.
  - **Why it matters beyond Economics:** ANY subject with
    numeric/Calculate objectives hits the same wall. **Mathematics** and
    **Principles_of_Accounts** will have MANY such objectives — for them
    this is a blocker, not an edge case. (Economics' other Calculate
    objectives only wrote because their recall questions happened to open
    with the literal word "Calculate"; the moment Sonnet sets up a
    scenario first, the gate rejects.)
  - **Proposed fix (precise, so it needs no re-diagnosis):** broaden the
    accept-rule so a recall question also passes if it CONTAINS a CSEC
    command word as a whole word ANYWHERE (not only at the start), in
    addition to the existing ends-in-`?` / starts-with-command-word
    cases. ~2-line change in `_validate_lesson_quality` +
    `_RECALL_COMMAND_WORDS` usage, plus one test (a scenario-first
    calculation prompt must pass). The validator's OTHER gates — length
    floor, chat-boilerplate filter, answer-leak check — are independent
    of this and MUST stay exactly as-is (do not loosen them).
  - **Current status:** ECON-3.14 is queued on THIS gap, not a content
    gap. It will write cleanly the moment the fix lands — no new content
    needed. Decision pending; not yet implemented (deliberately deferred,
    documented here so it is actionable later without re-investigating).
