import os
from dotenv import load_dotenv
load_dotenv()
from backend.db.init_db import open_db
db = open_db(os.getenv("DB_PATH"))
rows = db.execute("SELECT objective_id, COUNT(*) as cnt FROM mark_points WHERE objective_id LIKE 'ECON-%' GROUP BY objective_id ORDER BY cnt DESC").fetchall()
for r in rows: print(dict(r))
