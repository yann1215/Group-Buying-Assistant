from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable


def get_runtime_dir() -> Path:
    """
    用户数据、缓存、输出文件所在目录。
    打包后是 exe 所在目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = get_runtime_dir()

# PyInstaller 内置资源所在目录：
# onedir 时通常是 _internal；
# onefile 时通常是临时 _MEI... 目录；
# 源码运行时则退回项目根目录。
BUNDLE_ROOT = Path(
    getattr(sys, "_MEIPASS", PROJECT_ROOT)
).resolve()

WECHATMSG_LITE_ROOT = (
    BUNDLE_ROOT
    / "external"
    / "WeChatMsg_Lite"
)


def _ensure_wechatmsg_lite_path() -> None:
    """
    确保 external/WeChatMsg_Lite 已加入 Python 模块搜索路径。
    """
    if not WECHATMSG_LITE_ROOT.exists():
        raise FileNotFoundError(
            "未找到 WeChatMsg_Lite："
            f"{WECHATMSG_LITE_ROOT}"
        )

    root_str = str(WECHATMSG_LITE_ROOT)

    if root_str not in sys.path:
        sys.path.insert(0, root_str)


def get_wechat_group_members(
    group_name: str,
    db_dir: str | None = None,
    db_version: int = 4,
    auto_decrypt: bool = True,
    source_dir: str | None = None,
    decrypt_output_root: str = f"{PROJECT_ROOT}/temp",
    use_cache_db: bool = False,
    use_cache_key: bool = True,
    force_decrypt: bool = True,
    force_find_key: bool = False,
    allow_manual_key_input: bool = True,
    key_input_func: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """
    获取指定微信群聊的成员信息。

    数据库解密顺序：
    1. 尝试使用缓存 key；
    2. 缓存 key 失败后尝试自动识别；
    3. 自动识别失败后允许用户手动输入 key；
    4. 用户输入 auto/自动识别时重新自动识别。
    """
    _ensure_wechatmsg_lite_path()

    from member import get_member

    return get_member(
        group_name=group_name,
        db_dir=db_dir,
        db_version=db_version,
        auto_decrypt=auto_decrypt,
        source_dir=source_dir,
        decrypt_output_root=decrypt_output_root,
        use_cache_db=use_cache_db,
        use_cache_key=use_cache_key,
        force_decrypt=force_decrypt,
        force_find_key=force_find_key,
        allow_manual_key_input=allow_manual_key_input,
        key_input_func=key_input_func,
    )


def get_wechat_group_messages(
    group_name: str,
    start_time: str,
    end_time: str,
    options: dict[str, Any] | None = None,
    output_format: str = "csv",
    db_dir: str | None = None,
    output_dir: str = f"{PROJECT_ROOT}/temp",
    db_version: int = 4,
    auto_decrypt: bool = True,
    source_dir: str | None = None,
    decrypt_output_root: str = f"{PROJECT_ROOT}/temp",
    use_cache_db: bool = False,
    use_cache_key: bool = True,
    force_decrypt: bool = True,
    force_find_key: bool = False,
    allow_manual_key_input: bool = True,
    key_input_func: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """
    获取并导出指定微信群聊的聊天记录。

    数据库解密顺序：
    1. 尝试使用缓存 key；
    2. 缓存 key 失败后尝试自动识别；
    3. 自动识别失败后允许用户手动输入 key；
    4. 用户输入 auto/自动识别时重新自动识别。
    """
    _ensure_wechatmsg_lite_path()

    from msg import get_msg

    return get_msg(
        group_name=group_name,
        start_time=start_time,
        end_time=end_time,
        options=options,
        output_format=output_format,
        db_dir=db_dir,
        output_dir=output_dir,
        db_version=db_version,
        auto_decrypt=auto_decrypt,
        source_dir=source_dir,
        decrypt_output_root=decrypt_output_root,
        use_cache_db=use_cache_db,
        use_cache_key=use_cache_key,
        force_decrypt=force_decrypt,
        force_find_key=force_find_key,
        allow_manual_key_input=allow_manual_key_input,
        key_input_func=key_input_func,
    )