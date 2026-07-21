# app/database/db.py

import sqlite3
from pathlib import Path

from app.config import DB_PATH, ensure_dirs


CURRENT_DB_VERSION = 1


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
