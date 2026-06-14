# app/database/db.py

import sqlite3
from pathlib import Path

from app.config import DB_PATH, ensure_dirs


def get_conn():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    ensure_dirs()

    schema_path = Path(__file__).resolve().parent / "schema.sql"

    with get_conn() as conn:
        with open(schema_path, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()