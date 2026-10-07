"""订单工作文件的唯一路径入口（源码与打包运行共用）。"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path, PureWindowsPath

from app.config import BASE_DIR

ORDERS_DIR = BASE_DIR / "orders"
ORDER_INPUT_DIR = ORDERS_DIR / "input"
ORDER_CONFIG_DIR = ORDERS_DIR / "config"
ORDER_OUTPUT_DIR = ORDERS_DIR / "output"
ORDER_ARCHIVE_DIR = ORDERS_DIR / "archive"
WORKSPACE_DIR = BASE_DIR / "workspace"
TEMP_DIR = BASE_DIR / "temp"

PRODUCT_CONFIG_SUFFIX = "_商品配置"
SHARE_RESULT_SUFFIX = "_均摊结果"
BULK_RESULT_SUFFIX = "_大货结果"
MEMBER_RESULT_SUFFIX = "_成员检查结果"
RESULT_SUFFIXES = (SHARE_RESULT_SUFFIX, BULK_RESULT_SUFFIX, MEMBER_RESULT_SUFFIX)


def ensure_order_dirs() -> None:
    for path in (ORDER_INPUT_DIR, ORDER_CONFIG_DIR, ORDER_OUTPUT_DIR,
                 ORDER_ARCHIVE_DIR, WORKSPACE_DIR, TEMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def sanitize_filename(value: str) -> str:
    name = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "_", str(value or "").strip()).rstrip(" .")
    if not name:
        raise ValueError("名称为空或无法转换为有效文件名")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", name, re.I):
        name = "_" + name
    return name


def get_order_input_path(value: str | Path, *, default_order_dir: str | Path | None = None) -> str:
    raw = str(value).strip().strip('"').strip("'").strip()
    if not raw:
        return ""
    # PureWindowsPath 也使 Windows 路径在跨平台测试时保持原样。
    if not PureWindowsPath(raw).suffix:
        raw += ".xlsx"
    if not any(separator in raw for separator in ("/", "\\")) and not PureWindowsPath(raw).drive:
        return str(Path(default_order_dir) / raw if default_order_dir is not None else ORDER_INPUT_DIR / raw)
    return raw


def get_workspace_dir(session_id: int | str, *, create: bool = True) -> Path:
    value = str(session_id)
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise ValueError(f"无效会话编号：{session_id}")
    path = WORKSPACE_DIR / str(int(value))
    if path.is_symlink() or not is_within(path, WORKSPACE_DIR):
        raise ValueError(f"工作目录超出 workspace：{path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def format_order_path(value: str | Path | None, *, empty: str = "未设置") -> str:
    """仅用于展示；input 内显示文件名，其余显示绝对路径。"""
    if not value or not str(value).strip():
        return empty
    path = Path(get_order_input_path(value)).expanduser().resolve()
    return path.name if is_within(path, ORDER_INPUT_DIR) else str(path)


def get_parsed_orders_path(session_id: int | str) -> Path:
    return get_workspace_dir(session_id) / "parsed_orders.csv"


def get_chat_history_path(session_id: int | str, room_wxid: str, *, filtered: bool = True) -> Path:
    """聊天记录最终路径只使用会话 ID 和真实群聊 wxid。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+@chatroom", room_wxid):
        raise ValueError("无效群聊 wxid，无法确定聊天记录文件名")
    workspace = get_workspace_dir(session_id)
    suffix = "" if filtered else "_unfiltered"
    path = workspace / f"{room_wxid}{suffix}.csv"
    if not is_within(path, workspace):
        raise ValueError("聊天记录文件超出当前工作目录")
    return path


def _group_file(directory: Path, group_name: str, suffix: str, extension: str = ".csv") -> Path:
    if not re.fullmatch(r"\.[A-Za-z0-9]+", extension):
        raise ValueError(f"无效扩展名：{extension}")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{sanitize_filename(group_name)}{suffix}{extension}"


def get_product_config_path(group_name: str) -> Path:
    return _group_file(ORDER_CONFIG_DIR, group_name, PRODUCT_CONFIG_SUFFIX)


def get_share_output_path(group_name: str, extension: str = ".csv") -> Path:
    return _group_file(ORDER_OUTPUT_DIR, group_name, SHARE_RESULT_SUFFIX, extension)


def get_bulk_output_path(group_name: str, extension: str = ".csv") -> Path:
    return _group_file(ORDER_OUTPUT_DIR, group_name, BULK_RESULT_SUFFIX, extension)


def get_member_output_path(group_name: str, extension: str = ".txt") -> Path:
    return _group_file(ORDER_OUTPUT_DIR, group_name, MEMBER_RESULT_SUFFIX, extension)


def get_archive_dir(group_name: str | None, session_id: int | str) -> Path:
    session_name = get_workspace_dir(session_id, create=False).name
    return ORDER_ARCHIVE_DIR / f"{sanitize_filename(group_name or '未命名')}_{datetime.now():%Y%m%d}_{session_name}"


def is_within(path: str | Path, directory: str | Path) -> bool:
    return Path(path).resolve().is_relative_to(Path(directory).resolve())


def get_group_output_files(group_name: str | None) -> list[Path]:
    """精确匹配群名和已注册后缀，不使用可能命中其他群的前缀 glob。"""
    if not group_name or not ORDER_OUTPUT_DIR.exists():
        return []
    stems = {(sanitize_filename(group_name) + suffix).casefold() for suffix in RESULT_SUFFIXES}
    return sorted(path for path in ORDER_OUTPUT_DIR.iterdir()
                  if path.is_file() and path.stem.casefold() in stems)
