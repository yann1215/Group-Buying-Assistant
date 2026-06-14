# app/core/chat_service.py

from app.database.repositories import add_message, get_messages
from app.llm.ollama_client import OllamaClient


class ChatService:
    def __init__(self):
        self.llm = OllamaClient()

    def send_message(self, session_id: int, user_text: str) -> str:
        add_message(
            session_id=session_id,
            role="user",
            content=user_text
        )

        history = get_messages(session_id)

        llm_messages = [
            {
                "role": msg["role"],
                "content": msg["content"]
            }
            for msg in history
            if msg["role"] in ("user", "assistant", "system")
        ]

        assistant_text = self.llm.chat(llm_messages)

        add_message(
            session_id=session_id,
            role="assistant",
            content=assistant_text
        )

        return assistant_text