"""
Repair over-aggressive rebinds from _poa_enrich.py.

Problems:
  POA-11.1  207 chunks  -- 'cheque' keyword hit every transaction in the corpus
  POA-4.2    54 chunks  -- 'general ledger' / 'sales ledger' appear everywhere
  POA-9.2    58 chunks  -- 'private limited company' common in section 9 content
  POA-10.5   57 chunks  -- 'direct cost' appears in all manufacturing questions

Strategy per objective:
  1. Apply a TIGHT "topic is really about this" filter to the current chunks.
  2. Chunks that fail the tight filter need a new home.
  3. Infer new home from section indicators in the chunk text.
     - Fallback: use the 'source_family' + 'content_type' to guess section.
"""

import os
import sys
import re

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

import backend.db.init_db as v1

DB_PATH = os.getenv("DB_PATH")
db = v1.open_db(DB_PATH)
SUBJECT = "Principles_of_Accounts"

SECTION_RE = re.compile(
    r"SECTION\s+(\d+)", re.IGNORECASE
)

# Map section number → a representative high-chunk objective in that section
# Used to park displaced chunks
SECTION_FALLBACK = {
    "1":  "POA-1.2",    "2":  "POA-2.4",   "3":  "POA-3.2",
    "4":  "POA-4.3",    "5":  "POA-5.3",   "6":  "POA-6.3",
    "7":  "POA-7.10",   "8":  "POA-8.7",   "9":  "POA-9.6",
    "10": "POA-10.3",   "11": "POA-11.3",
}


def count(oid):
    return db.execute(
        "SELECT COUNT(*) FROM chunks WHERE objective_id=? AND subject_id=?",
        (oid, SUBJECT),
    ).fetchone()[0]


def infer_section(text):
    """Return best-guess section number from text content, or None."""
    m = SECTION_RE.search(text)
    if m:
        return m.group(1)
    # Try objective-ID markers embedded in notes TOC / SLMS content
    for s, oid in SECTION_FALLBACK.items():
        prefix = oid.split("-")[1].split(".")[0]  # e.g. "11"
        if f"SECTION {s}" in text.upper() or f"SECTION {prefix}" in text.upper():
            return s
    return None


def park(chunk_ids, default_oid):
    """Rebind a list of chunk IDs back to default_oid unless a better section is found."""
    moves = {}  # oid -> list of chunk_ids
    for cid in chunk_ids:
        text = db.execute("SELECT chunk_text FROM chunks WHERE id=?", (cid,)).fetchone()
        if not text:
            continue
        sec = infer_section(text["chunk_text"])
        dest = SECTION_FALLBACK.get(sec, default_oid)
        moves.setdefault(dest, []).append(cid)

    for dest, ids in moves.items():
        id_str = ",".join(str(i) for i in ids)
        db.execute(f"UPDATE chunks SET objective_id=? WHERE id IN ({id_str})", (dest,))

    return sum(len(v) for v in moves.values())


def trim(target_oid, tight_filter_fns, default_park_oid, max_keep=None):
    """
    From the current chunks at target_oid:
      - Keep chunks where ANY tight_filter_fn returns True.
      - Park the rest to their inferred sections (or default_park_oid).
      - If max_keep is set, also trim by content length (longest = most content first).
    Returns (kept, parked).
    """
    rows = db.execute(
        """
        SELECT c.id, c.chunk_text, d.content_type
        FROM chunks c
        JOIN documents d ON d.doc_id = c.doc_id
        WHERE c.objective_id=? AND c.subject_id=?
        """,
        (target_oid, SUBJECT),
    ).fetchall()

    keep_ids = []
    park_ids = []
    for r in rows:
        tl = r["chunk_text"].lower()
        if any(f(tl) for f in tight_filter_fns):
            keep_ids.append(r["id"])
        else:
            park_ids.append(r["id"])

    if max_keep and len(keep_ids) > max_keep:
        # Trim keep list by chunk length (longest first → richest content)
        lengths = {r["id"]: len(r["chunk_text"]) for r in rows if r["id"] in keep_ids}
        keep_ids.sort(key=lambda i: lengths.get(i, 0), reverse=True)
        park_ids.extend(keep_ids[max_keep:])
        keep_ids = keep_ids[:max_keep]

    parked = park(park_ids, default_park_oid)
    db.commit()
    return len(keep_ids), parked


print("=" * 60)
print("TRIM 1: POA-11.1 (methods of payment)")
print("=" * 60)
print(f"  Before: {count('POA-11.1')} chunks")

# Very tight: the TOPIC must be payment methods
pmt_filters = [
    lambda t: "method of payment" in t,
    lambda t: "methods of payment" in t,
    lambda t: "direct debit" in t and "payment" in t,
    lambda t: "standing order" in t and "payment" in t,
    lambda t: "electronic fund transfer" in t,
    lambda t: "eftpos" in t,
    lambda t: "electronic transfer" in t and "method" in t,
    lambda t: "method" in t and "payment" in t and (
        "cheque" in t or "cash" in t or "debit card" in t or "credit card" in t
    ),
]
kept, parked = trim("POA-11.1", pmt_filters, "POA-3.2", max_keep=20)
print(f"  Kept: {kept}  Parked to sections: {parked}")
print(f"  After: {count('POA-11.1')} chunks")


print()
print("=" * 60)
print("TRIM 2: POA-4.2 (types of ledgers)")
print("=" * 60)
print(f"  Before: {count('POA-4.2')} chunks")

