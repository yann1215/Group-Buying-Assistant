"""PyInstaller 启动时创建持久工作目录，不将用户数据放在解包目录。"""
from app.config import ensure_dirs

ensure_dirs()
