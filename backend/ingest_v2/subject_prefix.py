# PHASE: build
"""
backend/ingest_v2/subject_prefix.py
===================================
Single source of truth for the short objective-id prefix of each CSEC subject.

Objective ids are built as ``{PREFIX}-{section}.{obj}`` (e.g. ``POB-1.2``,
``ECON-3.9``). Adapters that learn a (section, objective) pair from a file's
front-matter or filename construct the canonical objective_id with this prefix,
then validate it against the locked syllabus via ObjectiveIndex.all_objective_ids().

The two prefixes the framework relies on today -- POB and ECON -- are confirmed:
the live POB syllabus uses ``POB-*`` ids, and the Economics syllabus CSV (curated
later) will use ``ECON-*``. The remaining five are provisional defaults and MUST
match each subject's syllabus CSV when that subject is onboarded; they are only
consulted once that subject is locked.
"""

# subject_id (the on-disk folder / DB subject_id) -> objective-id prefix.
SUBJECT_PREFIX = {
    "Principles_of_Business": "POB",
    "Economics": "ECON",
    "Mathematics": "MATH",
    "English": "ENG",
    "Principles_of_Accounts": "POA",
    "Integrated_Science": "INTSCI",
    "Information_Technology": "IT",
}


def prefix_for(subject_id: str) -> str:
    """Return the objective-id prefix for a subject_id, or raise on an unknown one.

    Failing loudly here is deliberate: an adapter that silently guessed a prefix
    would mint objective_ids that never match the syllabus, defeating Rule 1."""
    try:
        return SUBJECT_PREFIX[subject_id]
    except KeyError:
        known = ", ".join(sorted(SUBJECT_PREFIX))
        raise ValueError(
            f"unknown subject_id '{subject_id}'. Known subjects: {known}"
        )
