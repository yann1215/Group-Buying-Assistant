from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from app.core.path_manager import get_order_input_path


SUPPORTED_ORDER_SUFFIXES = {".xlsx"}

ORDER_SLOTS = (
    ("new_order_file", "new_order_updated_at"),
    ("old_order_file", "old_order_updated_at"),
    ("order_cache_1_file", "order_cache_1_updated_at"),
    ("order_cache_2_file", "order_cache_2_updated_at"),
)

ORDER_VERSION_FIELDS = tuple(
    field_name
    for file_field, updated_at_field in ORDER_SLOTS
    for field_name in (file_field, updated_at_field)
)


@dataclass(frozen=True)
class RemovedOrderPath:
    file_field: str
    file_path: str
    reason: str


@dataclass(frozen=True)
class OrderVersionUpdateResult:
    versions: dict[str, str]
    success: bool
    input_path: str = ""
    changed: bool = False
    duplicate_input: bool = False
    removed_paths: tuple[RemovedOrderPath, ...] = field(default_factory=tuple)
    error: str = ""


def empty_order_versions() -> dict[str, str]:
    """返回包含四个订单槽位及更新时间的空版本数据。"""
    return {field_name: "" for field_name in ORDER_VERSION_FIELDS}


def normalize_order_path(
    value: str | Path,
    *,
    default_order_dir: str | Path | None = None,
) -> str:
    """
    规范化用户输入的订单路径，但不检查文件是否存在。

    - 去除路径两侧空格和引号；
    - 没有后缀时补充 ``.xlsx``；
    - 只有文件名时放入 ``default_order_dir``；
    - 已含目录的相对路径和绝对路径保持原有形式。
    """
    return get_order_input_path(value, default_order_dir=default_order_dir)


def validate_order_path(value: str | Path) -> tuple[bool, str]:
    """检查路径是否为存在、可读且后缀受支持的订单文件。"""
    file_path = str(value).strip()
    if not file_path:
        return False, "订单路径为空"

    path = Path(file_path).expanduser()
    if path.suffix.lower() not in SUPPORTED_ORDER_SUFFIXES:
        return False, "订单文件仅支持 .xlsx"
    if not path.exists():
        return False, "找不到订单文件"
    if not path.is_file():
        return False, "订单路径不是文件"
    if not os.access(path, os.R_OK):
        return False, "订单文件不可读取"

    return True, ""


def clean_invalid_order_versions(
    versions: Mapping[str, Any] | None,
) -> tuple[dict[str, str], tuple[RemovedOrderPath, ...]]:
    """
    清除四个槽位中无效的订单路径，并同步清空对应更新时间。

    未知字段不会进入返回值，以保证结果可以直接交给数据库仓储层。
    """
    cleaned = _normalize_versions(versions)
    removed: list[RemovedOrderPath] = []

    for file_field, updated_at_field in ORDER_SLOTS:
        file_path = cleaned[file_field]
        if not file_path:
            cleaned[updated_at_field] = ""
            continue

        valid, reason = validate_order_path(file_path)
        if valid:
            continue

        removed.append(
            RemovedOrderPath(
                file_field=file_field,
                file_path=file_path,
                reason=reason,
            )
        )
        cleaned[file_field] = ""
        cleaned[updated_at_field] = ""

    return cleaned, tuple(removed)


def deduplicate_order_versions(
    versions: Mapping[str, Any] | None,
) -> tuple[dict[str, str], tuple[RemovedOrderPath, ...]]:
    """
    按新订单、旧订单、缓存1、缓存2的顺序去重。

    同一文件即使使用不同的相对路径表示，也只保留最靠前的版本。
    """
    deduplicated = _normalize_versions(versions)
    seen_paths: set[str] = set()
    removed: list[RemovedOrderPath] = []

    for file_field, updated_at_field in ORDER_SLOTS:
        file_path = deduplicated[file_field]
        if not file_path:
            deduplicated[updated_at_field] = ""
            continue

        identity = _path_identity(file_path)
        if identity not in seen_paths:
            seen_paths.add(identity)
            continue

        removed.append(
            RemovedOrderPath(
                file_field=file_field,
                file_path=file_path,
                reason="与较新的订单版本重复",
            )
        )
        deduplicated[file_field] = ""
        deduplicated[updated_at_field] = ""

    return deduplicated, tuple(removed)


def compact_order_versions(
    versions: Mapping[str, Any] | None,
) -> dict[str, str]:
    """
    将有效版本向前压紧，并保持每个路径与其更新时间成对移动。

    例如 ``新订单=A、旧订单为空、缓存1=B`` 会整理为
    ``新订单=A、旧订单=B、缓存1为空``。
    """
    normalized = _normalize_versions(versions)
    occupied_versions = [
        (normalized[file_field], normalized[updated_at_field])
        for file_field, updated_at_field in ORDER_SLOTS
        if normalized[file_field]
    ]

    compacted = empty_order_versions()
    for (file_field, updated_at_field), (file_path, updated_at) in zip(
        ORDER_SLOTS,
        occupied_versions,
    ):
        compacted[file_field] = file_path
        compacted[updated_at_field] = updated_at

    return compacted


