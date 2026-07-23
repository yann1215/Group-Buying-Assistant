# app/database/db.py

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import DB_PATH, ensure_dirs


CURRENT_DB_VERSION = 2


ORDER_VERSION_COLUMNS = {
    "new_order_file": "TEXT NOT NULL DEFAULT ''",
    "new_order_updated_at": "TEXT NOT NULL DEFAULT ''",
    "old_order_file": "TEXT NOT NULL DEFAULT ''",
    "old_order_updated_at": "TEXT NOT NULL DEFAULT ''",
    "order_cache_1_file": "TEXT NOT NULL DEFAULT ''",
    "order_cache_1_updated_at": "TEXT NOT NULL DEFAULT ''",
    "order_cache_2_file": "TEXT NOT NULL DEFAULT ''",
    "order_cache_2_updated_at": "TEXT NOT NULL DEFAULT ''",
}


def get_conn() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    ensure_dirs()

    schema_path = Path(__file__).resolve().parent / "schema.sql"
    schema_sql = schema_path.read_text(encoding="utf-8")

    with get_conn() as conn:
        conn.executescript(schema_sql)
        _migrate_database(conn)

        # 迁移过程中重建表会同时移除旧索引，再执行一次可补齐索引。
        conn.executescript(schema_sql)

        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"数据库外键检查失败，共发现 {len(violations)} 条异常记录"
            )

        conn.execute(f"PRAGMA user_version = {CURRENT_DB_VERSION}")
        conn.commit()


def _migrate_database(conn: sqlite3.Connection) -> None:
    """将旧版数据库升级到当前结构，同时保留已有数据。"""
    session_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }

    if "group_name" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN group_name TEXT")

    _migrate_order_version_columns(conn)

    if _has_current_foreign_keys(conn):
        return

    existing_violations = conn.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    if existing_violations:
        raise sqlite3.IntegrityError(
            "旧数据库中存在无所属会话或任务的记录，无法安全升级外键规则"
        )

    # SQLite 不能直接修改已有外键，只能在关闭外键检查后重建相关表。
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE messages_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE files_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                saved_path TEXT NOT NULL,
                file_type TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE analysis_tasks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                user_message_id INTEGER,
                status TEXT NOT NULL,
                task_type TEXT,
                parameters_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                FOREIGN KEY (session_id)
                    REFERENCES sessions(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (user_message_id)
                    REFERENCES messages_new(id)
                    ON DELETE SET NULL
            );

            CREATE TABLE results_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                result_type TEXT NOT NULL,
                content TEXT,
                file_path TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (task_id)
                    REFERENCES analysis_tasks_new(id)
                    ON DELETE CASCADE
            );

            INSERT INTO messages_new
            SELECT * FROM messages;

            INSERT INTO files_new
            SELECT * FROM files;

            INSERT INTO analysis_tasks_new
            SELECT * FROM analysis_tasks;

            INSERT INTO results_new
            SELECT * FROM results;

            DROP TABLE results;
            DROP TABLE analysis_tasks;
            DROP TABLE files;
            DROP TABLE messages;

            ALTER TABLE messages_new RENAME TO messages;
            ALTER TABLE files_new RENAME TO files;
            ALTER TABLE analysis_tasks_new RENAME TO analysis_tasks;
            ALTER TABLE results_new RENAME TO results;

            COMMIT;
            """
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _migrate_order_version_columns(conn: sqlite3.Connection) -> None:
    """补齐订单版本字段，并迁移旧会话中已有的订单信息。"""
    context_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(session_contexts)"
        ).fetchall()
    }

    for column_name, column_definition in ORDER_VERSION_COLUMNS.items():
        if column_name in context_columns:
            continue
        conn.execute(
            f"ALTER TABLE session_contexts "
            f"ADD COLUMN {column_name} {column_definition}"
        )

    # 某些旧数据库可能曾把当前订单直接保存在 order_file 列中。
    if "order_file" in context_columns:
        conn.execute(
            """
            UPDATE session_contexts
            SET new_order_file = TRIM(COALESCE(order_file, '')),
                new_order_updated_at = CASE
                    WHEN TRIM(COALESCE(new_order_updated_at, '')) = ''
                    THEN COALESCE(updated_at, CURRENT_TIMESTAMP)
                    ELSE new_order_updated_at
                END
            WHERE TRIM(COALESCE(new_order_file, '')) = ''
              AND TRIM(COALESCE(order_file, '')) != ''
            """
        )

    # 当前项目的旧版实际将订单保存在 context_json.order_input 中。
    rows = conn.execute(
        """
        SELECT session_id, context_json, updated_at
        FROM session_contexts
        WHERE TRIM(COALESCE(new_order_file, '')) = ''
        """
    ).fetchall()
    for row in rows:
        order_file = _legacy_order_file_from_json(row["context_json"])
        if not order_file:
            continue
        conn.execute(
            """
            UPDATE session_contexts
            SET new_order_file = ?,
                new_order_updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)
            WHERE session_id = ?
              AND TRIM(COALESCE(new_order_file, '')) = ''
            """,
            (order_file, row["session_id"]),
        )


def _legacy_order_file_from_json(context_json: Any) -> str:
    """从旧版会话 JSON 中提取订单路径；无有效路径时返回空串。"""
    try:
        context = json.loads(context_json)
    except (TypeError, json.JSONDecodeError):
        return ""

    if not isinstance(context, dict):
        return ""

    order_input = context.get("order_input")
    if isinstance(order_input, str):
        return order_input.strip()

    if isinstance(order_input, dict):
        for key in ("file_path", "order_file", "path"):
            value = order_input.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    return ""


def _has_current_foreign_keys(conn: sqlite3.Connection) -> bool:
    expected = {
        "messages": {("sessions", "CASCADE")},
        "session_contexts": {("sessions", "CASCADE")},
        "files": {("sessions", "CASCADE")},
        "analysis_tasks": {
            ("sessions", "CASCADE"),
            ("messages", "SET NULL"),
        },
        "results": {("analysis_tasks", "CASCADE")},
    }

    for table_name, expected_rules in expected.items():
        actual_rules = {
            (row["table"], row["on_delete"])
            for row in conn.execute(
                f"PRAGMA foreign_key_list({table_name})"
            ).fetchall()
        }
        if actual_rules != expected_rules:
            return False

    return True