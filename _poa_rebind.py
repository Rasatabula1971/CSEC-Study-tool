"""
Post-ingest rebind script for Principles_of_Accounts.
Follows the INTSCI-3.3.7 / IT-5.9 misbind-fix pattern documented in CLAUDE.md.

Run AFTER python -m backend.ingest_v2 --subject Principles_of_Accounts completes.

Step 1: coverage check -- shows which of the 4 targets got any chunks post-ingest.
Step 2: for each still-zero objective, find and display candidate donor chunks.
Step 3: execute rebinds with explicit confirmation per objective.
Step 4: final coverage check.
"""

import os
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv
load_dotenv()

import backend.db.init_db as v1

DB_PATH = os.getenv("DB_PATH")
db = v1.open_db(DB_PATH)

SUBJECT = "Principles_of_Accounts"

TARGET_OBJECTIVES = {
    "POA-3.5":  "translate source documents into transaction descriptions",
    "POA-4.7":  "interpret entries and balances",
    "POA-4.9":  "outline the uses and limitations of the trial balance",
    "POA-6.7":  "discuss the nature of depreciation",
    "POA-7.2":  "outline the three most commonly used control systems",
    "POA-7.6":  "construct a suspense account",
    "POA-7.11": "explain the significance of the balances on control accounts",
    "POA-8.3":  "give reasons for establishing partnerships",
    "POA-8.10": "prepare balance sheet of partnerships",
    "POA-9.10": "analyse performance and position using ratios",
}

# Donor objectives to pull rebind candidates from (per-objective)
DONOR_MAP = {
    "POA-3.5":  ["POA-3.3", "POA-3.4", "POA-3.6"],
    "POA-4.7":  ["POA-4.3", "POA-4.5", "POA-4.6", "POA-4.8"],
    "POA-4.9":  ["POA-4.8"],
    "POA-6.7":  ["POA-6.8", "POA-6.9"],
    "POA-7.2":  ["POA-7.1", "POA-7.3", "POA-7.8", "POA-7.12"],
    "POA-7.6":  ["POA-7.3", "POA-7.4", "POA-7.5", "POA-7.7"],
    "POA-7.11": ["POA-7.8", "POA-7.10"],
    "POA-8.3":  ["POA-8.1", "POA-8.2", "POA-8.4"],
    "POA-8.10": ["POA-8.7", "POA-8.8", "POA-8.9"],
    "POA-9.10": ["POA-5.6", "POA-5.7", "POA-5.9", "POA-9.9"],
}

# Keywords that strongly indicate the target objective's specific content
TARGET_KEYWORDS = {
    "POA-3.5":  ["translat", "description", "narrative", "narration", "describes the transaction"],
    "POA-4.7":  ["interpret", "reading the account", "what the balance means", "what the entry means"],
    "POA-4.9":  ["limitation", "uses of the trial balance", "trial balance cannot",
                 "trial balance does not", "weaknesses of the trial balance"],
    "POA-6.7":  ["nature of depreciation", "what is depreciation", "meaning of depreciation",
                 "depreciation is", "depreciation refers", "depreciation occurs",
                 "non-current assets lose", "assets lose value", "wearing out",
                 "obsolescence", "reason for depreciation", "cause of depreciation"],
    "POA-7.2":  ["three control", "control systems", "types of control", "error correction",
                 "bank reconciliation", "control accounts"],
    "POA-7.6":  ["suspense account", "suspense"],
    "POA-7.11": ["significance", "balance on the control", "what the balance", "control account balance"],
    "POA-8.3":  ["reason", "why form", "advantage of partnership", "benefit of partnership",
                 "why establish", "motive"],
    "POA-8.10": ["partnership balance sheet", "balance sheet of the partnership",
                 "partners' balance sheet", "partnership's balance sheet",
                 "balance sheet for the partnership"],
    "POA-9.10": ["ratio", "performance", "position", "analyse", "analysis"],
}


def coverage_check():
    print("\n" + "=" * 60)
    print("POST-INGEST COVERAGE CHECK")
    print("=" * 60)

    total = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE subject_id = ?", (SUBJECT,)
    ).fetchone()[0]
    print(f"Total POA chunks: {total}\n")

    print(f"{'Objective':<12} {'chunks':>7}  {'content_stmt'}")
    print("-" * 80)
    rows = db.execute(
        """
        SELECT o.objective_id, COUNT(c.id) as cnt, o.content_stmt
        FROM objectives o
        LEFT JOIN chunks c ON c.objective_id = o.objective_id
        WHERE o.subject_id = ?
        GROUP BY o.objective_id
        ORDER BY o.objective_id
        """,
        (SUBJECT,),
    ).fetchall()
    zero_count = 0
    for r in rows:
        marker = " <-- ZERO" if r["cnt"] == 0 else ""
        print(f"{r['objective_id']:<12} {r['cnt']:>7}  {r['content_stmt'][:55]}{marker}")
        if r["cnt"] == 0:
            zero_count += 1
    print(f"\nObjectives with zero chunks: {zero_count}")
    return {r["objective_id"]: r["cnt"] for r in rows}