def shift_order_versions(
    versions: Mapping[str, Any] | None,
    new_order_path: str | Path,
    *,
    updated_at: str | None = None,
    default_order_dir: str | Path | None = None,
) -> OrderVersionUpdateResult:
    """
    清理现有版本并将有效的新订单写入首位。

    顺移规则为 ``缓存2 ← 缓存1 ← 旧订单 ← 新订单``。输入路径无效时，
    不顺移现有有效版本，但返回已经完成失效清理和去重的版本数据。
    """
    original = _normalize_versions(versions)
    cleaned, invalid_paths = clean_invalid_order_versions(original)
    cleaned, duplicate_paths = deduplicate_order_versions(cleaned)
    cleaned = compact_order_versions(cleaned)
    removed_paths = invalid_paths + duplicate_paths

    normalized_input = normalize_order_path(
        new_order_path,
        default_order_dir=default_order_dir,
    )
    valid, error = validate_order_path(normalized_input)
    if not valid:
        return OrderVersionUpdateResult(
            versions=cleaned,
            success=False,
            input_path=normalized_input,
            changed=cleaned != original,
            removed_paths=removed_paths,
            error=error,
        )

    if (
        cleaned["new_order_file"]
        and _path_identity(cleaned["new_order_file"])
        == _path_identity(normalized_input)
    ):
        return OrderVersionUpdateResult(
            versions=cleaned,
            success=True,
            input_path=normalized_input,
            changed=cleaned != original,
            duplicate_input=True,
            removed_paths=removed_paths,
        )

    shifted = empty_order_versions()
    shifted["new_order_file"] = normalized_input
    shifted["new_order_updated_at"] = updated_at or _current_timestamp()

    source_slots = ORDER_SLOTS[:3]
    target_slots = ORDER_SLOTS[1:]
    for (source_file, source_time), (target_file, target_time) in zip(
        source_slots,
        target_slots,
    ):
        shifted[target_file] = cleaned[source_file]
        shifted[target_time] = cleaned[source_time]

    shifted, shifted_duplicates = deduplicate_order_versions(shifted)
    removed_paths += shifted_duplicates

    return OrderVersionUpdateResult(
        versions=shifted,
        success=True,
        input_path=normalized_input,
        changed=shifted != original,
        removed_paths=removed_paths,
    )


def _normalize_versions(
    versions: Mapping[str, Any] | None,
) -> dict[str, str]:
    normalized = empty_order_versions()
    if not versions:
        return normalized

    for field_name in ORDER_VERSION_FIELDS:
        value = versions.get(field_name, "")
        normalized[field_name] = "" if value is None else str(value).strip()

    return normalized


def _path_identity(value: str | Path) -> str:
    path = Path(value).expanduser().resolve(strict=False)
    return os.path.normcase(str(path))


def _current_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def update_order_entries(versions, entries, *, default_order_dir=None):
    """验证整组输入后一次更新，失败时保留原版本。"""
    original = _normalize_versions(versions)
    if len(entries) not in (1, 2):
        return OrderVersionUpdateResult(original, False, error="每次只支持一个或两个订单")
    resolved = []
    for entry in entries:
        path = normalize_order_path(entry["value"], default_order_dir=default_order_dir)
        attempted = [path]
        valid, error = validate_order_path(path)
        if not valid and error == "找不到订单文件" and entry.get("number") is not None:
            candidate = Path(path).with_name(f"{entry['number']} {Path(path).name}")
            path = str(candidate)
            attempted.append(path)
            valid, error = validate_order_path(path)
        if not valid:
            return OrderVersionUpdateResult(original, False, input_path="；".join(attempted), error=error)
        resolved.append((entry, path))
    if len(resolved) == 1:
        entry, path = resolved[0]
        if entry["label"] != "旧订单":
            return shift_order_versions(original, path)
        updated = dict(original)
        if original["new_order_file"] and _path_identity(original["new_order_file"]) == _path_identity(path):
            return OrderVersionUpdateResult(original, False, error="新旧订单不能是同一文件")
        updated["old_order_file"] = path
        updated["old_order_updated_at"] = _current_timestamp()
        updated, _ = deduplicate_order_versions(updated)
        return OrderVersionUpdateResult(updated, True, changed=updated != original)
    if _path_identity(resolved[0][1]) == _path_identity(resolved[1][1]):
        return OrderVersionUpdateResult(original, False, error="新旧订单不能是同一文件")
    labels = [entry["label"] for entry, _ in resolved]
    if any(label != "订单" for label in labels):
        if labels.count("新订单") > 1 or labels.count("旧订单") > 1:
            return OrderVersionUpdateResult(original, False, error="请指定不同的新旧订单标签")
        new_index = labels.index("新订单") if "新订单" in labels else 1 - labels.index("旧订单")
    elif all(entry.get("number") is not None for entry, _ in resolved):
        if resolved[0][0]["number"] == resolved[1][0]["number"]:
            return OrderVersionUpdateResult(original, False, error="订单编号重复")
        new_index = int(resolved[1][0]["number"] > resolved[0][0]["number"])
    else:
        new_index = 1
    updated = empty_order_versions()
    now = _current_timestamp()
    for slot, path in [("new", resolved[new_index][1]), ("old", resolved[1-new_index][1])]:
        updated[f"{slot}_order_file"] = path
        updated[f"{slot}_order_updated_at"] = now
    seen = {_path_identity(path) for _, path in resolved}
    history = []
    for file_field, time_field in ORDER_SLOTS:
        path = original[file_field]
        if path and validate_order_path(path)[0] and _path_identity(path) not in seen:
            history.append((path, original[time_field]))
            seen.add(_path_identity(path))
    for (file_field, time_field), (path, timestamp) in zip(ORDER_SLOTS[2:], history):
        updated[file_field], updated[time_field] = path, timestamp
    return OrderVersionUpdateResult(updated, True, changed=updated != original)
