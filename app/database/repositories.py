# app/database/repositories.py

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database.db import get_conn


MAX_SESSION_COUNT = 20

ORDER_VERSION_FIELDS = (
    "new_order_file",
    "new_order_updated_at",
    "old_order_file",
    "old_order_updated_at",
    "order_cache_1_file",
    "order_cache_1_updated_at",
    "order_cache_2_file",
    "order_cache_2_updated_at",
)

ORDER_FILE_FIELDS = (
    "new_order_file",
    "old_order_file",
    "order_cache_1_file",
    "order_cache_2_file",
)

ORDER_UPDATED_AT_FIELDS = (
    "new_order_updated_at",
    "old_order_updated_at",
    "order_cache_1_updated_at",
    "order_cache_2_updated_at",
)


def create_session(
    title: str = "新对话",
    group_name: str | None = None,
) -> int:
    normalized_title = str(title).strip() or "新对话"
    normalized_group_name = _normalize_optional_text(group_name)

    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sessions (title, group_name)
            VALUES (?, ?)
            """,
            (normalized_title, normalized_group_name),
        )
        session_id = int(cur.lastrowid)

        _prune_old_sessions(conn, MAX_SESSION_COUNT)
        conn.commit()
        return session_id


def get_session(session_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, title, group_name, created_at, updated_at
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None


def list_sessions(limit: int = MAX_SESSION_COUNT) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, title, group_name, created_at, updated_at
            FROM sessions
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_session(
    session_id: int,
    *,
    title: str | None = None,
    group_name: str | None = None,
) -> bool:
    updates: list[str] = []
    values: list[Any] = []

    if title is not None:
        updates.append("title = ?")
        values.append(str(title).strip() or "新对话")

    if group_name is not None:
        updates.append("group_name = ?")
        values.append(_normalize_optional_text(group_name))

    if not updates:
        return get_session(session_id) is not None

    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(session_id)

    with get_conn() as conn:
        cur = conn.execute(
            f"""
            UPDATE sessions
            SET {', '.join(updates)}
            WHERE id = ?
            """,
            values,
        )
        conn.commit()
        return cur.rowcount > 0


def touch_session(session_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE sessions
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_session(session_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def prune_old_sessions(max_count: int = MAX_SESSION_COUNT) -> int:
    with get_conn() as conn:
        deleted_count = _prune_old_sessions(conn, max_count)
        conn.commit()
        return deleted_count


def add_message(session_id: int, role: str, content: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (session_id, role, content)
            VALUES (?, ?, ?)
            """,
            (session_id, role, content),
        )

        conn.execute(
            """
            UPDATE sessions
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )

        conn.commit()
        return int(cur.lastrowid)


def get_messages(session_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def save_session_context(
    session_id: int,
    context: dict[str, Any],
) -> None:
    context_to_save = dict(context)
    order_versions = {
        field: context_to_save.pop(field)
        for field in ORDER_VERSION_FIELDS
        if field in context_to_save
    }
    context_json = json.dumps(
        context_to_save,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO session_contexts (
                session_id,
                context_json,
                updated_at
            )
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                context_json = excluded.context_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (session_id, context_json),
        )

        if order_versions:
            _update_order_versions(conn, session_id, order_versions)

        conn.execute(
            """
            UPDATE sessions
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        conn.commit()


def load_session_context(session_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT context_json, {', '.join(ORDER_VERSION_FIELDS)}
            FROM session_contexts
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

    if row is None:
        return {}

    try:
        context = json.loads(row["context_json"])
    except (TypeError, json.JSONDecodeError):
        context = {}

    if not isinstance(context, dict):
        context = {}

    for field in ORDER_VERSION_FIELDS:
        context[field] = row[field] or ""

    return context


def get_order_versions(session_id: int) -> dict[str, str]:
    """读取会话保存的四个订单版本路径及其更新时间。"""
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT {', '.join(ORDER_VERSION_FIELDS)}
            FROM session_contexts
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()

    if row is None:
        return {field: "" for field in ORDER_VERSION_FIELDS}

    return {field: row[field] or "" for field in ORDER_VERSION_FIELDS}


def update_order_versions(
    session_id: int,
    *,
    new_order_file: str,
    new_order_updated_at: str,
    old_order_file: str,
    old_order_updated_at: str,
    order_cache_1_file: str,
    order_cache_1_updated_at: str,
    order_cache_2_file: str,
    order_cache_2_updated_at: str,
) -> bool:
    """一次性更新会话的四个订单版本，不覆盖其他上下文数据。"""
    order_versions = {
        "new_order_file": new_order_file,
        "new_order_updated_at": new_order_updated_at,
        "old_order_file": old_order_file,
        "old_order_updated_at": old_order_updated_at,
        "order_cache_1_file": order_cache_1_file,
        "order_cache_1_updated_at": order_cache_1_updated_at,
        "order_cache_2_file": order_cache_2_file,
        "order_cache_2_updated_at": order_cache_2_updated_at,
    }

    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if exists is None:
            return False

        conn.execute(
            """
            INSERT INTO session_contexts (session_id)
            VALUES (?)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (session_id,),
        )
        _update_order_versions(conn, session_id, order_versions)
        conn.execute(
            """
            UPDATE sessions
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        conn.commit()
        return True


def _update_order_versions(
    conn: sqlite3.Connection,
    session_id: int,
    order_versions: dict[str, Any],
) -> None:
    invalid_fields = set(order_versions) - set(ORDER_VERSION_FIELDS)
    if invalid_fields:
        raise ValueError(
            f"不支持的订单版本字段：{', '.join(sorted(invalid_fields))}"
        )

    if not order_versions:
        return

    normalized_values: list[str] = []
    for field in order_versions:
        value = order_versions[field]
        if value is None:
            normalized_values.append("")
        elif field in ORDER_FILE_FIELDS or field in ORDER_UPDATED_AT_FIELDS:
            normalized_values.append(str(value).strip())

    assignments = [f"{field} = ?" for field in order_versions]
    assignments.append("updated_at = CURRENT_TIMESTAMP")
    normalized_values.append(session_id)

    conn.execute(
        f"""
        UPDATE session_contexts
        SET {', '.join(assignments)}
        WHERE session_id = ?
        """,
        normalized_values,
    )


def _prune_old_sessions(
    conn: sqlite3.Connection,
    max_count: int,
) -> int:
    if max_count < 1:
        raise ValueError("max_count 必须大于或等于 1")

    cur = conn.execute(
        """
        DELETE FROM sessions
        WHERE id IN (
            SELECT id
            FROM sessions
            ORDER BY updated_at DESC, id DESC
            LIMIT -1 OFFSET ?
        )
        """,
        (max_count,),
    )
    return cur.rowcount


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip()
    return normalized or None