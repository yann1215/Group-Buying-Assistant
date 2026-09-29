# app/config.py
from __future__ import annotations

import sys
from pathlib import Path


def get_runtime_base_dir() -> Path:
    """
    源码运行时使用项目根目录；
    PyInstaller 打包后使用 exe 所在目录，确保数据库和输出文件可持续保存。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_runtime_base_dir()
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
LOG_DIR = BASE_DIR / "logs"

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "deepseek-coder:8b-q8_0"


def ensure_dirs() -> None:
    from app.core.path_manager import ensure_order_dirs

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ensure_order_dirs()
