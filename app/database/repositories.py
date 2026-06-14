# app/database/repositories.py

from app.database.db import get_conn


def create_session(title: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (title) VALUES (?)",
            (title,)
        )
        conn.commit()
        return cur.lastrowid


def list_sessions():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at
            FROM sessions
            ORDER BY updated_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def add_message(session_id: int, role: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (session_id, role, content)
            VALUES (?, ?, ?)
            """,
            (session_id, role, content)
        )

        conn.execute(
            """
            UPDATE sessions
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,)
        )

        conn.commit()
        return cur.lastrowid


def get_messages(session_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,)
        ).fetchall()
        return [dict(row) for row in rows]