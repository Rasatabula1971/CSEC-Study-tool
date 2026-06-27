"""
Targeted remediation for the POA rebind side-effects.

Three donors were fully drained:
  POA-5.6  (use ratios to determine profitability)  → drained by POA-9.10 rebind (62 chunks)
  POA-7.1  (explain uses of control systems)        → drained by POA-7.2 rebind (2 chunks)
  POA-7.3  (distinguish errors re: trial balance)   → drained by POA-7.2 + POA-7.6 rebinds

Fix strategy:
  POA-5.6 : From the 62 chunks now at POA-9.10, return any that lack company-context
             keywords. Company-context = limited, shareholder, corporate, plc, llc,
             preference, debenture, dividend, ordinary share, rights issue.
             Everything without those markers is sole-trader ratio content → POA-5.6.
  POA-7.1 : Return 1 chunk from POA-7.2 that mentions "uses of control systems" or
             "explain the ... control systems".
  POA-7.3 : Return 1 chunk from POA-7.6 that mentions "errors which affect" / "errors
             which do not affect" / "distinguish" in a trial-balance context.
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


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def count(obj_id):
    return db.execute(
        "SELECT COUNT(*) FROM chunks WHERE objective_id = ? AND subject_id = ?",
        (obj_id, SUBJECT)
    ).fetchone()[0]


def show_counts(labels):
    for oid in labels:
        print(f"  {oid}: {count(oid)}")


# ─────────────────────────────────────────────────────────────────────
# Fix 1 — restore POA-5.6 from POA-9.10
# ─────────────────────────────────────────────────────────────────────

COMPANY_KEYWORDS = [
    "limited", "shareholder", "corporate", "plc ", " llc", " ltd",
    "preference share", "ordinary share", "debenture", "dividend",
    "rights issue", "company", "co-operative", "corporation",
    "limited liability", "public company", "private company",
    "liquidation", "winding up",
]

poa910_chunks = db.execute(
    """
    SELECT id, chunk_text FROM chunks
    WHERE objective_id = ? AND subject_id = ?
    """,
    ("POA-9.10", SUBJECT),
).fetchall()

restore_to_56 = []
keep_at_910 = []

for c in poa910_chunks:
    text_lower = c["chunk_text"].lower()
    if any(kw in text_lower for kw in COMPANY_KEYWORDS):
        keep_at_910.append(c["id"])
    else:
        restore_to_56.append(c["id"])

print("=" * 60)
print("FIX 1: POA-5.6 ← sole-trader ratio chunks from POA-9.10")
print("=" * 60)
print(f"  Chunks currently at POA-9.10 : {len(poa910_chunks)}")
print(f"  Company-context (stay 9.10)  : {len(keep_at_910)}")
print(f"  Sole-trader context (→ 5.6)  : {len(restore_to_56)}")

if restore_to_56:
    ids = ",".join(str(i) for i in restore_to_56)
    db.execute(
        f"UPDATE chunks SET objective_id = 'POA-5.6' WHERE id IN ({ids})"
    )
    db.commit()
    print(f"  → Restored {len(restore_to_56)} chunks to POA-5.6")
    print(f"  POA-5.6 now: {count('POA-5.6')}")
    print(f"  POA-9.10 now: {count('POA-9.10')}")
else:
    print("  Nothing to restore (all chunks have company keywords).")


# ─────────────────────────────────────────────────────────────────────
# Fix 2 — restore POA-7.1 from POA-7.2
# ─────────────────────────────────────────────────────────────────────

poa72_chunks = db.execute(
    """
    SELECT id, chunk_text FROM chunks
    WHERE objective_id = 'POA-7.2' AND subject_id = ?
    """,
    (SUBJECT,),
).fetchall()

CONTROL_USES_KW = [
    "uses of control systems",
    "uses of control",
    "explain the uses",
    "control systems in the accounting",
    "why control systems",
    "purpose of control systems",
    "purpose of control",
]

restore_to_71 = []
for c in poa72_chunks:
    text_lower = c["chunk_text"].lower()
    if any(kw in text_lower for kw in CONTROL_USES_KW):
        restore_to_71.append(c["id"])

# Fallback: take the first chunk from POA-7.2 if nothing matches
if not restore_to_71 and poa72_chunks:
    # Look for syllabus-page content
    for c in poa72_chunks:
        tl = c["chunk_text"].lower()
        if "explain the uses" in tl or "control systems" in tl:
            restore_to_71.append(c["id"])
            break
    # If still nothing, take the first chunk containing "control"
    if not restore_to_71:
        for c in poa72_chunks:
            if "control" in c["chunk_text"].lower():
                restore_to_71 = [c["id"]]
                break

print("\n" + "=" * 60)
print("FIX 2: POA-7.1 ← control-uses chunk from POA-7.2")
print("=" * 60)
print(f"  Chunks at POA-7.2: {len(poa72_chunks)}")
print(f"  Candidates for POA-7.1: {len(restore_to_71)}")

if restore_to_71:
    # Only take 1 chunk - leave the rest at POA-7.2
    take_one = [restore_to_71[0]]
    db.execute(
        f"UPDATE chunks SET objective_id = 'POA-7.1' WHERE id = ?",
        (take_one[0],)
    )
    db.commit()
    print(f"  → Restored chunk {take_one[0]} to POA-7.1")
    print(f"  POA-7.1 now: {count('POA-7.1')}")
    print(f"  POA-7.2 now: {count('POA-7.2')}")
else:
    print("  No suitable chunk found for POA-7.1.")


# ─────────────────────────────────────────────────────────────────────
# Fix 3 — restore POA-7.3 from POA-7.6
# ─────────────────────────────────────────────────────────────────────

poa76_chunks = db.execute(
    """
    SELECT id, chunk_text FROM chunks
    WHERE objective_id = 'POA-7.6' AND subject_id = ?
    """,
    (SUBJECT,),
).fetchall()

ERROR_KW = [
    "errors which affect",
    "errors which do not affect",
    "distinguish between",
    "affect the trial balance",
    "do not affect the trial balance",
    "errors that affect",
    "errors that do not",
    "which errors",
]

restore_to_73 = []
for c in poa76_chunks:
    text_lower = c["chunk_text"].lower()
    if any(kw in text_lower for kw in ERROR_KW):
        restore_to_73.append(c["id"])

print("\n" + "=" * 60)
print("FIX 3: POA-7.3 ← error-distinction chunk from POA-7.6")
print("=" * 60)
print(f"  Chunks at POA-7.6: {len(poa76_chunks)}")
print(f"  Candidates for POA-7.3: {len(restore_to_73)}")

if restore_to_73:
    take_one = [restore_to_73[0]]
    db.execute(
        f"UPDATE chunks SET objective_id = 'POA-7.3' WHERE id = ?",
        (take_one[0],)
    )
    db.commit()
    print(f"  → Restored chunk {take_one[0]} to POA-7.3")
    print(f"  POA-7.3 now: {count('POA-7.3')}")
    print(f"  POA-7.6 now: {count('POA-7.6')}")
else:
    # Try POA-7.2 as a fallback - it also absorbed some POA-7.3 content
    poa72_fresh = db.execute(
        """
        SELECT id, chunk_text FROM chunks
        WHERE objective_id = 'POA-7.2' AND subject_id = ?
        """,
        (SUBJECT,),
    ).fetchall()
    for c in poa72_fresh:
        text_lower = c["chunk_text"].lower()
        if any(kw in text_lower for kw in ERROR_KW):
            restore_to_73.append(c["id"])

    if restore_to_73:
        db.execute(
            f"UPDATE chunks SET objective_id = 'POA-7.3' WHERE id = ?",
            (restore_to_73[0],)
        )
        db.commit()
        print(f"  → Restored chunk {restore_to_73[0]} from POA-7.2 to POA-7.3")
        print(f"  POA-7.3 now: {count('POA-7.3')}")
        print(f"  POA-7.2 now: {count('POA-7.2')}")
    else:
        print("  No suitable error-distinction chunk found.")


# ─────────────────────────────────────────────────────────────────────
# Final summary
# ─────────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
print("FINAL COVERAGE (objectives touched in this run)")
print("=" * 60)
ALL_TOUCHED = [
    "POA-5.6", "POA-7.1", "POA-7.3",   # restored
    "POA-9.10", "POA-7.2", "POA-7.6",  # donors
    # original targets that were fixed
    "POA-3.5", "POA-4.7", "POA-4.9", "POA-6.7",
    "POA-7.11", "POA-8.3", "POA-8.10", "POA-9.10",
]
show_counts(sorted(set(ALL_TOUCHED)))

# zero check
zero_rows = db.execute(
    """
    SELECT o.objective_id, o.content_stmt
    FROM objectives o
    LEFT JOIN chunks c ON c.objective_id = o.objective_id
    WHERE o.subject_id = ?
    GROUP BY o.objective_id
    HAVING COUNT(c.id) = 0
    ORDER BY o.objective_id
    """,
    (SUBJECT,),
).fetchall()
print(f"\nTotal objectives with zero chunks: {len(zero_rows)}")
for r in zero_rows:
    print(f"  {r['objective_id']}: {r['content_stmt'][:60]}")