def find_donor_candidates(target_id: str, donors: list, keywords: list):
    print(f"\n{'=' * 60}")
    print(f"DONOR SEARCH: {target_id} -- {TARGET_OBJECTIVES[target_id]}")
    print(f"Donor pool: {donors}")
    print(f"Keywords: {keywords}")
    print("-" * 60)

    placeholders = ",".join("?" * len(donors))
    all_chunks = db.execute(
        f"""
        SELECT c.id, c.objective_id, c.chunk_id, c.source_family,
               c.chunk_text, d.content_type, d.source_file
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.subject_id = ?
          AND c.objective_id IN ({placeholders})
        ORDER BY c.objective_id, c.id
        """,
        [SUBJECT] + donors,
    ).fetchall()

    print(f"Total chunks in donor pool: {len(all_chunks)}")

    # Filter by keywords
    candidates = []
    for chunk in all_chunks:
        text_lower = chunk["chunk_text"].lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                candidates.append(chunk)
                break

    print(f"Keyword-matching candidates: {len(candidates)}")
    for c in candidates[:10]:  # show up to 10
        preview = c["chunk_text"][:200].replace("\n", " ")
        print(f"\n  ID={c['id']} donor={c['objective_id']} type={c['content_type']}")
        print(f"  {preview}...")
    if len(candidates) > 10:
        print(f"  ... and {len(candidates) - 10} more")
    return candidates


def execute_rebind(target_id: str, candidate_ids: list, dry_run: bool = True):
    if not candidate_ids:
        print(f"  No candidates to rebind for {target_id}")
        return 0

    id_list = ",".join(str(i) for i in candidate_ids)
    count_before = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE objective_id = ?", (target_id,)
    ).fetchone()[0]
    donor_before = db.execute(
        f"SELECT COUNT(*) FROM chunks WHERE id IN ({id_list})"
    ).fetchone()[0]

    if dry_run:
        print(f"  DRY RUN: would rebind {donor_before} chunks -> {target_id} "
              f"(was {count_before})")
        return donor_before

    db.execute(
        f"UPDATE chunks SET objective_id = ? WHERE id IN ({id_list})",
        (target_id,),
    )
    db.commit()
    count_after = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE objective_id = ?", (target_id,)
    ).fetchone()[0]
    print(f"  Rebound {donor_before} chunks -> {target_id}: "
          f"{count_before} -> {count_after}")
    return donor_before


def check_review_queue(target_id: str):
    """Check ingest_review_queue for any promotable chunks for this objective."""
    rows = db.execute(
        """
        SELECT id, chunk_text, reason, created_at
        FROM ingest_review_queue
        WHERE objective_id = ?
        ORDER BY id
        """,
        (target_id,),
    ).fetchall()
    if rows:
        print(f"\n  Review queue entries for {target_id}: {len(rows)}")
        for r in rows[:5]:
            preview = str(r["chunk_text"])[:150].replace("\n", " ")
            print(f"    ID={r['id']} reason={r['reason']}: {preview}...")
    else:
        print(f"  Review queue: no entries for {target_id}")

    # Also check for any text containing target keywords regardless of objective_id
    target_kws = TARGET_KEYWORDS.get(target_id, [])
    if target_kws:
        kw = target_kws[0]  # first/strongest keyword
        unbound = db.execute(
            """
            SELECT id, chunk_text, reason, objective_id, created_at
            FROM ingest_review_queue
            WHERE LOWER(chunk_text) LIKE ?
            LIMIT 5
            """,
            (f"%{kw.lower()}%",),
        ).fetchall()
        if unbound:
            print(f"  Review queue entries containing '{kw}': {len(unbound)}")
            for r in unbound[:3]:
                preview = str(r["chunk_text"])[:150].replace("\n", " ")
                print(f"    ID={r['id']} obj={r['objective_id']}: {preview}...")

    return rows


def main():
    counts = coverage_check()

    print("\n\n" + "=" * 60)
    print("PHASE 2: REBIND ANALYSIS")
    print("=" * 60)

    rebind_plan = {}  # target_id -> list of chunk ids to rebind

    for target_id, content_stmt in TARGET_OBJECTIVES.items():
        current = counts.get(target_id, 0)
        print(f"\n--- {target_id}: {content_stmt} [current chunks: {current}] ---")

        # Check review queue
        check_review_queue(target_id)

        # Find donor candidates
        candidates = find_donor_candidates(
            target_id, DONOR_MAP[target_id], TARGET_KEYWORDS[target_id]
        )
        rebind_plan[target_id] = [c["id"] for c in candidates]

    # Summary of plan
    print("\n\n" + "=" * 60)
    print("REBIND PLAN SUMMARY")
    print("=" * 60)
    for target_id, ids in rebind_plan.items():
        print(f"  {target_id}: {len(ids)} chunks to rebind from donors")

    # Execute rebinds
    print("\n\n" + "=" * 60)
    print("EXECUTING REBINDS")
    print("=" * 60)
    for target_id, ids in rebind_plan.items():
        print(f"\n{target_id}:")
        execute_rebind(target_id, ids, dry_run=False)

    # Final coverage check
    print("\n\n" + "=" * 60)
    print("FINAL COVERAGE CHECK (POST-REBIND)")
    print("=" * 60)
    final_counts = coverage_check()

    # Assess POA-8.10 volume
    poa_810_count = final_counts.get("POA-8.10", 0)
    print(f"\nPOA-8.10 volume assessment: {poa_810_count} chunks")
    if poa_810_count >= 3:
        print("  -> SUFFICIENT for lesson generation (3+ chunks)")
    elif poa_810_count >= 1:
        print("  -> MARGINAL (1-2 chunks): lesson may be thin but possible")
    else:
        print("  -> INSUFFICIENT: zero chunks even after rebind")
        print("     Next step: check past papers for partnership balance sheet questions")

    # Donor coverage check -- confirm no donor lost all its chunks
    print("\nDonor coverage check (confirm no donor went to zero):")
    all_donors = set()
    for donors in DONOR_MAP.values():
        all_donors.update(donors)
    for donor in sorted(all_donors):
        cnt = final_counts.get(donor, 0)
        warning = " <-- WARNING: donor now zero" if cnt == 0 else ""
        print(f"  {donor}: {cnt} chunks{warning}")


if __name__ == "__main__":
    main()