ledger_filters = [
    lambda t: "types of ledger" in t,
    lambda t: "types of account book" in t,
    lambda t: "general ledger" in t and ("types" in t or "nominal" in t or "personal" in t or "real" in t),
    lambda t: "identify" in t and "ledger" in t,
    lambda t: "nominal ledger" in t and "real ledger" in t,
    lambda t: "subsidiary ledger" in t and ("general" in t or "type" in t),
    lambda t: "classes of" in t and "ledger" in t,
    lambda t: "division" in t and "ledger" in t,
    lambda t: "general ledger" in t and "subsidiary" in t,
    lambda t: "purchases ledger" in t and "sales ledger" in t and "general ledger" in t,
]
kept, parked = trim("POA-4.2", ledger_filters, "POA-4.3", max_keep=20)
print(f"  Kept: {kept}  Parked to sections: {parked}")
print(f"  After: {count('POA-4.2')} chunks")


print()
print("=" * 60)
print("TRIM 3: POA-10.5 (basic costing principles)")
print("=" * 60)
print(f"  Before: {count('POA-10.5')} chunks")

costing_filters = [
    lambda t: "costing principle" in t,
    lambda t: "basic costing" in t,
    lambda t: "prime cost" in t and "definition" in t,
    lambda t: "direct cost" in t and "indirect cost" in t and ("principle" in t or "concept" in t or "classify" in t or "classification" in t),
    lambda t: "apply" in t and "costing" in t,
    lambda t: "cost concept" in t,
    lambda t: "total cost" in t and "principle" in t,
    lambda t: ("fifo" in t or "lifo" in t or "avco" in t) and "cost" in t and "principle" in t,
    lambda t: "applying basic costing" in t,
    lambda t: "marginal cost" in t,
    lambda t: "absorption cost" in t,
]
kept, parked = trim("POA-10.5", costing_filters, "POA-10.3", max_keep=15)
print(f"  Kept: {kept}  Parked to sections: {parked}")
print(f"  After: {count('POA-10.5')} chunks")


print()
print("=" * 60)
print("TRIM 4: POA-9.2 (types of limited liability companies)")
print("=" * 60)
print(f"  Before: {count('POA-9.2')} chunks")

company_type_filters = [
    lambda t: "types of" in t and ("company" in t or "limited" in t or "co-operative" in t),
    lambda t: "private limited company" in t and "public limited company" in t,
    lambda t: "plc" in t and "ltd" in t,
    lambda t: "identify" in t and ("company" in t or "limited liability") and "type" in t,
    lambda t: "private company" in t and "public company" in t,
    lambda t: "co-operative" in t and ("limited" in t or "company" in t) and "type" in t,
    lambda t: "identify the types" in t,
    lambda t: "types of limited" in t,
]
kept, parked = trim("POA-9.2", company_type_filters, "POA-9.1", max_keep=20)
print(f"  Kept: {kept}  Parked to sections: {parked}")
print(f"  After: {count('POA-9.2')} chunks")


print()
print("=" * 60)
print("TRIM 5: POA-9.8 (appropriate profits between dividends/reserves)")
print("=" * 60)
print(f"  Before: {count('POA-9.8')} chunks")

approp_filters = [
    lambda t: "appropriation" in t and ("dividend" in t or "reserve" in t),
    lambda t: "transfer to" in t and "reserve" in t,
    lambda t: "retained earnings" in t and ("dividend" in t or "reserve" in t),
    lambda t: "general reserve" in t and "dividend" in t,
    lambda t: "appropriate" in t and "profit" in t and ("dividend" in t or "reserve" in t),
    lambda t: "profit appropriation" in t,
    lambda t: "dividends and reserves" in t,
    lambda t: "between dividend" in t,
    lambda t: "transfer profit" in t,
]
kept, parked = trim("POA-9.8", approp_filters, "POA-9.6", max_keep=25)
print(f"  Kept: {kept}  Parked to sections: {parked}")
print(f"  After: {count('POA-9.8')} chunks")


# ── Final state ──────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("FINAL COVERAGE CHECK")
print("=" * 60)
rows = db.execute(
    """
    SELECT o.objective_id, COUNT(c.id) as cnt
    FROM objectives o
    LEFT JOIN chunks c ON c.objective_id = o.objective_id
    WHERE o.subject_id = ?
    GROUP BY o.objective_id
    ORDER BY cnt DESC, o.objective_id
    """,
    (SUBJECT,),
).fetchall()

total = db.execute("SELECT COUNT(*) FROM chunks WHERE subject_id=?", (SUBJECT,)).fetchone()[0]
print(f"Total chunks (should still be 2286): {total}")

zeros = [r for r in rows if r["cnt"] == 0]
thin = [r for r in rows if 1 <= r["cnt"] <= 3]
heavy = [r for r in rows if r["cnt"] >= 100]

if zeros:
    print("ZEROS:")
    for r in zeros:
        print(f"  {r['objective_id']}")
else:
    print("No zeros.")

print(f"\nThin (1-3 chunks): {len(thin)}")
for r in thin:
    print(f"  {r['objective_id']}: {r['cnt']}")

print(f"\nHeavy (>= 100 chunks): {len(heavy)}")
for r in heavy:
    print(f"  {r['objective_id']}: {r['cnt']}")

print("\nObjectives that were over-bound → after trim:")
for oid in ["POA-11.1", "POA-4.2", "POA-10.5", "POA-9.2", "POA-9.8"]:
    print(f"  {oid}: {count(oid)}")

print("\nPreviously-thin objectives that were donors:")
for oid in ["POA-4.4", "POA-3.4", "POA-11.2", "POA-7.4"]:
    print(f"  {oid}: {count(oid)}")
