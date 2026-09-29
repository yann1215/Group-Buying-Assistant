"""会话文件归档。调用方在事务上下文内删除数据库记录，失败则恢复文件。"""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from app.core import path_manager as paths
from app.core.order_version_manager import ORDER_SLOTS
from app.analysis.product_config import product_config_owner_path


class ArchiveError(RuntimeError):
    """归档失败；数据库和内存会话应当保留。"""


@dataclass(frozen=True)
class FileTransfer:
    source: Path
    destination: Path
    move: bool = True


def _unique_destination(directory: Path, name: str, reserved: set[Path]) -> Path:
    original = directory / name
    candidate = original
    index = 2
    while candidate.exists() or candidate in reserved:
        candidate = original.with_name(f"{original.stem}_{index}{original.suffix}")
        index += 1
    reserved.add(candidate)
    return candidate


@contextmanager
def _file_transaction(transfers: list[FileTransfer], workspace: Path | None = None) -> Iterator[None]:
    """先完整复制，再移除需移动的源文件；异常时逆序恢复，不覆盖已有文件。

    workspace 的临时回滚副本仅位于 temp，成功后清理，不进入 archive。
    """
    created: list[FileTransfer] = []
    removed: list[FileTransfer] = []
    backup_root: Path | None = None
    backup: Path | None = None
    workspace_touched = False
    try:
        for item in transfers:
            if item.source.resolve() == item.destination.resolve():
                continue
            if not item.source.is_file():
                raise FileNotFoundError(f"归档源不是文件：{item.source}")
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            # 独占占位，禁止静默覆盖（也防止规划后出现的目标文件被覆盖）。
            with item.destination.open("xb"):
                pass
            created.append(item)
            shutil.copy2(item.source, item.destination)

        if workspace is not None and workspace.exists():
            paths.TEMP_DIR.mkdir(parents=True, exist_ok=True)
            backup_root = Path(tempfile.mkdtemp(prefix="archive-rollback-", dir=paths.TEMP_DIR))
            backup = backup_root / "workspace"
            shutil.copytree(workspace, backup)

        for item in created:
            if item.move:
                item.source.unlink()
                removed.append(item)

        if backup is not None:
            workspace_touched = True
            # workspace 由路径管理器生成，并已验证解析后仍位于 WORKSPACE_DIR。
            shutil.rmtree(workspace)
        yield
    except Exception as exc:
        rollback_errors: list[str] = []
        for item in reversed(removed):
            try:
                if item.source.exists():
                    raise FileExistsError(f"恢复位置已被占用：{item.source}")
                shutil.move(str(item.destination), str(item.source))
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if workspace_touched and backup is not None:
            try:
                shutil.copytree(backup, workspace, dirs_exist_ok=True)
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        # 回滚不完整时保留所有恢复副本，明确给出位置供重试或人工恢复。
        if not rollback_errors:
            for item in reversed(created):
                try:
                    item.destination.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    rollback_errors.append(str(cleanup_exc))
        if rollback_errors:
            backup_root = None  # 保留 temp 中的恢复副本
            locations = ", ".join(str(item.destination) for item in created)
            raise ArchiveError(
                f"文件归档失败：{exc}；回滚未完成：{'; '.join(rollback_errors)}。"
                f"请保留归档及 temp 恢复副本：{locations}"
            ) from exc
        raise ArchiveError(f"文件归档失败，已恢复原文件，对话未删除：{exc}") from exc
    finally:
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)


@contextmanager
def archive_conversation_files(
    session_id: int,
    group_name: str | None,
    context: Mapping[str, Any],
) -> Iterator[Path]:
    """归档四槽订单、当前商品配置及精确匹配的结果，清理工作目录。

    不访问数据库。调用方必须在 with 块内删除 session；数据库删除失败
    同样触发文件回滚。缺失文件忽略，其余 I/O 错误均阻止删除会话。
    """
    archive_dir = paths.get_archive_dir(group_name, session_id)
    workspace = paths.get_workspace_dir(session_id, create=False)
    reserved: set[Path] = set()
    seen: set[Path] = set()
    transfers: list[FileTransfer] = []

    def collect(value: str | Path | None, category: str, move: bool) -> None:
        if not value:
            return
        source = Path(value).expanduser().resolve()
        if source in seen or not source.exists():
            return
        seen.add(source)
        destination = _unique_destination(archive_dir / category, source.name, reserved)
        transfers.append(FileTransfer(source, destination, move))

    for file_field, _ in ORDER_SLOTS:
        value = context.get(file_field)
        if value:
            collect(value, "input", paths.is_within(value, paths.ORDER_INPUT_DIR))

    if group_name:
        collect(paths.get_product_config_path(group_name), "config", True)
        collect(product_config_owner_path(paths.get_product_config_path(group_name)), "config", True)
    collect(context.get("share_config_file"), "config", True)
    if context.get("share_config_file"):
        collect(product_config_owner_path(context["share_config_file"]), "config", True)
    for output in paths.get_group_output_files(group_name):
        collect(output, "output", True)

    for category in ("input", "config", "output"):
        directory = archive_dir / category
        if not paths.is_within(directory, paths.ORDER_ARCHIVE_DIR):
            raise ArchiveError(f"归档目录超出 orders/archive：{directory}")
        directory.mkdir(parents=True, exist_ok=True)
    with _file_transaction(transfers, workspace):
        yield archive_dir


def rename_conversation_files(
    old_group_name: str | None,
    new_group_name: str,
    current_config_file: str | None,
) -> str | None:
    """群名改变时同时重命名配置和结果；任一冲突都保留原文件。"""
    new_config = paths.get_product_config_path(new_group_name)
    old_config = Path(current_config_file) if current_config_file else (
        paths.get_product_config_path(old_group_name) if old_group_name else None
    )
    transfers: list[FileTransfer] = []
    if old_config is not None and old_config.exists():
        transfers.append(FileTransfer(old_config, new_config))
    if old_config is not None and product_config_owner_path(old_config).exists():
        transfers.append(FileTransfer(product_config_owner_path(old_config), product_config_owner_path(new_config)))
    old_safe = paths.sanitize_filename(old_group_name) if old_group_name else ""
    new_safe = paths.sanitize_filename(new_group_name)
    for output in paths.get_group_output_files(old_group_name):
        transfers.append(FileTransfer(output, output.with_name(new_safe + output.name[len(old_safe):])))
    with _file_transaction(transfers):
        pass
    return str(new_config) if new_config.exists() else None
