# app/config.py

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "app" / "outputs"

DB_PATH = DATA_DIR / "app.db"

FILES_DIR = OUTPUT_DIR / "files"
CSV_OUTPUT_DIR = OUTPUT_DIR / "csv"
LOG_DIR = OUTPUT_DIR / "logs"

OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "deepseek-coder:8b-q8_0"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    CSV_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)