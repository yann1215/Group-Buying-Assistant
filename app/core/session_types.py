"""会话类型及共用信息；业务上下文分别保存。"""
from dataclasses import dataclass, field
from typing import Any, ClassVar

SINGLE_CAR = "single_car"
MERGED_SHIPPING = "merged_shipping"
SESSION_TYPE_LABELS = {SINGLE_CAR: "单车会话", MERGED_SHIPPING: "合发会话"}


def validate_session_type(value: str) -> str:
    if value not in SESSION_TYPE_LABELS:
        raise ValueError(f"不支持的会话类型：{value}")
    return value


@dataclass
class ConversationContext:
    session_id: int | None = None
    conversation_title_override: str | None = None
    legacy_context: dict[str, Any] | None = None
    migration_needs_review: bool = False
    session_type: ClassVar[str]

    def common_data(self) -> dict[str, Any]:
        return {
            "context_version": 2,
            "session_type": self.session_type,
            "conversation_title_override": self.conversation_title_override,
            "legacy_context": self.legacy_context,
            "migration_needs_review": self.migration_needs_review,
        }


@dataclass
class MergedShippingContext(ConversationContext):
    session_type: ClassVar[str] = MERGED_SHIPPING
    merge_groups: list[str] = field(default_factory=list)
    merge_source_ids: list[int | None] = field(default_factory=list)
    merge_title: str | None = None
    merge_output_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.common_data(), merge_groups=list(self.merge_groups),
                    merge_source_ids=list(self.merge_source_ids), merge_title=self.merge_title,
                    merge_output_files=list(self.merge_output_files))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MergedShippingContext":
        def strings(key):
            value = data.get(key)
            return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

        ids = data.get("merge_source_ids")
        return cls(
            conversation_title_override=data.get("conversation_title_override"),
            legacy_context=data.get("legacy_context"),
            migration_needs_review=data.get("migration_needs_review") is True,
            merge_groups=strings("merge_groups"),
            merge_source_ids=[item if type(item) is int and item > 0 else None for item in ids]
            if isinstance(ids, list) else [],
            merge_title=data.get("merge_title"), merge_output_files=strings("merge_output_files"),
        )
