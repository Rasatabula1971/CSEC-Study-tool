# Visual Page Builder — Feasibility Notes & Open Architecture Question

**Date:** 2026-06-23
**Status:** Proof-of-concept validated. Not wired into any subject pipeline.

---

## 1. What this is

A candidate feature for the CSEC AI Study Partner: convert a lesson into a standalone,
interactive HTML visual (sliders, clickable diagrams, tabbed sections) instead of plain
lesson text. Modeled on the "Visual Page Builder" Cowork skill
(`visual-page-builder` — *"Generate beautiful self-contained HTML pages that explain any
concept visually"*).

## 2. Where it sits in the architecture

Build-time, not runtime — same bucket as lesson composition and Gemini-steered
classification. The 3B Ollama model is never involved; it is not capable of this output
and runtime is the wrong place for it.

## 3. Model choice: Gemini free tier vs. Claude Sonnet

| Factor | Gemini Flash (free tier) | Claude Sonnet (paid) |
|---|---|---|
| Cost | $0 at this volume | Cents per page — trivial volume, but not free |
| Rate limits | ~10–30 RPM, 250–1,500 RPD depending on model — far above Rylee's actual usage | N/A |
| Data privacy | Free-tier inputs/outputs may be used by Google to improve their models; no opt-out at this tier | Standard API terms |
| Design quality | Strong on structure/interactivity; untested whether it matches Sonnet's design polish at scale | Stronger default on intentional visual design |
| Existing infra | `google-genai` SDK already in use for grading path — zero new integration cost | Would be a new call path |

**Decision so far:** try Gemini Flash first since the SDK is already in place and the
free tier easily covers expected volume. Fall back to Sonnet per-page only if a test
generation disappoints on quality. Same privacy-exception class already accepted for
the Gemini grading call — not a new precedent.

## 4. Prototype result

Test prompt: *"Chemistry: Acids, bases, metals, and non-metals explanation, Integrated
Science CSEC level."*

Output: a single self-contained HTML file (no CDN dependencies, inline CSS/JS) covering:
- Acids vs. bases comparison cards
- Interactive pH scale slider with CSEC-relevant examples (lime juice, bleach, milk of
  magnesia, etc.)
- Clickable metal/non-metal element explorer
- Three core reaction types (neutralization, acid+metal, acid+carbonate) with equations

**Verdict:** quality and interactivity exceed what a static lesson page would offer.
Validates Gemini free tier as sufficient for this feature — no Sonnet fallback needed
based on this sample.

## 5. Open architecture question: one-objective-to-one-file vs. topic clusters

The prototype above covers **three separate objectives in one file** (acids/bases,
metals/non-metals, reactions), not the strict 1:1 `objective_id` → file mapping
originally assumed.

| Option | Pros | Cons |
|---|---|---|
| **Strict 1:1** (one file per `objective_id`) | Matches existing naming convention; no new DB table; trivial serving logic | Breaks natural interactivity — e.g. a single pH slider doesn't make sense split across multiple files |
| **Topic clusters** (one file serves several related objectives) | Matches how CSEC chemistry is actually taught/tested together; matches the prototype's natural shape | Requires a lightweight lookup table (`objective_id` → `visual_file_id`); serving logic (`GET /api/visual/{objective_id}`) needs an extra join instead of a direct filename match |

**Leaning:** topic clusters, since CSEC content groups (e.g. all of an ISCI module's
chemistry-core objectives) are pedagogically taught and examined together. This is a
real change to the earlier "no new DB table needed" assumption — clustering requires
at minimum a small mapping table or a JSON manifest per subject.

**Not yet decided.** No commitment until the next subject syllabus is locked and this
gets prioritized.

## 6. Gate status

Per the syllabus-lock rule, this feature is **not wired into any subject pipeline**
until that subject's syllabus is locked and ingested. Currently:

- POB, Economics: locked, ingested — eligible once the cluster-vs-1:1 decision is made
- Integrated Science: syllabus locked, ingestion dry-run in progress — eligible once ingestion completes
- Remaining subjects: not yet onboarded — not eligible

## 7. Open items / next steps

- [ ] Decide: 1:1 file mapping vs. topic clusters (Section 5)
- [ ] If clusters: design the mapping table/manifest format
- [ ] Build `backend/generate_visual.py` (Gemini Flash call, on-demand + cache to SSD)
- [ ] Add `GET /api/visual/{objective_id}` route in `app.py`
- [ ] Add a "Visualize" button to `chat.html` next to lesson responses
- [ ] Re-test prototype quality on a second subject's content before treating Gemini
      Flash as the settled default (one sample is not a pattern)
