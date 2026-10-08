from typing import Literal
from pydantic import BaseModel, ConfigDict, model_validator


class InstructionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["normalized", "needs_clarification", "chat", "unsupported"]
    normalized_command: str | None
    clarification_question: str | None
    chat_reply: str | None

    @classmethod
    def model_json_schema(cls, **kwargs):
        # 将字段互斥也编码进生成约束，不能只依赖生成后的 Python 校验。
        branches = []
        for status, selected in (("normalized", "normalized_command"),
                                 ("needs_clarification", "clarification_question"),
                                 ("chat", "chat_reply"), ("unsupported", "chat_reply")):
            properties = {"status": {"const": status, "type": "string"}}
            for field in ("normalized_command", "clarification_question", "chat_reply"):
                properties[field] = {"type": "string", "minLength": 1} if field == selected else {"type": "null"}
            branches.append({"type": "object", "properties": properties,
                             "required": list(properties), "additionalProperties": False})
        return {"title": cls.__name__, "anyOf": branches}

    @model_validator(mode="after")
    def check_fields(self):
        selected = {"normalized": "normalized_command", "needs_clarification": "clarification_question",
                    "chat": "chat_reply", "unsupported": "chat_reply"}[self.status]
        for field in ("normalized_command", "clarification_question", "chat_reply"):
            value = getattr(self, field)
            if field == selected:
                if not value or not value.strip():
                    raise ValueError("缺少状态对应内容")
            elif value is not None:
                raise ValueError("状态对应字段必须互斥")
        return self
