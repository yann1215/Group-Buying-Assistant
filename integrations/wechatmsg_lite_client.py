from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WECHATMSG_LITE_ROOT = PROJECT_ROOT / "external" / "WeChatMsg_Lite"


def _ensure_wechatmsg_lite_path() -> None:
    if not WECHATMSG_LITE_ROOT.exists():
        raise FileNotFoundError(
            f"未找到 WeChatMsg_Lite：{WECHATMSG_LITE_ROOT}"
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
) -> dict[str, Any]:
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
) -> dict[str, Any]:
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
    )