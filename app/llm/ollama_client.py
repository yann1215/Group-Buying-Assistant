# app/llm/ollama_client.py

import ollama

from app.config import DEFAULT_MODEL


class OllamaClient:
    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    def chat(self, messages: list[dict]) -> str:
        response = ollama.chat(
            model=self.model,
            messages=messages
        )

        return response["message"]["content"]