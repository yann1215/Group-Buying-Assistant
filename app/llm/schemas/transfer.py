from typing import Literal
from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sender_order: str | None
    receiver_order: str | None
    product: str | None
    quantity: int | None = Field(ge=1)
    state: Literal["proposed", "confirmed", "cancelled", "uncertain"]
    message_refs: list[str] = Field(min_length=1)
    knowledge_refs: list[str] = Field(min_length=1)
    explanation: str

    @model_validator(mode="after")
    def confirmed_needs_both_messages(self):
        if self.state == "confirmed" and len(set(self.message_refs)) < 2:
            raise ValueError("已确认转单必须引用转出和接收的不同消息；资料不足应标 uncertain。")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["matched", "suspected", "unresolved"]
    summary: str
    message_refs: list[str]
    order_refs: list[str] = Field(min_length=1)
    knowledge_refs: list[str] = Field(min_length=1)


class TransferResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stage: Literal["extract", "review"]
    events: list[Event]
    findings: list[Finding]
    limitations: list[str]

    @classmethod
    def model_json_schema(cls, **kwargs):
        definition = super().model_json_schema(**kwargs)
        event = definition["$defs"]["Event"]
        event_branches = []
        for state in ("confirmed", "other"):
            branch = deepcopy(event)
            branch["properties"]["state"] = ({"type": "string", "const": "confirmed"} if state == "confirmed"
                                               else {"type": "string", "enum": ["proposed", "cancelled", "uncertain"]})
            if state == "confirmed":
                branch["properties"]["message_refs"].update(minItems=2, uniqueItems=True)
            # 先引用证据，再输出状态，减少小模型在未整理证据时提前选定状态。
            fields = branch["properties"]
            branch["properties"] = {name: fields[name] for name in
                                    ("message_refs", "sender_order", "receiver_order", "product", "quantity",
                                     "knowledge_refs", "explanation", "state")}
            event_branches.append(branch)
        definition["$defs"]["Event"] = {"anyOf": event_branches}
        finding = definition["$defs"]["Finding"]
        finding_branches = []
        for status in ("matched", "suspected", "unresolved"):
            branch = deepcopy(finding)
            branch["properties"]["status"] = {"type": "string", "const": status}
            if status != "unresolved":
                branch["properties"]["message_refs"]["minItems"] = 1
            finding_branches.append(branch)
        definition["$defs"]["Finding"] = {"anyOf": finding_branches}
        branches = []
        for stage, empty in (("extract", "findings"), ("review", "events")):
            branch = deepcopy({k: v for k, v in definition.items() if k != "$defs"})
            branch["properties"]["stage"] = {"type": "string", "const": stage}
            branch["properties"][empty]["maxItems"] = 0
            branches.append(branch)
        return {"title": cls.__name__, "$defs": definition.get("$defs", {}), "anyOf": branches}

    @classmethod
    def model_json_schema_for_payload(cls, payload):
        definition = cls.model_json_schema()
        # 阶段和引用集合由程序提供，生成时不能选择不存在的编号。
        definition["anyOf"] = [b for b in definition["anyOf"]
                               if b["properties"]["stage"]["const"] == payload["stage"]]
        empty, unused = ("findings", "Finding") if payload["stage"] == "extract" else ("events", "Event")
        definition["anyOf"][0]["properties"][empty] = {"type": "array", "items": {"type": "null"}, "maxItems": 0}
        definition["$defs"].pop(unused, None)
        identifiers = {
            "message_refs": [r["id"] for r in payload.get("messages", [])],
            "order_refs": [r["id"] for r in payload.get("differences", [])],
            "knowledge_refs": [r["id"] for r in payload["knowledge"]],
        }
        if not identifiers["message_refs"] and "Finding" in definition["$defs"]:
            definition["$defs"]["Finding"]["anyOf"] = [
                b for b in definition["$defs"]["Finding"]["anyOf"]
                if b["properties"]["status"]["const"] == "unresolved"]

        def constrain(node):
            if isinstance(node, dict):
                for name, field in node.get("properties", {}).items():
                    if name in identifiers:
                        if identifiers[name]:
                            field["items"] = {"type": "string", "enum": identifiers[name]}
                        else:
                            field.pop("minItems", None)
                            field["maxItems"] = 0
                for value in node.values():
                    constrain(value)
            elif isinstance(node, list):
                for value in node:
                    constrain(value)
        constrain(definition)
        return definition

    @model_validator(mode="after")
    def check_stage(self):
        if self.stage == "extract" and self.findings or self.stage == "review" and self.events:
            raise ValueError("分析阶段的输出字段不匹配")
        return self
