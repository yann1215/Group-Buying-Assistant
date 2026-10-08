# app/config.py
from __future__ import annotations

import sys
import os
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

DEFAULT_MODEL = "Qwen3-1.7B-Q8_0"
MODEL_RESOURCE = "models/Qwen3-1.7B-Q8_0.gguf"
LLM_MODEL_PATH = os.environ.get("GROUP_BUYING_MODEL_PATH")
LLM_THREADS = min(8, max(1, (os.cpu_count() or 2) // 2))
LLM_GPU_LAYERS = 0  # 默认 CPU；使用支持 GPU 的 llama-cpp-python 构建后可调整。
LLM_CONTEXT_SIZE = 16384
KNOWLEDGE_RESOURCE = "knowledge/knowledge.md"
INSTRUCTION_MAX_TOKENS = 1024
TRANSFER_MAX_TOKENS = 4096


def get_resource_path(relative: str) -> Path:
    """用户可编辑资源优先，打包的默认资源其次。"""
    local = BASE_DIR / relative
    if local.is_file():
        return local
    return Path(getattr(sys, "_MEIPASS", BASE_DIR)) / relative


def ensure_dirs() -> None:
    from app.core.path_manager import ensure_order_dirs

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ensure_order_dirs()
