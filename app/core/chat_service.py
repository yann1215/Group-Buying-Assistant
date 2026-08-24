# app/core/chat_service.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.core.intent_parser import parse_user_intent
from app.core.order_version_manager import (
    ORDER_VERSION_FIELDS,
    OrderVersionUpdateResult,
    shift_order_versions,
)
from app.core.tool_orchestrator import ToolOrchestrator
from app.database.repositories import (
    add_message,
    create_session,
    delete_session,
    get_order_versions,
    get_messages,
    get_session,
    list_sessions,
    load_session_context,
    save_session_context,
    touch_session,
    update_order_versions,
    update_session,
)
from app.llm.ollama_client import OllamaClient


ORDER_SLOT_LABELS = {
    "new_order_file": "新订单",
    "old_order_file": "旧订单",
    "order_cache_1_file": "缓存1",
    "order_cache_2_file": "缓存2",
}


class ChatService:

    def __init__(
        self,
        key_input_func: Callable[[str], str] | None = None,
    ) -> None:
        self.llm = OllamaClient()

        self.tools = ToolOrchestrator(
            key_input_func=key_input_func,
        )

    def create_conversation(
        self,
        title: str = "新对话",
        group_name: str | None = None,
    ) -> int:
        session_id = create_session(
            title=title,
            group_name=group_name,
        )
        self.tools.load_context(
            session_id,
            {"group_name": group_name},
        )
        self.save_working_context(session_id)
        self._discard_deleted_contexts()
        return session_id

    def list_conversations(self) -> list[dict[str, Any]]:
        return list_sessions()

    def load_conversation(
        self,
        session_id: int,
    ) -> list[dict[str, Any]]:
        if get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")

        context_data = load_session_context(session_id)
        self.tools.load_context(session_id, context_data)
        self._sync_new_order_to_tools(session_id, context_data)
        touch_session(session_id)
        return get_messages(session_id)

    def delete_conversation(self, session_id: int) -> bool:
        deleted = delete_session(session_id)
        if deleted:
            self.tools.remove_context(session_id)
        return deleted

    def save_working_context(self, session_id: int) -> None:
        session = get_session(session_id)
        if session is None:
            raise ValueError(f"会话不存在：{session_id}")

        self._ensure_context_loaded(session_id)
        context_data = self.tools.get_context_data(session_id)

        save_session_context(session_id, context_data)

        group_name = context_data.get("group_name")
        if group_name and (
            group_name != session.get("group_name")
            or session.get("title") == "新对话"
        ):
            # 群名称第一次确定或发生变化时同步标题。
            # 同一群之后可以单独修改 title，不会被每轮保存覆盖。
            update_session(
                session_id,
                title=str(group_name),
                group_name=str(group_name),
            )

    def set_working_context(
        self,
        session_id: int,
        group_name: str | None = None,
        order_input: str | Path | None = None,
        order_output_dir: str | Path | None = None,
    ) -> str | None:
        """
        设置当前会话的工作上下文。

        例如：
            - 当前处理哪个微信群
            - 当前订单文件路径
            - 输出目录
        """
        self._ensure_context_loaded(session_id)

        order_message: str | None = None
        if order_input is not None:
            result = self._update_order_versions(
                session_id,
                order_input,
            )
            order_message = self._format_order_update_result(result)
            if not result.success:
                return order_message

        self.tools.set_context(
            session_id=session_id,
            group_name=group_name,
            order_output_dir=order_output_dir,
        )
        self.save_working_context(session_id)
        return order_message

    def get_processing_message(
            self,
            user_text: str,
    ) -> str | None:
        """
        根据用户指令返回任务开始前显示的临时提示。

        这里只返回文本，不写入数据库。
        """
        intent = parse_user_intent(user_text)
        intent_name = intent.get("intent")

        processing_messages = {
            "member_check": "正在核对成员……",
            "calculate_share": "正在计算均摊……",
            "calculate_bulk_goods": "正在计算大货……",
            "update_share_config": "正在更新均摊配置……",
            "confirm_share_config": "正在确认均摊配置……",
            "update_special_members": "正在更新特殊成员信息……",
        }

        return processing_messages.get(intent_name)

    def send_message(self, session_id: int, user_text: str) -> str:
        self._ensure_context_loaded(session_id)
        add_message(
            session_id=session_id,
            role="user",
            content=user_text,
        )

        intent = parse_user_intent(user_text)
        order_input = intent.get("order_input")
        if order_input:
            result = self._update_order_versions(
                session_id,
                order_input,
            )
            order_message = self._format_order_update_result(result)

            # 无效输入不能进入编排器，否则旧逻辑会把不存在的路径
            # 写入当前工作上下文。
            if not result.success:
                self.save_working_context(session_id)
                add_message(
                    session_id=session_id,
                    role="assistant",
                    content=order_message,
                )
                return order_message

            # 单独设置订单时直接返回四个版本；若同一句还要求查成员、
            # 均摊或大货，则继续执行该业务，并使用刚更新的新订单。
            if intent.get("intent") == "set_context":
                self.tools.update_context_from_intent(
                    self.tools.get_context(session_id),
                    {
                        **intent,
                        "order_input": None,
                    },
                )
                self.save_working_context(session_id)
                add_message(
                    session_id=session_id,
                    role="assistant",
                    content=order_message,
                )
                return order_message

        try:
            tool_result = self.tools.handle(
                session_id=session_id,
                user_text=user_text,
            )
        finally:
            # 即使工具执行过程中报错，也保留本轮已经解析出的有效上下文。
            self.save_working_context(session_id)

        if tool_result is not None:
            add_message(
                session_id=session_id,
                role="assistant",
                content=tool_result,
            )
            return tool_result

        history = get_messages(session_id)

        llm_messages = [
            {
                "role": msg["role"],
                "content": msg["content"],
            }
            for msg in history
            if msg["role"] in ("user", "assistant", "system")
        ]

        assistant_text = self.llm.chat(llm_messages)

        add_message(
            session_id=session_id,
            role="assistant",
            content=assistant_text,
        )

        return assistant_text

    def _ensure_context_loaded(self, session_id: int) -> None:
        if session_id in self.tools.contexts:
            return

        if get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")

        self.tools.load_context(
            session_id,
            context_data := load_session_context(session_id),
        )
        self._sync_new_order_to_tools(session_id, context_data)

    def _discard_deleted_contexts(self) -> None:
        existing_ids = {
            int(session["id"])
            for session in list_sessions()
        }

        for session_id in list(self.tools.contexts):
            if session_id not in existing_ids:
                self.tools.remove_context(session_id)

    def _update_order_versions(
        self,
        session_id: int,
        order_input: str | Path,
    ) -> OrderVersionUpdateResult:
        result = shift_order_versions(
            get_order_versions(session_id),
            order_input,
        )

        # 即使新输入无效，也要保存本次发现的失效历史路径清理结果。
        if result.changed:
            saved = update_order_versions(
                session_id,
                **{
                    field: result.versions[field]
                    for field in ORDER_VERSION_FIELDS
                },
            )
            if not saved:
                raise ValueError(f"会话不存在：{session_id}")

        self._sync_new_order_to_tools(session_id, result.versions)
        return result

    def _sync_new_order_to_tools(
        self,
        session_id: int,
        versions: dict[str, Any],
    ) -> None:
        """
        将数据库中的四个订单版本同步到运行时工具上下文。

        只有新订单变化时才清除依赖订单内容的运行时缓存；旧订单和缓存
        版本只用于历史比较，不影响当前成员检查、均摊或大货计算。
        """
        ctx = self.tools.get_context(session_id)
        old_new_order = str(ctx.new_order_file or "")

        for field_name in ORDER_VERSION_FIELDS:
            value = str(versions.get(field_name) or "").strip()
            setattr(ctx, field_name, value or None)

        if old_new_order != str(ctx.new_order_file or ""):
            ctx.member_checked = False
            ctx.member_check_result = None
            ctx.parsed_order_file = None
            ctx.share_config_file = None
            ctx.product_configs = None
            ctx.bulk_request.pending_confirmation = False
            ctx.bulk_request.confirmed = False

    @staticmethod
    def _format_order_update_result(
        result: OrderVersionUpdateResult,
    ) -> str:
        lines: list[str] = []

        if result.success:
            if result.duplicate_input:
                lines.append("当前新订单已经是该文件，订单版本没有发生变化。")
            else:
                lines.append("订单更新成功。")
        else:
            lines.extend(
                [
                    f"订单输入错误：{result.error}。",
                    f"已检查：{result.input_path or '未提供有效路径'}",
                    "现有有效订单版本没有移动。",
                ]
            )

        if result.removed_paths:
            lines.append("")
            lines.append("已清除无法使用或重复的历史订单：")
            for removed in result.removed_paths:
                label = ORDER_SLOT_LABELS.get(
                    removed.file_field,
                    removed.file_field,
                )
                lines.append(
                    f"- {label}：{removed.file_path}（{removed.reason}）"
                )

        lines.append("")
        for field_name, label in ORDER_SLOT_LABELS.items():
            lines.append(
                f"{label}：{result.versions.get(field_name) or '未设置'}"
            )

        if result.success:
            lines.extend(
                [
                    "",
                    "查成员、均摊和大货计算将使用新订单。",
                ]
            )

        return "\n".join(lines)