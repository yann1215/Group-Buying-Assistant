from app.config import get_resource_path, INSTRUCTION_MAX_TOKENS
from app.llm.schemas.instruction import InstructionResult


class InstructionNormalizer:
    def __init__(self, client):
        self.client = client

    def normalize(self, text, context):
        if len(text) > 6000:
            raise ValueError("指令过长，请分开输入。")
        prompt = get_resource_path("app/llm/prompts/instruction.md").read_text(encoding="utf-8")
        return self.client.structured(prompt, {"text": text, "context": context}, InstructionResult,
                                      max_tokens=INSTRUCTION_MAX_TOKENS)
