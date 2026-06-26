# app/core/chat_service.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.database.repositories import add_message, get_messages
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
        self.tools.set_context(
            session_id=session_id,
            group_name=group_name,
            order_input=order_input,
            order_output_dir=order_output_dir,
        )

    def send_message(self, session_id: int, user_text: str) -> str:
        user_message_id = add_message(
            session_id=session_id,
            role="user",
            content=user_text,
        )

        tool_result = self.tools.handle(
            session_id=session_id,
            user_text=user_text,
        )

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