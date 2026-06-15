import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env")

from backend.db.init_db import open_db

db = open_db(os.getenv("DB_PATH"))
rows = db.execute(
    "SELECT COUNT(*) as n FROM mark_points WHERE question_id NOT LIKE '%-stem'"
).fetchone()
print("Non-stem mark_points:", rows["n"])

if rows["n"] > 0:
    sample = db.execute(
        "SELECT mark_point_id, question_id FROM mark_points "
        "WHERE question_id NOT LIKE '%-stem' LIMIT 10"
    ).fetchall()
    print("\nSample rows:")
    for r in sample:
        print(f"  {r['mark_point_id']} | question_id={r['question_id']}")
