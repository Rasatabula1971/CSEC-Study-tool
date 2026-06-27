"""
Enrich thin POA objectives by keyword-searching the full corpus.

Strategy:
- For each objective with <= 5 chunks, define high-signal keyword sets.
- Search ALL POA chunks that currently belong to an objective with >= 6 chunks
  (so donors are never thinned below a safe floor).
- Show candidates, then execute.
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

# ── per-objective keyword sets ────────────────────────────────────────
# Only include terms that unambiguously indicate this objective's content.
# Use broad sets here since we're searching the full corpus.

KEYWORD_MAP = {
    # 1-chunk objectives
    "POA-2.9": [
        "construct a balance sheet",
        "construct balance sheet",
        "draw up the balance sheet",
        "draw up a balance sheet",
        "prepare the balance sheet",
    ],
    "POA-4.2": [
        "general ledger", "subsidiary ledger", "purchases ledger",
        "sales ledger", "creditors ledger", "debtors ledger",
        "personal ledger", "nominal ledger", "real ledger",
        "types of ledger", "types of account book",
    ],
    "POA-4.9": [
        "limitation of the trial balance",
        "limitations of the trial balance",
        "uses of the trial balance",
        "trial balance cannot detect",
        "trial balance does not detect",
        "errors not detected by",
        "weakness of the trial balance",
    ],
    "POA-5.2": [
        "components of the financial statements",
        "components of financial statements",
        "elements of the financial statements",
        "income statement and balance sheet",
        "income statement, balance sheet",
        "financial statements consist",
        "financial statements include",
        "financial statements comprise",
    ],
    "POA-6.1": [
        "accrual concept",
        "matching concept",
        "matching principle",
        "prudence concept",
        "concepts that underpin",
        "underlying concept",
        "accounting concepts and adjustments",
        "concept of accruals",
    ],
    "POA-6.4": [
        "reasons for bad debts",
        "reasons for bad debt",
        "causes of bad debts",
        "cause of bad debt",
        "why bad debts arise",
        "why bad debts occur",
        "debtor is unable to pay",
        "debtor fails to pay",
        "unable to recover the debt",
        "debtor become insolvent",
        "debtor gone bankrupt",
    ],
    "POA-7.1": [
        "uses of control systems",
        "purposes of control systems",
        "role of control systems",
        "why we use control systems",
        "importance of control systems",
        "control systems help",
        "control systems are used",
    ],
    "POA-7.3": [
        "errors of omission",
        "errors of commission",
        "errors of principle",
        "compensating error",
        "complete reversal",
        "errors of original entry",
        "do not affect the trial balance",
        "errors that do not affect",
        "errors that affect the trial balance",
        "which errors affect",
    ],
    "POA-7.7": [
        "revised profit",
        "corrected profit",
        "statement of adjusted profit",
        "statement showing the revised",
        "effect on profit",
        "adjust the profit",
        "profit after correction",
    ],
    "POA-9.9": [
        "final accounts of a company",
        "final accounts of limited",
        "income statement of the company",
        "profit and loss account of the company",
        "trading account of the company",
        "financial statements of a limited",
        "financial statements of the company",
    ],
    "POA-11.4": [
        "accounting software",
        "payroll software",
        "computerised payroll",
        "computerized payroll",
        "sage payroll",
        "quickbooks",
        "peachtree",
        "pastel",
        "software for payroll",
        "payroll system",
    ],
    "POA-11.6": [
        "calculate earnings",
        "calculate wages",
        "calculate gross pay",
        "gross pay calculation",
        "hourly rate",
        "time rate",
        "overtime calculation",
        "basic pay",
        "earning calculation",
        "employees earnings",
        "employee earnings",
    ],

    # 2-chunk objectives
    "POA-7.5": [
        "need for suspense account",
        "why a suspense account",
        "why open a suspense account",
        "purpose of a suspense account",
        "reason for suspense account",
        "when a suspense account is opened",
        "opening a suspense account",
    ],
    "POA-7.8": [
        "purpose of control accounts",
        "purposes of control accounts",
        "why control accounts are prepared",
        "reason for control accounts",
        "advantages of control accounts",
        "why use control accounts",
        "function of control accounts",
    ],
    "POA-10.4": [
        "final accounts for a manufacturing",
        "manufacturing final accounts",
        "income statement for a manufacturer",
        "profit and loss for a manufacturing",
    ],
    "POA-10.5": [
        "costing principles",
        "basic costing",
        "cost concept",
        "cost classification",
        "direct cost", "indirect cost",
        "prime cost",
        "production cost",
        "cost of production",
    ],
    "POA-11.1": [
        "methods of payment",
        "method of payment",
        "cheque", "cash payment", "bank transfer",
        "credit card", "debit card", "direct debit",
        "standing order", "electronic transfer",
        "online banking",
    ],

    # 3-chunk objectives
    "POA-8.2": [
        "features of a partnership",
        "characteristics of a partnership",
        "unlimited liability",
        "mutual agency",
        "partnership features",
        "shared management",
        "deed of partnership",
    ],
    "POA-9.2": [
        "types of limited liability",
        "private limited company",
        "public limited company",
        "co-operative",
        "types of company",
        "types of limited company",
    ],
    "POA-9.7": [
        "dividend calculation",
        "calculate dividend",
        "preference dividend",
        "ordinary dividend",
        "interim dividend",
        "final dividend",
        "dividend per share",
        "dividend payment",
    ],
    "POA-9.8": [
        "appropriate profits",
        "appropriation of profits",
        "general reserve",
        "retained earnings",
        "transfer to reserve",
        "dividend and reserve",
        "profit appropriation",
    ],
}


def rich_donors(min_chunks: int = 6):
    """Chunk ids belonging to objectives that have >= min_chunks chunks."""
    rich = db.execute(
        f"""
        SELECT objective_id
        FROM (
            SELECT objective_id, COUNT(*) as cnt
            FROM chunks
            WHERE subject_id = ?
            GROUP BY objective_id
        ) WHERE cnt >= ?
        """,
        (SUBJECT, min_chunks),
    ).fetchall()
    return [r["objective_id"] for r in rich]


def current_count(oid):
    return db.execute(
        "SELECT COUNT(*) FROM chunks WHERE objective_id=? AND subject_id=?",
        (oid, SUBJECT),
    ).fetchone()[0]


def find_candidates(target_id, keywords, eligible_donors):
    placeholders = ",".join("?" * len(eligible_donors))
    all_chunks = db.execute(
        f"""
        SELECT c.id, c.objective_id, c.chunk_text, d.content_type
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.subject_id = ?
          AND c.objective_id IN ({placeholders})
        """,
        [SUBJECT] + eligible_donors,
    ).fetchall()

    candidates = []
    for chunk in all_chunks:
        text_lower = chunk["chunk_text"].lower()
        for kw in keywords:
            if kw.lower() in text_lower:
                candidates.append(chunk)
                break
    return candidates


def main():
    donors = rich_donors(min_chunks=6)
    print(f"Rich donors (>= 6 chunks): {len(donors)} objectives")

    rebind_plan = {}  # target_id -> list of chunk ids

    for target_id, keywords in KEYWORD_MAP.items():
        before = current_count(target_id)
        candidates = find_candidates(target_id, keywords, donors)

        # Exclude chunks already at the target
        candidates = [c for c in candidates if c["objective_id"] != target_id]

        print(f"\n{'='*55}")
        print(f"{target_id} [{before} chunks]  -> +{len(candidates)} candidates")
        print(f"  keywords: {keywords[:3]}{'...' if len(keywords) > 3 else ''}")
        for c in candidates[:8]:
            preview = c["chunk_text"][:180].replace("\n", " ")
            print(f"  id={c['id']} from={c['objective_id']} type={c['content_type']}")
            print(f"    {preview}")

        rebind_plan[target_id] = [c["id"] for c in candidates]

    # ── Summary ────────────────────────────────────────────────────────
    print("\n\n" + "=" * 55)
    print("REBIND PLAN")
    print("=" * 55)
    total_rebinds = 0
    for tid, ids in rebind_plan.items():
        if ids:
            print(f"  {tid}: +{len(ids)} chunks (currently {current_count(tid)})")
            total_rebinds += len(ids)
    print(f"\nTotal chunks to rebind: {total_rebinds}")

    if not any(rebind_plan.values()):
        print("Nothing to rebind.")
        return

    # ── Execute ────────────────────────────────────────────────────────
    print("\n\n" + "=" * 55)
    print("EXECUTING")
    print("=" * 55)
    for target_id, ids in rebind_plan.items():
        if not ids:
            continue
        id_str = ",".join(str(i) for i in ids)
        db.execute(
            f"UPDATE chunks SET objective_id = ? WHERE id IN ({id_str})",
            (target_id,),
        )
    db.commit()

    print("\nPost-rebind counts for targets:")
    for tid in KEYWORD_MAP:
        after = current_count(tid)
        print(f"  {tid}: {after}")

    print("\nDonor check (any previously-rich donor now < 6?):")
    for oid in donors:
        cnt = current_count(oid)
        if cnt < 6:
            print(f"  WARNING: {oid} dropped to {cnt}")

    print("\nFull zero check:")
    zeros = db.execute(
        """
        SELECT o.objective_id FROM objectives o
        LEFT JOIN chunks c ON c.objective_id = o.objective_id
        WHERE o.subject_id = ?
        GROUP BY o.objective_id
        HAVING COUNT(c.id) = 0
        """,
        (SUBJECT,),
    ).fetchall()
    if zeros:
        for z in zeros:
            print(f"  ZERO: {z['objective_id']}")
    else:
        print("  All 106 objectives still have >= 1 chunk.")


if __name__ == "__main__":
    main()
