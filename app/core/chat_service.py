# app/core/chat_service.py

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable

from app.core.intent_parser import parse_user_intent, MERGE_INTENTS
from app.core.session_types import SINGLE_CAR, MERGED_SHIPPING, UNCLASSIFIED, MergedShippingContext, validate_session_type
from app.core.order_version_manager import (
    ORDER_VERSION_FIELDS,
    OrderVersionUpdateResult,
    shift_order_versions,
    update_order_entries,
)
from app.core.tool_orchestrator import ToolOrchestrator, SessionToolContext, invalidate_share_confirmation
from app.core.archive_manager import archive_conversation_files
from app.core.path_manager import sanitize_filename, format_order_path

from app.database.repositories import (
    MAX_SESSION_COUNT,
    add_message,
    create_session,
    classify_session,
    delete_session,
    find_session_by_group_name,
    get_order_versions,
    get_messages,
    get_session,
    list_sessions,
    load_session_context,
    load_session_draft,
    save_session_draft,
    save_session_context,
    # touch_session,
    update_order_versions,
    update_session,
)
from app.llm.llama_client import LlamaClient
from app.llm.instruction_normalizer import InstructionNormalizer
from app.llm.transfer_analyzer import TransferAnalyzer


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
        self.llm = LlamaClient()
        self.instruction_normalizer = InstructionNormalizer(self.llm)

        self.tools = ToolOrchestrator(
            key_input_func=key_input_func,
        )
        self.tools.transfer_analysis_client = TransferAnalyzer(self.llm)

    def create_conversation(
        self,
        title: str = "新对话",
        group_name: str | None = None,
        session_type: str = UNCLASSIFIED,
    ) -> int:
        validate_session_type(session_type)
        if session_type == MERGED_SHIPPING and group_name:
            raise ValueError("合发会话不能设置单车群聊名称")
        if session_type == UNCLASSIFIED and group_name and str(group_name).strip():
            session_type = SINGLE_CAR
        if group_name:
            if self._check_group_name_conflict(-1, group_name) is not None:
                raise ValueError(f"群聊名称已被其他对话使用：{group_name}")
        # 保持最多 30 个对话，但淘汰旧对话也必须走完整归档流程。
        sessions = list_sessions(limit=None)
        for session in reversed(sessions[MAX_SESSION_COUNT - 1:]):
            self.delete_conversation(int(session["id"]))
        session_id = create_session(
            title=("新合发会话" if title == "新对话" and session_type == MERGED_SHIPPING else title),
            group_name=group_name,
            session_type=session_type,
        )
        self.tools.load_context(
            session_id,
            {"group_name": group_name, "session_type": session_type},
        )
        self.save_working_context(session_id)
        self._discard_deleted_contexts()
        return session_id

    def list_conversations(self) -> list[dict[str, Any]]:
        return list_sessions()

    def load_conversation_draft(self, session_id: int) -> str:
        return load_session_draft(session_id)

    def save_conversation_draft(self, session_id: int, text: str) -> None:
        save_session_draft(session_id, text)

    def load_conversation(
        self,
        session_id: int,
    ) -> list[dict[str, Any]]:
        if get_session(session_id) is None:
            raise ValueError(f"会话不存在：{session_id}")

        context_data = load_session_context(session_id)
        self.tools.load_context(session_id, context_data)
        self._sync_new_order_to_tools(session_id, context_data)
        if context_data.get("session_type") == SINGLE_CAR and not context_data.get("config_owner_id"):
            # 首次迁移必须立即保存归属，避免再次打开旧对话时生成另一标识。
            self.save_working_context(session_id)
        # touch_session(session_id)
        return get_messages(session_id)

    def delete_conversation(
            self,
            session_id: int,
    ) -> bool:
        session = get_session(session_id)
        if session is None:
            raise ValueError(f"会话不存在：{session_id}")
        context = load_session_context(session_id)
        if session_id in self.tools.contexts:
            context.update(self.tools.get_context_data(session_id))
        context.update(get_order_versions(session_id))
        group_name = context.get("group_name") or session.get("group_name")
        with archive_conversation_files(session_id, group_name, context):
            if not delete_session(session_id):
                raise RuntimeError(f"删除会话记录失败：{session_id}")
        self.tools.remove_context(session_id)
        return True

    def save_working_context(self, session_id: int) -> None:
        session = get_session(session_id)
        if session is None:
            raise ValueError(f"会话不存在：{session_id}")

        self._ensure_context_loaded(session_id)
        context_data = self.tools.get_context_data(session_id)

        save_session_context(session_id, context_data)

        group_name = context_data.get("group_name")
        preferred_title = context_data.get("conversation_title_override") or context_data.get("merge_title")
        if group_name and (
            group_name != session.get("group_name")
            or session.get("title") == "新对话"
        ):
            # 群名称第一次确定或发生变化时同步标题。
            # 同一群之后可以单独修改 title，不会被每轮保存覆盖。
            update_session(
                session_id,
                title=preferred_title or str(group_name),
                group_name=str(group_name),
            )
        elif preferred_title and preferred_title != session.get("title"):
            update_session(session_id, title=preferred_title)

    def set_working_context(
            self,
            session_id: int,
            group_name: str | None = None,
            order_input: str | Path | None = None,
    ) -> str | None:

        self._ensure_context_loaded(session_id)
        if self.tools.get_context(session_id).session_type == UNCLASSIFIED:
            early_reply, messages, _ = self._initialize_business(
                session_id, {"intent": "set_context", "group_name": group_name, "order_input": order_input})
            return early_reply or "\n\n".join(messages) or None
        if self.tools.get_context(session_id).session_type != SINGLE_CAR:
            raise ValueError("合发会话不支持设置单车群名或订单，请进入单车会话操作")

        messages: list[str] = []

        # ---------------------------------
        # 群聊名称独立处理
        # ---------------------------------
        if group_name is not None:
            normalized_group_name = str(
                group_name
            ).strip()

            if normalized_group_name:
                conflict_session = (
                    self._check_group_name_conflict(
                        session_id,
                        normalized_group_name,
                    )
                )

                if conflict_session is not None:
                    messages.append(
                        "群聊名称重名。\n"
                        f"“{normalized_group_name}”"
                        "已经被其他对话使用，"
                        "当前车的群聊名称未修改。"
                    )
                else:
                    self.tools.set_context(
                        session_id=session_id,
                        group_name=normalized_group_name,
                    )

                    messages.append(
                        f"群聊名称已更新："
                        f"{normalized_group_name}"
                    )

        # ---------------------------------
        # 订单独立处理
        # ---------------------------------
        if order_input is not None:
            result = self._update_order_versions(
                session_id,
                order_input,
            )

            messages.append(
                self._format_order_update_result(
                    result
                )
            )

        # 无论其中哪个字段失败，
        # 已经成功更新的字段都保存
        self.save_working_context(session_id)

        return (
            "\n\n".join(messages)
            if messages
            else None
        )

    def send_message(
            self,
            session_id: int,
            user_text: str,
            progress_callback: Callable[[str], None] | None = None,
    ) -> str:

        self._ensure_context_loaded(session_id)
        add_message(
            session_id=session_id,
            role="user",
            content=user_text,
        )

        intent = parse_user_intent(user_text, self.tools.get_context(session_id).session_type)
        ctx = self.tools.get_context(session_id)
        waiting = {
            "transfer_analysis": bool(getattr(ctx, "pending_transfer_analysis", None)),
            "order_comparison": bool(getattr(ctx, "pending_order_comparison", None)),
            "participation": bool(getattr(ctx, "pending_participation", None)),
            "share": bool(getattr(getattr(ctx, "share_request", None), "pending_config_confirmation", False)),
            "bulk": bool(getattr(getattr(ctx, "bulk_request", None), "pending_confirmation", False)),
        }
        # 精确短回复由已有状态机消费，不能在模型调用前改变等待状态。
        short_reply = bool(any(waiting.values()) and re.fullmatch(
            r"(?:确认分析|确认|是|yes|y|1|对|无误|没问题|没有问题|算|计算|算吧|继续|继续算|下一步|好|好的|取消|否|不是|不|不要|不对|不正确|先别改|不要改|暂不修改|选择\s*\d+|\d+)",
            user_text.strip(), re.I))
        inquiry = bool(re.search(r"怎么|如何|什么意思|为什么", user_text))
        clauses = [s.strip() for s in re.split(r"[，,；;\n]+", user_text) if s.strip()]
        partial = any(
            parse_user_intent(clause, ctx.session_type)["intent"] == "chat"
            and re.search(r"帮|请|看看|检查|别|不要|取消|改|删|计算|提取|弄", clause)
            for clause in clauses
        )
        chat_reply = None
        if not short_reply and (intent["intent"] == "chat" or inquiry or partial):
            if progress_callback:
                progress_callback("正在理解指令……")
            try:
                result = self.instruction_normalizer.normalize(user_text, {
                    "session_type": ctx.session_type, "group_name": getattr(ctx, "group_name", None),
                    "waiting": waiting,
                    "recent_messages": [{"role": m["role"], "content": m["content"][:1200]}
                                        for m in get_messages(session_id)[-5:-1]],
                })
                if result.status == "normalized":
                    effective_text = result.normalized_command.strip()
                    normalized_intent = parse_user_intent(effective_text, ctx.session_type)
                    if normalized_intent["intent"] == "chat":
                        raise ValueError("规范化指令尚未被识别，请使用更明确的指令。")
                    # 问句不能被升级为写入/执行；否定和取消也不能被模型丢弃。
                    read_only = {"show_orders", "show_share", "show_special_members", "show_merge_groups"}
                    if inquiry and normalized_intent["intent"] not in read_only:
                        raise ValueError("这条消息包含咨询或疑问，请明确要执行的操作。")
                    if re.search(r"不要|别|取消|暂不|不想|不用", user_text) and not re.search(
                            r"不要|别|取消|暂不|不想|不用|不参摊|查看", effective_text):
                        raise ValueError("无法可靠保留原指令的否定语义，请明确要取消或查看的操作。")
                    confirmation = normalized_intent["intent"]
                    if confirmation == "confirm_transfer_analysis" and not waiting["transfer_analysis"]:
                        raise ValueError("当前没有等待确认的转单分析。")
                    if confirmation == "confirm_orders" and not waiting["order_comparison"]:
                        raise ValueError("当前没有等待确认的订单比对。")
                    if confirmation == "confirm_share_config" and not waiting["share"]:
                        raise ValueError("当前没有等待确认的均摊计算。")
                    user_text, intent = effective_text, normalized_intent
                else:
                    chat_reply = result.clarification_question or result.chat_reply
            except (RuntimeError, ValueError, OSError) as error:
                chat_reply = str(error)
            if chat_reply:
                add_message(session_id=session_id, role="assistant", content=chat_reply)
                return chat_reply
        if intent["intent"] == "unsupported":
            reply = intent["reply"]
            add_message(session_id=session_id, role="assistant", content=reply)
            return reply

        initial_input = self.tools.get_context(session_id).session_type == UNCLASSIFIED
        context_messages: list[str] = []
        context_has_error = False
        if initial_input:
            early_reply, context_messages, context_has_error = self._initialize_business(session_id, intent)
            if early_reply is not None:
                add_message(session_id=session_id, role="assistant", content=early_reply)
                return early_reply

        # =========================================================
        # 1. 独立处理群聊名称
        # =========================================================
        group_name = intent.get("group_name")

        if group_name and not initial_input:
            normalized_group_name = str(group_name).strip()

            conflict_session = self._check_group_name_conflict(
                session_id,
                normalized_group_name,
            )

            if conflict_session is not None:
                context_messages.append(
                    "群聊名称重名。\n"
                    f"“{normalized_group_name}”已经被其他对话使用，"
                    "当前车的群聊名称未修改。"
                )
                context_has_error = True

            else:
                # 群名合法，立即更新。
                # 不等待订单校验结果。
                self.tools.set_context(
                    session_id=session_id,
                    group_name=normalized_group_name,
                )

                # 立即持久化，因此后面的订单失败也不会影响群名
                self.save_working_context(session_id)

                context_messages.append(
                    f"群聊名称已更新：{normalized_group_name}"
                )

        # =========================================================
        # 2. 独立处理订单
        # =========================================================
        order_input = intent.get("order_entries") or intent.get("order_input")

        if order_input and not initial_input:
            result = self._update_order_versions(
                session_id,
                order_input,
            )

            order_message = self._format_order_update_result(
                result
            )

            context_messages.append(order_message)

            if not result.success:
                context_has_error = True

        # =========================================================
        # 3. 如果本句话只是录入上下文，统一返回结果
        # =========================================================
        if intent.get("intent") == "set_context":

            reply = "\n\n".join(context_messages)

            if not reply:
                reply = "当前信息没有发生变化。"

            add_message(
                session_id=session_id,
                role="assistant",
                content=reply,
            )

            return reply

        # =========================================================
        # 4. 如果还包含“查成员/算均摊/算大货”等操作
        #    但本轮明确输入的基础信息有错误，就先停止后续业务
        # =========================================================
        if context_has_error:
            self.save_working_context(session_id)

            reply = "\n\n".join(context_messages)

            add_message(
                session_id=session_id,
                role="assistant",
                content=reply,
            )

            return reply

        try:
            tool_result = self.tools.handle(
                session_id=session_id,
                user_text=user_text,
                progress_callback=progress_callback,
                parsed_intent=intent,
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

        assistant_text = "当前没有可继续执行的操作，请输入明确指令。"

        add_message(
            session_id=session_id,
            role="assistant",
            content=assistant_text,
        )

        return assistant_text

    def _initialize_business(self, session_id: int, intent: dict[str, Any]) -> tuple[str | None, list[str], bool]:
        """先校验首次输入，再一次提交类型和业务数据；失败保留未分类状态。"""
        current = self.tools.get_context(session_id)
        if current.session_type != UNCLASSIFIED:
            raise ValueError("仅未分类会话可以自动归类")
        common = current.to_dict()
        if intent["intent"] in MERGE_INTENTS:
            candidate = MergedShippingContext.from_dict(common)
            candidate.session_id = session_id
            reply = None
            if intent["intent"] == "set_merge_groups":
                from app.core.order_merge_workflow import handle_order_merge
                reply = handle_order_merge(self.tools, candidate, intent)
                if not candidate.merge_groups:
                    return reply, [], True
            classify_session(session_id, candidate.to_dict())
            self.tools.contexts[session_id] = candidate
            self.save_working_context(session_id)
            return reply, [], False

        group = str(intent.get("group_name") or "").strip()
        order_input = intent.get("order_entries") or intent.get("order_input")
        if not group and not order_input:
            return None, [], False
        messages = []
        has_error = False
        valid_group = None
        if group:
            try:
                conflict = self._check_group_name_conflict(session_id, group)
                if conflict is not None:
                    messages.append(f"群聊名称重名。\n“{group}”已经被其他对话使用，当前群聊名称未修改。")
                    has_error = True
                else:
                    valid_group = group
                    messages.append(f"群聊名称已更新：{group}")
            except ValueError as error:
                messages.append(f"群聊名称无效：{error}")
                has_error = True
        result = None
        if order_input:
            updater = update_order_entries if isinstance(order_input, list) else shift_order_versions
            result = updater(get_order_versions(session_id), order_input)
            messages.append(self._format_order_update_result(result))
            has_error = has_error or not result.success
        if not valid_group and not (result and result.success):
            return "\n\n".join(messages), messages, True
        candidate = SessionToolContext.from_dict(common)
        candidate.session_id = session_id
        candidate.group_name = valid_group
        if result and result.success:
            for name in ORDER_VERSION_FIELDS:
                setattr(candidate, name, result.versions[name] or None)
        classify_session(session_id, candidate.to_dict())
        self.tools.contexts[session_id] = candidate
        self.save_working_context(session_id)
        return None, messages, has_error

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

        if context_data.get("session_type") == SINGLE_CAR and not context_data.get("config_owner_id"):
            self.save_working_context(session_id)

    def _discard_deleted_contexts(self) -> None:
        existing_ids = {
            int(session["id"])
            for session in list_sessions()
        }

        for session_id in list(self.tools.contexts):
            if session_id not in existing_ids:
                self.tools.remove_context(session_id)

    def _check_group_name_conflict(
            self,
            session_id: int,
            group_name: str | None,
    ) -> dict[str, Any] | None:

        normalized_group_name = str(
            group_name or ""
        ).strip()

        if not normalized_group_name:
            return None

        conflict = find_session_by_group_name(
            normalized_group_name,
            exclude_session_id=session_id,
        )
        if conflict is not None:
            return conflict
        # Windows 大小写及非法字符替换后也不能让两车共用文件名。
        safe_name = sanitize_filename(normalized_group_name).casefold()
        for session in list_sessions(limit=None):
            if session["id"] != session_id and session.get("group_name"):
                if sanitize_filename(session["group_name"]).casefold() == safe_name:
                    return session
        return None

    def _update_order_versions(
        self,
        session_id: int,
        order_input: str | Path | list[dict[str, Any]],
    ) -> OrderVersionUpdateResult:
        if self.tools.get_context(session_id).session_type != SINGLE_CAR:
            raise ValueError("合发会话不能录入单车订单")
        updater = update_order_entries if isinstance(order_input, list) else shift_order_versions
        result = updater(get_order_versions(session_id), order_input)

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
        if ctx.session_type != SINGLE_CAR:
            return
        old_new_order = str(ctx.new_order_file or "")

        for field_name in ORDER_VERSION_FIELDS:
            value = str(versions.get(field_name) or "").strip()
            setattr(ctx, field_name, value or None)

        if old_new_order != str(ctx.new_order_file or ""):
            invalidate_share_confirmation(ctx)
            ctx.member_checked = False
            ctx.member_check_result = None
            ctx.parsed_order_file = None
            # 配置属于当前车的持久文件，换订单后仍保留其路径和手工设置。
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
                    f"已检查：{format_order_path(result.input_path, empty='未提供有效路径')}",
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
                    f"- {label}：{format_order_path(removed.file_path)}（{removed.reason}）"
                )

        lines.append("")
        for field_name, label in ORDER_SLOT_LABELS.items():
            lines.append(
                f"{label}：{format_order_path(result.versions.get(field_name))}"
            )

        if result.success:
            lines.extend(
                [
                    "",
                    "查成员、均摊和大货计算将使用新订单。",
                ]
            )

        return "\n".join(lines)
