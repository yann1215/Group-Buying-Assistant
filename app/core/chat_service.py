# app/core/chat_service.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.database.repositories import (
    add_message,
    create_session,
    delete_session,
    get_messages,
    get_session,
    list_sessions,
    load_session_context,
    save_session_context,
    touch_session,
    update_session,
)
from app.llm.ollama_client import OllamaClient
from app.core.tool_orchestrator import ToolOrchestrator


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
        order_input: str | Path | dict[str, Any] | None = None,
        order_output_dir: str | Path | None = None,
    ) -> None:
        """
        设置当前会话的工作上下文。

        例如：
            - 当前处理哪个微信群
            - 当前订单文件路径
            - 输出目录
        """
        self._ensure_context_loaded(session_id)
        self.tools.set_context(
            session_id=session_id,
            group_name=group_name,
            order_input=order_input,
            order_output_dir=order_output_dir,
        )
        self.save_working_context(session_id)

    def send_message(self, session_id: int, user_text: str) -> str:
        self._ensure_context_loaded(session_id)
        add_message(
            session_id=session_id,
            role="user",
            content=user_text,
        )

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
            load_session_context(session_id),
        )

    def _discard_deleted_contexts(self) -> None:
        existing_ids = {
            int(session["id"])
            for session in list_sessions()
        }

        for session_id in list(self.tools.contexts):
            if session_id not in existing_ids:
                self.tools.remove_context(session_id)