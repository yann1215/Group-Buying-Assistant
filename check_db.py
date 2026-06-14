import sqlite3
from pathlib import Path

db_path = Path("data/app.db")

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT * FROM messages").fetchall()

for row in rows:
    print(dict(row))

conn.close()