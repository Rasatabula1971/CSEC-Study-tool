# ingest_v2 — source-family-aware ingestion framework

One ingestion pipeline that runs unchanged across all seven CSEC subjects. The
corpus follows a uniform per-subject folder skeleton, and three source families
recur in every subject. ingest_v2 captures that uniformity so onboarding a new
subject is **drop three files + run one command** — no new code.

## Why v2 (vs `backend/ingest.py`)

v1 was built around PDFs + keyword-only objective matching. v2 keeps that path
(it is the `GenericPDFAdapter`, byte-equivalent to v1) and adds **adapters** for
the other families:

| Source family | Folder dispatch key | Adapter | Output |
|---|---|---|---|
| Caribbean AI markdown | `Caribbean AI` / `Caribbean_AI_Textbooks`, `.md` | `CaribbeanAIAdapter` | chunks (front-matter → objective, **high** confidence) |
| T&T MoE SLMS office docs | `T&T MoE SLMS`, `.docx/.pptx/.pptm/.pdf` | `MoESLMSAdapter` | chunks (filename `S{n} Obj {range}` → objectives) |
| Kerwin Springer MCQ | `Kerwin Springer`, `.json` | `KerwinMCQAdapter` | `mcq_questions` rows (topic map → objective) |
| Generic PDF (catch-all) | any `.pdf` no other adapter claimed | `GenericPDFAdapter` | chunks (keyword overlap, v1 parity) |

**Folder structure is the dispatch key, not subject identity.** The same four
adapters serve every subject. Adapters are pure (they never touch the DB); the
**orchestrator** owns all writes, embedding, dedup, and review-queue routing.

## Onboarding a new subject — four steps

1. **Drop a manifest** at `manifests/{subject_id}.yaml` (lower-case filename).
   Copy `manifests/economics.yaml` and edit `subject_id`, `display_name`,
   `source_root` (the corpus root on the data drive), and `known_gaps`. Set
   `paper_2_grading_enabled: true` only if real per-paper mark schemes exist.

   > **OPERATOR DECISION — `skip_patterns` for non-POB subjects (UNSETTLED).**
   > POB only had the four numeric KB folders, so its skip set has a clear v1
   > precedent. Other subjects carry folders POB never had — Economics, for
   > example, has **`Subject Reports`, `Worked Solutions`, and `Textbooks`** under
   > its `source_root`. Whether each of those should be ingested (and as what
   > `content_type`) is a real decision with **no v1 precedent** and is NOT decided
   > by this framework. Before running ingest on any non-POB subject, the operator
   > must review the subject's actual folder tree and set `skip_patterns`
   > deliberately. The shipped manifests use only the documented defaults
   > (`_Review Needed`, `_download_*`, build/VCS dirs, `App_Upload_Staging`); they
   > do **not** pre-decide the subject-specific folders. By default, anything not
   > skipped is walked, and a `.pdf` in an unrecognised folder is ingested as
   > `notes` by the GenericPDFAdapter — confirm that is what you want first.
2. **Drop a syllabus CSV** at `syllabus_csvs/{subject_id}.csv` (header columns:
   `section_id, section_num, section_title, objective_id, objective_num,
   content_stmt, skill_type, command_words, exam_weight`). Load + lock it with the
   existing `backend/db/syllabus_parser.py` → `backend/db/lock_subject.py`. The
   subject must be `syllabus_locked = 1` before ingestion will run.
3. **Drop an MCQ topic map** at `mcq_topic_maps/{subject_id}.yaml` mapping the
   Kerwin bank's actual topic/subtopic strings to objective_ids (see the schema
   comment in `mcq_topic_maps/economics.yaml`). If there is no MCQ bank, an empty
   `topic_map: {}` + `unmapped_objective: REVIEW` is fine.
4. **Run one command:**
   ```
   python -m backend.ingest_v2 --subject <Subject> --dry-run   # preview
   python -m backend.ingest_v2 --subject <Subject>             # apply
   ```
   `--adapter <family>` runs one family in isolation for debugging
   (`caribbean_ai | moe_slms | kerwin_mcq | generic_pdf`).

## Objective ids & Rule 1

Objective ids are `{PREFIX}-{section}.{obj}` (e.g. `ECON-3.9`); prefixes live in
`subject_prefix.py`. Every record must resolve to a real, locked `objective_id`
or it is sent to `ingest_review_queue` — never indexed under a guessed id, and an
MCQ that cannot be mapped (the `REVIEW` sentinel) is queued, never inserted.

## Schema

`m018_mcq_questions` adds the `mcq_questions` table (imported MCQ banks — distinct
from the runtime-generated `practice_questions` table, which is untouched) and the
`chunks.source_family` audit column. It is a standalone, file-based migration applied
via the shared `schema_migrations` ledger (idempotent). It is **not** wired into the
app's startup migrations — apply it deliberately.

Every runner invocation requires `--version`. `--status` reports whether a migration
is already applied without changing anything; `--db <path>` targets a specific DB so a
temp copy can be migrated without touching the live `DB_PATH`.

```powershell
# verify only (no change) -- note --status still requires --version:
python -m backend.db.migrations.runner --version m018_mcq_questions --status --db <path>
# apply:
python -m backend.db.migrations.runner --version m018_mcq_questions --db <path>
```

**Apply order — do not skip the parity gate.** Back up first (`launch\backup.bat`),
apply m018 to a TEMP COPY, run the parity gate against that copy, and only then apply
to the live DB:

```powershell
# 1. apply m018 to a temp copy only
python -m backend.db.migrations.runner --version m018_mcq_questions --db C:\tmp\csec_temp.sqlite

# 2. parity gate against that copy (PowerShell env var; the test never mutates it).
#    First remove the @pytest.mark.skip line in tests/test_ingest_v2/test_pob_parity.py.
$env:PARITY_DB_PATH = "C:\tmp\csec_temp.sqlite"
python -m pytest tests/test_ingest_v2/test_pob_parity.py -v -s

# 3. only if the gate passes, apply to the live DB (uses DB_PATH from .env):
python -m backend.db.migrations.runner --version m018_mcq_questions
```

## Layout

```
backend/ingest_v2/
├── manifest.py            # SubjectManifest (Pydantic) + load_manifest
├── subject_prefix.py      # subject_id → objective-id prefix
├── objective_index.py     # locked-syllabus lookups (section/obj, keyword, membership)
├── normalize.py           # shared text normalisation (mojibake, custom tags)
├── orchestrator.py        # IngestOrchestrator + IngestSummary (owns the DB)
├── registry.py            # wires adapters into dispatch order
├── __main__.py            # CLI
├── adapters/
│   ├── base.py            # IngestRecord + BaseAdapter
│   ├── caribbean_ai.py
│   ├── moe_slms.py
│   ├── kerwin_mcq.py
│   └── generic_pdf.py
├── manifests/             # one YAML per subject
├── syllabus_csvs/         # one CSV per subject (header → syllabus_parser)
└── mcq_topic_maps/        # one YAML per subject
```

## Known issues

- 31 POB mark-scheme PDFs were relocated from a since-deleted `D:\` staging path to `E:\...\03_MARK_SCHEMES\` at some point; their original v1 chunk bindings are orphaned (source file no longer exists) but harmless. v2 ingests the current location correctly.
- GenericOfficeAdapter (docx/pptx outside T&T MoE SLMS naming convention) deferred — not needed for POB (covered via upload-feature ingestion, confirmed by hash match); revisit if a future subject has bulk .docx/.pptx notes with no upload history.
