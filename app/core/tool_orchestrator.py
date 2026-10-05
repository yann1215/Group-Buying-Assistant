# app/core/tool_orchestrator.py

"""

1. 判断当前 session 有没有群聊名称和订单文件
2. 录入均摊只保存参数；计算前先展示商品配置并等待确认
3. 确认后调用 parse_group_member_orders()，名单有问题则暂停
4. 校验配置未变化后计算；查看均摊只读取当前有效结果

"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from decimal import Decimal
import hashlib
import json
from uuid import uuid4

from app.analysis.order_parser import parse_order_file
from app.analysis.special_member import (
    SpecialMemberError,
    update_special_members_with_preview,
    validate_special_member_cache,
)
from app.analysis.member_parser import parse_group_member_orders
from app.analysis.special_member import (
    get_non_share_order_nos,
)
from app.analysis.share_calculator import calculate_share, normalize_share_type
from app.analysis.product_config import (
    ensure_product_config_file,
    load_product_share_config_file,
    update_product_share_config_file,
    update_product_config_before_share,
    update_product_config_after_share,
    update_product_config_before_bulk,
    claim_product_config,
    owns_product_config,
    reset_product_share_fields,
)
from app.analysis.bulk_calculator import (
    create_bulk_receivable_orders,
)
from app.core.intent_parser import (
    has_affirmative_words,
    has_negative_words,
    has_share_confirmation_words,
    parse_user_intent,
    is_intent_allowed,
    unsupported_intent_reply,
)
from app.core.session_types import ConversationContext, MergedShippingContext, SINGLE_CAR, MERGED_SHIPPING, validate_session_type
from app.core.path_manager import get_parsed_orders_path, get_product_config_path, format_order_path
from app.core.archive_manager import rename_conversation_files


def emit_progress(
    callback: Callable[[str], None] | None,
    message: str,
) -> None:
    if callback is not None:
        callback(message)


def build_bulk_product_price_preview(
    product_configs: list[dict[str, Any]] | None,
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[str],
    bool,
]:
    """
    生成大货确认阶段的商品价格检查信息。

    返回：
        valid_price_lines:
            有效商品单价展示

        zero_price_products:
            检测到单价为 0 的商品
            忽略专拍商品

        missing_price_products:
            未检测到有效单价的商品
            忽略专拍商品

        zero_quantity_products:
            商品数量为 0 的商品

        all_detected_prices_below_8:
            所有“检测到单价”的非专拍商品是否都 < 8 元

    说明：
        - 专拍商品不参与单价展示、0 元检查、缺失单价检查、
          <8 元判断。
        - 单价为 0 仍属于“检测到了单价”，因此参与 <8 元判断。
        - 单价为空、无法解析、负数，都视为“未检测到单价”。
    """

    valid_price_lines: list[str] = []
    zero_price_products: list[str] = []
    missing_price_products: list[str] = []
    zero_quantity_products: list[str] = []

    # 用于判断：
    # “所有检测到单价的商品是否都 < 8”
    detected_prices: list[Decimal] = []

    for item in product_configs or []:
        product_name = str(
            item.get("商品名称") or ""
        ).strip()

        if not product_name:
            continue

        # ---------------------------------
        # 1. 检查商品数量
        # ---------------------------------
        quantity = item.get("商品数量")

        try:
            quantity_number = int(quantity)
        except (TypeError, ValueError):
            quantity_number = None

        if quantity_number == 0:
            zero_quantity_products.append(
                product_name
            )

        # ---------------------------------
        # 2. 专拍不参与价格相关检查
        # ---------------------------------
        if product_name.endswith("专拍"):
            continue

        # ---------------------------------
        # 3. 检查商品单价
        # ---------------------------------
        price_text = str(
            item.get("商品单价") or ""
        ).strip()

        if not price_text:
            missing_price_products.append(
                product_name
            )
            continue

        try:
            price = Decimal(price_text)
        except Exception:
            missing_price_products.append(
                product_name
            )
            continue

        if price < 0:
            missing_price_products.append(
                product_name
            )
            continue

        # 只要成功读到 >= 0 的价格，
        # 就属于“检测到单价”
        detected_prices.append(price)

        if price == 0:
            zero_price_products.append(
                product_name
            )
            continue

        # > 0 才属于正常展示的有效价格
        valid_price_lines.append(
            f"- {product_name}：{price:.2f} 元"
        )

    # 注意：
    # 缺失单价的商品不参与这个判断。
    #
    # 例如：
    # A = 5
    # B = 7
    # C = 未检测到
    #
    # 对“检测到单价”的 A/B 来说全部 < 8，
    # 因此仍然返回 True。
    all_detected_prices_below_8 = (
        bool(detected_prices)
        and all(
            price < Decimal("8")
            for price in detected_prices
        )
    )

    return (
        valid_price_lines,
        zero_price_products,
        missing_price_products,
        zero_quantity_products,
        all_detected_prices_below_8,
    )


@dataclass
class ShareRequestState:
    share_mode: str | None = None
    calculation_scope: str | None = None
    amount: str | None = None
    force: bool = False

    pending_config_confirmation: bool = False
    config_confirmed: bool = False
    confirmation_signature: str | None = None
    reset_product_amounts: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "share_mode": self.share_mode,
            "calculation_scope": self.calculation_scope,
            "amount": self.amount,
            "force": self.force,
            "pending_config_confirmation": self.pending_config_confirmation,
            "config_confirmed": self.config_confirmed,
            "confirmation_signature": self.confirmation_signature,
            "reset_product_amounts": self.reset_product_amounts,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ShareRequestState":
        if not isinstance(data, dict):
            return cls()

        return cls(
            share_mode=_optional_string(data.get("share_mode")),
            calculation_scope=_optional_string(
                data.get("calculation_scope")
            ),
            amount=_optional_string(data.get("amount")),
            force=data.get("force") is True,
            pending_config_confirmation=(
                data.get("pending_config_confirmation") is True
            ),
            config_confirmed=data.get("config_confirmed") is True,
            confirmation_signature=data.get("confirmation_signature"),
            reset_product_amounts=data.get("reset_product_amounts") is True,
        )


@dataclass
class BulkGoodsRequestState:
    pending_confirmation: bool = False
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_confirmation": self.pending_confirmation,
            "confirmed": self.confirmed,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "BulkGoodsRequestState":
        if not isinstance(data, dict):
            return cls()

        return cls(
            pending_confirmation=(
                data.get("pending_confirmation") is True
            ),
            confirmed=data.get("confirmed") is True,
        )


@dataclass
class SingleCarContext(ConversationContext):
    """单车业务上下文。"""
    session_type = SINGLE_CAR
    config_owner_id: str = field(default_factory=lambda: uuid4().hex)
    last_share_result: dict[str, Any] | None = None
    last_share_signature: str | None = None
    share_results_invalidated: bool = False
    legacy_share_signature: str | None = None
    group_name: str | None = None

    pending_order_comparison: dict[str, Any] | None = None
    pending_participation: dict[str, Any] | None = None

    special_members: list[dict[str, Any]] = field(
        default_factory=list
    )

    # 订单版本。现有业务统一使用 new_order_file，其余版本用于历史比较。
    new_order_file: str | None = None
    new_order_updated_at: str | None = None
    old_order_file: str | None = None
    old_order_updated_at: str | None = None
    order_cache_1_file: str | None = None
    order_cache_1_updated_at: str | None = None
    order_cache_2_file: str | None = None
    order_cache_2_updated_at: str | None = None

    # 新订单核对缓存
    member_checked: bool = False
    member_check_result: dict[str, Any] | None = None
    parsed_order_file: str | None = None

    share_config_file: str | None = None
    product_configs: list[dict[str, Any]] | None = None

    share_request: ShareRequestState = field(
        default_factory=ShareRequestState
    )

    bulk_request: BulkGoodsRequestState = field(
        default_factory=BulkGoodsRequestState
    )

    def to_dict(self) -> dict[str, Any]:
        """
        转换为可写入 JSON 的会话上下文。

        群成员核对结果和解析订单路径属于易过期缓存，
        不进行持久化。恢复会话后必须重新核对。
        """
        return {
            **self.common_data(),
            "pending_order_comparison": _to_json_safe(self.pending_order_comparison),
            "pending_participation": _to_json_safe(self.pending_participation),
            "config_owner_id": self.config_owner_id,
            "last_share_result": _to_json_safe(self.last_share_result),
            "last_share_signature": self.last_share_signature,
            "share_results_invalidated": self.share_results_invalidated,
            "legacy_share_signature": self.legacy_share_signature,
            "group_name": self.group_name,
            "special_members": _to_json_safe(self.special_members),
            "new_order_file": self.new_order_file,
            "new_order_updated_at": self.new_order_updated_at,
            "old_order_file": self.old_order_file,
            "old_order_updated_at": self.old_order_updated_at,
            "order_cache_1_file": self.order_cache_1_file,
            "order_cache_1_updated_at": self.order_cache_1_updated_at,
            "order_cache_2_file": self.order_cache_2_file,
            "order_cache_2_updated_at": self.order_cache_2_updated_at,
            "share_config_file": self.share_config_file,
            "product_configs": _to_json_safe(self.product_configs),
            "share_request": self.share_request.to_dict(),
            "bulk_request": self.bulk_request.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SessionToolContext":
        if not isinstance(data, dict):
            return cls()

        special_members = data.get("special_members")
        product_configs = data.get("product_configs")

        return cls(
            conversation_title_override=_optional_string(data.get("conversation_title_override")),
            legacy_context=data.get("legacy_context"),
            migration_needs_review=data.get("migration_needs_review") is True,
            pending_order_comparison=data.get("pending_order_comparison") if isinstance(data.get("pending_order_comparison"), dict) else None,
            pending_participation=data.get("pending_participation") if isinstance(data.get("pending_participation"), dict) else None,
            config_owner_id=str(data.get("config_owner_id") or uuid4().hex),
            last_share_result=data.get("last_share_result") if isinstance(data.get("last_share_result"), dict) else None,
            last_share_signature=data.get("last_share_signature"),
            share_results_invalidated=data.get("share_results_invalidated") is True,
            legacy_share_signature=data.get("legacy_share_signature"),
            group_name=_optional_string(data.get("group_name")),
            special_members=_dict_list_or_empty(special_members),
            new_order_file=_optional_string(data.get("new_order_file")),
            new_order_updated_at=_optional_string(
                data.get("new_order_updated_at")
            ),
            old_order_file=_optional_string(data.get("old_order_file")),
            old_order_updated_at=_optional_string(
                data.get("old_order_updated_at")
            ),
            order_cache_1_file=_optional_string(
                data.get("order_cache_1_file")
            ),
            order_cache_1_updated_at=_optional_string(
                data.get("order_cache_1_updated_at")
            ),
            order_cache_2_file=_optional_string(
                data.get("order_cache_2_file")
            ),
            order_cache_2_updated_at=_optional_string(
                data.get("order_cache_2_updated_at")
            ),

            # 核对状态始终使用默认值 False/None，避免恢复过期结果。
            member_checked=False,
            member_check_result=None,
            parsed_order_file=None,

            share_config_file=_optional_string(
                data.get("share_config_file")
            ),
            product_configs=(
                _dict_list_or_empty(product_configs)
                if isinstance(product_configs, list)
                else None
            ),
            share_request=ShareRequestState.from_dict(
                data.get("share_request")
            ),
            bulk_request=BulkGoodsRequestState.from_dict(
                data.get("bulk_request")
            ),
        )


# 兼容既有调用名称；该类型仅用于单车业务。
SessionToolContext = SingleCarContext


class ToolOrchestrator:

    def __init__(
        self,
        key_input_func: Callable[[str], str] | None = None,
    ) -> None:
        self.contexts: dict[int, SessionToolContext | MergedShippingContext] = {}

        self.key_input_func = key_input_func

    def get_context(self, session_id: int) -> SessionToolContext | MergedShippingContext:
        return self.contexts.setdefault(
            session_id,
            SessionToolContext(session_id=session_id),
        )

    def get_context_data(self, session_id: int) -> dict[str, Any]:
        return self.get_context(session_id).to_dict()

    def load_context(
        self,
        session_id: int,
        context_data: dict[str, Any] | None,
    ) -> SessionToolContext | MergedShippingContext:
        data = context_data if isinstance(context_data, dict) else {}
        validate_session_type(data.get("session_type", SINGLE_CAR))
        if data.get("session_type") == MERGED_SHIPPING:
            ctx = MergedShippingContext.from_dict(data)
            ctx.session_id = session_id
            self.contexts[session_id] = ctx
            return ctx
        ctx = SessionToolContext.from_dict(data)
        ctx.session_id = session_id
        # 旧上下文明确记录的配置文件迁入固定目录，保留手工修改。
        if ctx.group_name and ctx.share_config_file:
            ctx.share_config_file = rename_conversation_files(
                None, ctx.group_name, ctx.share_config_file,
            ) or str(get_product_config_path(ctx.group_name))
            claim_product_config(
                ctx.share_config_file, ctx.config_owner_id,
                adopt_legacy=not bool((context_data or {}).get("config_owner_id")),
            )
        if not ctx.last_share_result and not ctx.share_results_invalidated and not ctx.legacy_share_signature:
            ctx.legacy_share_signature = share_signature(ctx, include_members=True)
        self.contexts[session_id] = ctx
        return ctx

    def remove_context(self, session_id: int) -> None:
        self.contexts.pop(session_id, None)

    def update_group_name(
            self,
            ctx: SessionToolContext,
            new_group_name: str,
    ) -> None:
        if not isinstance(ctx, SessionToolContext):
            raise ValueError("合发会话不能设置单车群聊名称")
        new_group_name = str(
            new_group_name or ""
        ).strip()

        if not new_group_name:
            return

        old_group_name = str(
            ctx.group_name or ""
        ).strip()

        if old_group_name == new_group_name:
            return

        ctx.share_config_file = rename_conversation_files(
            old_group_name, new_group_name, ctx.share_config_file,
        )
        ctx.group_name = new_group_name
        # 成员缓存包含配置路径和群成员，改名后重新检查。
        ctx.member_checked = False
        ctx.member_check_result = None

    def set_context(
            self,
            session_id: int,
            group_name: str | None = None,
    ) -> None:

        ctx = self.get_context(session_id)

        if group_name is not None:
            self.update_group_name(ctx, group_name)

    def update_context_from_intent(
            self,
            ctx: SessionToolContext,
            intent: dict[str, Any],
    ) -> None:
        """
        从用户当前输入中更新当前车的基础信息。

        group_name 只是当前车的属性。
        修改群名不得重置任何其他业务状态。
        """

        if intent.get("group_name"):
            self.update_group_name(ctx, intent["group_name"])

    def update_share_request_from_intent(
            self,
            ctx: SessionToolContext,
            intent: dict[str, Any],
    ) -> None:
        """
        从用户当前输入中更新均摊参数。

        当均摊方式或计算方式变化时，
        原有商品配置确认状态失效。
        """
        req = ctx.share_request
        new_mode = intent.get("share_mode") or req.share_mode
        new_scope = intent.get("calculation_scope") or req.calculation_scope or "flat"
        changed = new_mode != req.share_mode or new_scope != (req.calculation_scope or "flat")
        # 独立模式未显式输入总额时，完整的商品金额合计也是当前总均摊。
        if changed and req.calculation_scope == "independent" and req.amount is None:
            if ctx.share_config_file and owns_product_config(ctx.share_config_file, ctx.config_owner_id):
                configs = load_product_share_config_file(ctx.share_config_file)
                active = [c for c in configs if c.get("计入均摊")]
                if active and all(c.get("商品均摊") not in (None, "") for c in active):
                    req.amount = f"{sum((Decimal(c['商品均摊']) for c in active), Decimal(0)):.2f}"
        if changed:
            req.reset_product_amounts = True
            invalidate_share_confirmation(ctx)
        new_amount = intent.get("amount")
        intent["share_parameters_changed"] = changed or (new_amount is not None and new_amount != req.amount)
        if new_amount is not None and new_amount != req.amount:
            invalidate_share_confirmation(ctx)
        req.share_mode = new_mode
        req.calculation_scope = new_scope
        if new_amount is not None:
            req.amount = new_amount
        # force 只由本轮继续指令消费，不能遗留给后续计算。
        req.force = bool(intent.get("force"))

    def handle(
            self,
            session_id: int,
            user_text: str,
            progress_callback: Callable[[str], None] | None = None,
            parsed_intent: dict[str, Any] | None = None,
    ) -> str | None:

        ctx = self.get_context(session_id)
        intent = parsed_intent if parsed_intent is not None else parse_user_intent(user_text, ctx.session_type)

        if not is_intent_allowed(intent["intent"], ctx.session_type):
            return unsupported_intent_reply(ctx.session_type)
        if intent["intent"] == "unsupported":
            return intent["reply"]
        if intent["intent"] == "rename_conversation":
            from app.core.conversation_workflow import rename_conversation
            return rename_conversation(ctx, intent)
        if isinstance(ctx, MergedShippingContext):
            if intent["intent"] == "chat":
                return None
            from app.core.order_merge_workflow import handle_order_merge
            return handle_order_merge(self, ctx, intent)

        from app.core.participation_workflow import handle_participation
        from app.core.order_comparison_workflow import handle_order_comparison
        comparison_reply = handle_order_comparison(ctx, intent, user_text)
        if comparison_reply is not None:
            return comparison_reply

        participation_reply = handle_participation(self, ctx, intent, user_text)
        if participation_reply is not None:
            return participation_reply

        self.update_context_from_intent(ctx, intent)

        # 只有处于“大货等待确认”状态时，
        # 才把“是”“没问题”等识别成大货确认。
        if ctx.bulk_request.pending_confirmation:
            if has_affirmative_words(user_text):
                return self.handle_confirm_bulk_goods(ctx,progress_callback=progress_callback)

            if has_negative_words(user_text):
                ctx.bulk_request.pending_confirmation = False
                ctx.bulk_request.confirmed = False

                # 清除可能遗留的强制计算状态
                ctx.share_request.force = False

                return (
                    "已取消本次大货计算。\n"
                    "请修改或同步订单信息后，重新输入“查大货”或“算大货”。"
                )

        if (
            ctx.share_request.pending_config_confirmation
            and intent["intent"] in {"chat", "calculate_share", "update_share_config", "confirm_share_config"}
            and has_share_confirmation_words(user_text)
        ):
            intent["intent"] = "confirm_share_config"
            # 普通确认不能继承“继续算”解析出的忽略名单标记。
            intent["force"] = False
            ctx.share_request.force = False

        if intent["intent"] == "chat":
            return None

        if intent["intent"] == "set_context":
            return format_context_update_result(ctx)

        if intent["intent"] == "calculate_bulk_goods":
            return self.handle_calculate_bulk_goods(ctx)

        if intent["intent"] == "update_special_members":
            return self.handle_update_special_members(ctx, intent)

        if intent["intent"] == "show_special_members":
            return format_special_members(ctx.special_members)

        if intent["intent"] == "show_share":
            return self.handle_show_share(ctx)

        if intent["intent"] == "cancel_share":
            if ctx.share_request.pending_config_confirmation or ctx.share_request.config_confirmed:
                invalidate_share_confirmation(ctx)
                return '已取消本次均摊计算；修改配置后请重新输入“算均摊”。'
            return None

        if intent["intent"] == "member_check":
            check_result = self.ensure_member_checked(
                ctx,
                force=True,
                progress_callback=progress_callback,
            )
            return format_member_check_result(check_result)

        if intent["intent"] == "calculate_share":
            self.update_share_request_from_intent(ctx, intent)
            if intent.get("product_share_amounts") or any(
                intent.get(key) is not None for key in ("share_mode", "calculation_scope", "amount")
            ):
                reply = self.handle_update_share_config(ctx, intent)
                if not intent.get("config_saved"):
                    return reply
            if intent.get("force") and ctx.share_request.config_confirmed:
                return self.execute_confirmed_share(ctx, progress_callback)
            return self.handle_calculate_share(
                ctx,
                intent,
                progress_callback=progress_callback,
            )

        if intent["intent"] == "update_share_config":
            self.update_share_request_from_intent(ctx, intent)

            # print("ENTER: update_share_config")

            return self.handle_update_share_config(ctx, intent)

        if intent["intent"] == "confirm_share_config":
            if any(intent.get(key) is not None for key in ("share_mode", "calculation_scope", "amount")) or intent.get("product_share_amounts"):
                self.update_share_request_from_intent(ctx, intent)
                reply = self.handle_update_share_config(ctx, intent)
                if not intent.get("config_saved"):
                    return reply
                return self.handle_calculate_share(ctx, intent, progress_callback)
            return self.handle_confirm_share_config(ctx, intent, progress_callback)

        return None

    def handle_update_special_members(
            self,
            ctx: SessionToolContext,
            intent: dict[str, Any],
    ) -> str:
        updates = (
                intent.get("special_member_updates")
                or []
        )

        if not updates:
            return (
                "没有识别到需要设置的特殊成员信息。\n"
                "例如：车主：昵称=Yann，单号=1，不参摊"
            )

        try:
            members, previews = update_special_members_with_preview(ctx.special_members, updates)
        except SpecialMemberError as exc:
            return f"身份更新失败，原因：{exc}"

        ctx.special_members = members
        ctx.pending_participation = None
        ctx.bulk_request.pending_confirmation = False
        ctx.bulk_request.confirmed = False

        # 特殊成员发生变化后，旧名单检查结果必须失效。
        ctx.member_checked = False
        ctx.member_check_result = None
        ctx.parsed_order_file = None
        invalidate_share_confirmation(ctx)

        action = next((item.get('_操作') for item in updates if item.get('_操作')), '')
        message = {
            '修改': '特殊成员信息已修改并保存；其他身份参数已清空，参摊设置保留，后续检查将重新匹配补全。',
            '清空': '指定身份参数已清空并保存；无剩余身份参数的成员已移除。后续检查可能重新补全空字段。',
            '删除': '指定特殊成员已移除并保存。',
        }.get(action, '特殊成员设置已保存。')
        lines = [message]
        for removed, member in previews:
            row = format_special_members([member]).split('\n', 1)[1]
            lines.append(('已移除：' if removed else '') + row)
        return '\n'.join(lines)

    def ensure_member_checked(
            self,
            ctx: SessionToolContext,
            force: bool = False,
            progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:

        emit_progress(
            progress_callback,
            "正在检查成员……",
        )

        # 即使存在旧缓存，也应先确认特殊成员配置仍然有效。
        special_member_errors = (
            validate_special_member_cache(
                ctx.special_members,
                require_owner=True,
                require_non_share_order_no=False,
                require_order_no=False,
                require_share_state=False,
            )
        )

        if special_member_errors:
            return {
                "ok": False,
                "need_special_member_setup": True,
                "stage": "special_member_setup",
                "message": (
                    "查成员前需要先完成特殊成员设置。"
                ),
                "errors": special_member_errors,
                "special_members": ctx.special_members,
            }

        if (
                ctx.member_checked
                and ctx.member_check_result
                and ctx.parsed_order_file
                and Path(ctx.parsed_order_file).is_file()
                and not force
        ):
            return ctx.member_check_result

        if not ctx.group_name:
            return {
                "ok": False,
                "message": "缺少群聊名称。",
            }

        if not ctx.new_order_file:
            return {
                "ok": False,
                "message": "缺少订单文件。",
            }

        self.ensure_config_ownership(ctx)

        # 普通订单输出
        result = parse_group_member_orders(
            group_name=ctx.group_name,
            order_input=ctx.new_order_file,
            parsed_output_path=get_parsed_orders_path(ctx.session_id),
            special_members=ctx.special_members,
            key_input_func=self.key_input_func,
        )

        # member_parser 可能根据群昵称和订单补全特殊成员信息。
        resolved_special_members = result.get(
            "special_members"
        )

        if resolved_special_members is not None:
            ctx.special_members = resolved_special_members

        ctx.member_checked = True
        ctx.member_check_result = result

        ctx.parsed_order_file = result.get(
            "parsed_order_file"
        )

        # 查成员阶段已经同步过商品配置，
        # 直接保存到当前会话，避免后续均摊/大货重复读取。
        share_config_file = result.get(
            "share_config_file"
        )

        if share_config_file:
            ctx.share_config_file = str(
                share_config_file
            )

        product_configs = result.get(
            "product_configs"
        )

        if product_configs is not None:
            ctx.product_configs = product_configs

        return result

    def handle_calculate_bulk_goods(
            self,
            ctx: SessionToolContext,
    ) -> str:
        if not ctx.group_name:
            return (
                "需要先设置待处理的群聊名称。\n"
                "例如：群聊名称：xxx"
            )

        if not ctx.new_order_file:
            return (
                "需要先设置订单文件。\n"
                "例如：订单：订单.xlsx"
            )

        # ---------------------------------
        # 1. 这里只解析订单，不检查微信群成员
        # ---------------------------------

        parsed_order_file = parse_order_file(
            order_input=ctx.new_order_file,
            output_path=get_parsed_orders_path(ctx.session_id),
        )

        ctx.parsed_order_file = parsed_order_file

        # ---------------------------------
        # 2. 同步基础商品配置
        # ---------------------------------

        self.ensure_config_ownership(ctx)
        ctx.share_config_file = ensure_product_config_file(
            parsed_order_file=parsed_order_file,
            group_name=ctx.group_name,
        )

        # ---------------------------------
        # 3. 大货阶段只更新：
        #    商品单价
        #    商品大货总价
        # ---------------------------------

        update_product_config_before_bulk(
            config_file=ctx.share_config_file,
            original_order_file=ctx.new_order_file,
        )

        # 更新完成后重新读取商品配置
        ctx.product_configs = (
            load_product_share_config_file(
                ctx.share_config_file
            )
        )

        (
            price_lines,
            zero_price_products,
            missing_price_products,
            zero_quantity_products,
            all_prices_below_8,
        ) = build_bulk_product_price_preview(
            ctx.product_configs
        )

        # ---------------------------------
        # 4. 进入等待人工确认状态
        # ---------------------------------

        ctx.bulk_request.pending_confirmation = True
        ctx.bulk_request.confirmed = False

        lines = [
            "大货计算前请确认以下信息。",
            format_complete_calculation_preview(ctx, include_share=False),
            "",
            "当前有效商品单价：",
        ]

        # ---------------------------------
        # 1. 有效商品单价
        # ---------------------------------

        if price_lines:
            lines.extend(price_lines)
        else:
            lines.append(
                "- 没有检测到有效商品单价"
            )

        # ---------------------------------
        # 2. 单价异常
        # ---------------------------------

        if zero_price_products:
            lines.extend(
                [
                    "",
                    "以下商品单价为 0：",
                ]
            )

            for product_name in zero_price_products:
                lines.append(
                    f"- {product_name}"
                )

        if missing_price_products:
            lines.extend(
                [
                    "",
                    "以下商品未检测到单价：",
                ]
            )

            for product_name in missing_price_products:
                lines.append(
                    f"- {product_name}"
                )

        # ---------------------------------
        # 3. 商品数量为 0
        # ---------------------------------

        if zero_quantity_products:
            lines.extend(
                [
                    "",
                    "以下商品当前数量为 0：",
                ]
            )

            for product_name in zero_quantity_products:
                lines.append(
                    f"- {product_name}"
                )

        # ---------------------------------
        # 4. 所有已检测单价均 < 8 元
        # ---------------------------------

        if all_prices_below_8:
            lines.extend(
                [
                    "",
                    "注意：当前所有检测到单价的商品单价"
                    "都低于 8 元。",
                    "请确认订单中的价格是否仍然是均摊价格，"
                    "尚未同步更新为实际大货单价。",
                ]
            )

        lines.extend(
            [
                "",
                "请再次确认：",
                "1. 商品单价是否与实际大货单价一致？",
                "2. 是否有满百减一等价格变化？",
                "3. 漏收、补收的均摊是否已经处理？",
                "4. 当前订单是否已经全部同步？",
                "",
                "以上内容全部确认无误后，请回复“是”。",
            ]
        )

        return "\n".join(lines)


    def handle_confirm_bulk_goods(
            self,
            ctx: SessionToolContext,
            progress_callback: Callable[[str], None] | None = None,
    ) -> str:
        if not ctx.bulk_request.pending_confirmation:
            return "当前没有等待确认的大货计算。"

        # 用户确认时再强制重新读取一次订单，
        # 防止两次消息之间订单文件被修改。
        check_result = self.ensure_member_checked(
            ctx,
            force=True,
            progress_callback=progress_callback,
        )

        if not check_result.get("ok"):
            ctx.bulk_request.pending_confirmation = False
            return format_member_check_result(
                check_result
            )

        blocking_issues = (
                check_result.get("blocking_issues")
                or []
        )

        if blocking_issues:
            ctx.bulk_request.pending_confirmation = False

            return (
                    "确认时重新检查发现群成员或订单已发生变化，"
                    "本次大货计算已停止。\n\n"
                    + format_member_check_result(
                check_result
            )
            )

        parsed_order_file = (
                check_result.get("parsed_order_file")
                or ctx.parsed_order_file
        )

        if not parsed_order_file:
            ctx.bulk_request.pending_confirmation = False
            return "没有找到订单的简化文件。"

        if not ctx.share_config_file:
            ctx.bulk_request.pending_confirmation = False
            return "没有找到商品配置文件。"

        update_product_config_before_bulk(
            config_file=ctx.share_config_file,
            original_order_file=ctx.new_order_file,
        )

        ctx.product_configs = (
            load_product_share_config_file(
                ctx.share_config_file
            )
        )

        emit_progress(
            progress_callback,
            "正在计算大货……",
        )

        result = create_bulk_receivable_orders(
            parsed_order_file=parsed_order_file,
            group_name=ctx.group_name,
        )

        ctx.bulk_request.pending_confirmation = False
        ctx.bulk_request.confirmed = True

        # 一个完整业务计算已经结束，清除可能残留的“先算”状态
        ctx.share_request.force = False

        lines = [
            "大货应收订单已生成。",
            f"订单数量：{result.get('order_count')}",
            f"结果文件：{result.get('result_file')}",
            "",
            "其中原订单的“总金额”已作为“大货应收金额”，"
            "代码没有重新计算或修改该金额。",
        ]

        return "\n".join(lines)

    def ensure_share_config_loaded(
            self,
            ctx: SessionToolContext,
            parsed_order_file: str,
    ) -> None:
        """
        确保当前商品配置文件存在，并同步基础商品信息。

        每次调用都会：
            1. 根据当前 parsed_orders 同步商品序号、名称、数量；
            2. 新商品初始化“计入均摊”；
            3. 保留已有阶段字段；
            4. 重新读取配置到 ctx.product_configs。
        """
        self.ensure_config_ownership(ctx)
        ctx.share_config_file = ensure_product_config_file(
            parsed_order_file=parsed_order_file,
            group_name=ctx.group_name,
        )

        ctx.product_configs = load_product_share_config_file(
            ctx.share_config_file
        )

    def ensure_config_ownership(self, ctx: SessionToolContext) -> None:
        if not ctx.group_name:
            return
        path = str(get_product_config_path(ctx.group_name))
        if claim_product_config(path, ctx.config_owner_id):
            ctx.product_configs = None
            ctx.last_share_signature = None
            invalidate_share_confirmation(ctx)
        ctx.share_config_file = path

    def prepare_share_config(self, ctx: SessionToolContext) -> str | None:
        """只解析本地订单、保存配置；不读取微信群成员。"""
        if not ctx.group_name:
            return "需要先设置群聊名称。"
        if not ctx.new_order_file:
            return "需要先设置订单文件。"
        self.ensure_config_ownership(ctx)
        ctx.parsed_order_file = parse_order_file(
            order_input=ctx.new_order_file,
            output_path=get_parsed_orders_path(ctx.session_id),
        )
        self.ensure_share_config_loaded(ctx, ctx.parsed_order_file)
        req = ctx.share_request
        if req.reset_product_amounts:
            reset_product_share_fields(
                ctx.share_config_file, share_mode=req.share_mode,
                calculation_scope=req.calculation_scope or "flat",
            )
            req.reset_product_amounts = False
        if req.share_mode:
            if req.calculation_scope == "independent" or req.amount is not None:
                update_product_config_before_share(
                    config_file=ctx.share_config_file, share_mode=req.share_mode,
                    calculation_scope=req.calculation_scope or "flat", total_amount=req.amount,
                )
        ctx.product_configs = load_product_share_config_file(ctx.share_config_file)
        return None

    def share_config_errors(self, ctx: SessionToolContext) -> list[str]:
        req = ctx.share_request
        errors = []
        if not req.share_mode:
            errors.append("请说明均摊方式：人头摊或个数摊。")
        active = [c for c in ctx.product_configs or [] if c.get("计入均摊")]
        if not active:
            errors.append("没有可参摊商品，请检查商品配置。")
        if req.calculation_scope == "independent":
            missing = [c["商品名称"] for c in active if c.get("商品均摊") in (None, "")]
            if missing:
                errors.append("请补充各商品独立均摊金额：" + "、".join(missing))
            elif req.amount is not None:
                total = sum((Decimal(str(c["商品均摊"])) for c in active), Decimal(0))
                if total != Decimal(req.amount):
                    errors.append(f"各商品均摊合计 {total:.2f} 元与总均摊 {req.amount} 元不一致。")
        elif req.amount is None:
            errors.append("请补充总均摊金额，例如：金额120。")
        elif Decimal(req.amount) <= 0:
            errors.append("拉通总均摊金额必须大于 0。")
        return errors

    def handle_update_share_config(self, ctx: SessionToolContext, intent: dict[str, Any]) -> str:
        error = self.prepare_share_config(ctx)
        if error:
            return "均摊参数已保存。" + error
        req = ctx.share_request
        updates = intent.get("product_share_amounts") or []
        if updates and req.calculation_scope != "independent":
            return "商品独立金额未写入，请先设置独立模式：独立人头摊或独立个数摊。"
        unmatched = []
        updated_items = []
        changed = bool(intent.get("share_parameters_changed"))
        if updates:
            before = ctx.product_configs
            result = update_product_share_config_file(config_file=ctx.share_config_file, updates=updates)
            updated_items = result.get('updated_items') or []
            unmatched = result.get("unmatched_updates") or []
            changed = changed or before != load_product_share_config_file(ctx.share_config_file)
        if changed or unmatched:
            invalidate_share_confirmation(ctx)
            reset_product_share_fields(
                ctx.share_config_file, share_mode=req.share_mode,
                calculation_scope=req.calculation_scope or "flat", clear_amounts=False,
            )
        ctx.product_configs = load_product_share_config_file(ctx.share_config_file)
        intent["config_saved"] = not unmatched
        lines = ["均摊配置已保存，本次未执行计算。"]
        if updates and not intent.get('share_parameters_changed'):
            from app.analysis.participation import product_preview
            names = {item['商品名称'] for item in updated_items}
            lines.extend(product_preview(item) for item in ctx.product_configs if item['商品名称'] in names)
        else:
            lines.append(format_pending_share_summary(ctx))
        if unmatched:
            for item in unmatched:
                name = item.get('商品名称') or item.get('商品序号')
                candidates = item.get('候选商品') or []
                if candidates:
                    lines.append(f"“{name}”匹配到多个商品，本项金额未写入：" + '、'.join(candidates)
                                 + '。请用完整商品名称或商品序号重新录入金额。')
                else:
                    lines.append(f"“{name}”未匹配到商品，本项金额未写入。")
        if not updates or intent.get('share_parameters_changed'):
            lines.extend(self.share_config_errors(ctx))
        lines.append('配置完整后请输入“算均摊”，确认商品配置后才会查成员并计算。')
        return "\n".join(lines)

    def handle_calculate_share(self, ctx: SessionToolContext, intent: dict[str, Any],
                               progress_callback: Callable[[str], None] | None = None) -> str:
        invalidate_share_confirmation(ctx)
        error = self.prepare_share_config(ctx)
        if error:
            return error
        errors = self.share_config_errors(ctx)
        if errors:
            return "暂不计算，也未查成员。\n" + "\n".join(errors) + "\n\n" + format_pending_share_summary(ctx)
        ctx.bulk_request.pending_confirmation = False
        req = ctx.share_request
        req.pending_config_confirmation = True
        req.confirmation_signature = share_signature(ctx)
        lines = ["计算均摊前，请确认商品配置：", format_complete_calculation_preview(ctx)]
        lines.extend([f"配置文件：{ctx.share_config_file}", '确认无误后可输入“计算”“算”“无误”或“下一步”；需修改时请修改配置后重新输入“算均摊”。'])
        return "\n".join(lines)

    def handle_confirm_share_config(self, ctx: SessionToolContext, intent: dict[str, Any],
                                    progress_callback: Callable[[str], None] | None = None) -> str:
        req = ctx.share_request
        if not req.pending_config_confirmation:
            return '当前没有待确认的商品均摊配置，请先输入“算均摊”。'
        if not req.confirmation_signature or req.confirmation_signature != share_signature(ctx):
            return "商品配置或订单已变化，请重新确认。\n\n" + self.handle_calculate_share(ctx, {})
        req.pending_config_confirmation = False
        req.config_confirmed = True
        return self.execute_confirmed_share(ctx, progress_callback)

    def execute_confirmed_share(self, ctx: SessionToolContext,
                                progress_callback: Callable[[str], None] | None = None) -> str:
        req = ctx.share_request
        force = req.force
        req.force = False
        if not req.config_confirmed or not req.confirmation_signature or req.confirmation_signature != share_signature(ctx):
            return self.handle_calculate_share(ctx, {}, progress_callback)
        # 重新查成员，不能沿用配置或订单变更前的核对缓存。
        check_result = self.ensure_member_checked(ctx, force=True, progress_callback=progress_callback)
        if not check_result.get("ok"):
            return format_member_check_result(check_result)
        if req.confirmation_signature != share_signature(ctx):
            return "成员核对期间商品配置或订单发生变化，暂不计算。\n\n" + self.handle_calculate_share(ctx, {})
        if get_blocking_member_issues(check_result) and not force:
            return ("计算均摊前发现名单核对问题，暂不计算。\n\n"
                    + format_member_check_result(check_result)
                    + "\n\n如需忽略名单问题，可输入：忽略名单问题，继续计算。")
        ctx.product_configs = load_product_share_config_file(ctx.share_config_file)
        errors = self.share_config_errors(ctx)
        if errors:
            invalidate_share_confirmation(ctx)
            return "\n".join(errors)
        emit_progress(progress_callback, "正在计算均摊……")
        result = calculate_share(
            parsed_order_file=ctx.parsed_order_file, total_amount=req.amount,
            share_mode=req.share_mode, calculation_scope=req.calculation_scope or "flat",
            product_configs=ctx.product_configs, group_name=ctx.group_name,
            excluded_order_nos=get_non_share_order_nos(ctx.special_members),
        )
        invalidate_share_confirmation(ctx)
        if not result.get("ok"):
            return format_share_need_user_input(result) if result.get("need_user_input") else str(result.get("message") or "均摊计算失败。")
        update_product_config_after_share(ctx.share_config_file, result.get("product_configs") or [])
        ctx.product_configs = load_product_share_config_file(ctx.share_config_file)
        result["product_configs"] = ctx.product_configs
        ctx.last_share_result = result
        ctx.share_results_invalidated = False
        ctx.last_share_signature = share_signature(ctx, include_members=True)
        return format_share_result(result, ctx.group_name, check_result, ctx.special_members)

    def handle_show_share(self, ctx: SessionToolContext) -> str:
        # 只读：不准备订单、不写配置、不检查成员、不生成结果文件。
        signature = share_signature(ctx, include_members=True)
        if ctx.last_share_result and signature and signature == ctx.last_share_signature:
            return format_share_summary(ctx.last_share_result)
        configs = []
        if ctx.share_config_file and owns_product_config(ctx.share_config_file, ctx.config_owner_id):
            if Path(ctx.share_config_file).is_file():
                configs = load_product_share_config_file(ctx.share_config_file)
        legacy_changed = bool(ctx.legacy_share_signature and ctx.legacy_share_signature != signature)
        if not ctx.last_share_result and not ctx.share_results_invalidated and not legacy_changed:
            historical = format_legacy_share_summary(configs)
            if historical:
                return historical
        stale = bool(ctx.last_share_result or legacy_changed or (
            ctx.share_results_invalidated and any(c.get("单份均摊") not in (None, "") for c in configs)))
        status = "原计算结果已失效，请重新计算。" if stale else "尚未计算均摊。"
        return status + "\n" + format_pending_share_summary(ctx, configs)


def invalidate_share_confirmation(ctx: SessionToolContext) -> None:
    req = ctx.share_request
    req.pending_config_confirmation = False
    req.config_confirmed = False
    req.confirmation_signature = None
    req.force = False
    ctx.last_share_signature = None
    ctx.share_results_invalidated = True


def share_signature(ctx: SessionToolContext, *, include_members: bool = False) -> str | None:
    """校验实际文件内容，发现手工改表、原路径覆盖订单以及特殊成员变化。"""
    if not ctx.new_order_file or not ctx.share_config_file:
        return None
    if not owns_product_config(ctx.share_config_file, ctx.config_owner_id):
        return None
    try:
        configs = load_product_share_config_file(ctx.share_config_file)
        fields = ["商品序号", "商品名称", "商品数量", "计入均摊", "均摊类型", "商品均摊"]
        if include_members:
            fields.append("单份均摊")
        data = {
            "order": hashlib.sha256(Path(ctx.new_order_file).read_bytes()).hexdigest(),
            "order_path": str(Path(ctx.new_order_file).resolve()),
            "owner": ctx.config_owner_id,
            "group": ctx.group_name,
            "configs": [{k: c.get(k) for k in fields} for c in configs],
            "mode": ctx.share_request.share_mode,
            "scope": ctx.share_request.calculation_scope,
            "amount": ctx.share_request.amount,
            "reset_pending": ctx.share_request.reset_product_amounts,
        }
        if include_members:
            data["members"] = ctx.special_members
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    except (OSError, ValueError, RuntimeError):
        return None


def format_share_money(value: Any) -> str:
    return f"{Decimal(str(value)):.2f}" if value not in (None, "") else "未填写"


def format_legacy_share_summary(configs: list[dict[str, Any]]) -> str | None:
    """只展示 CSV 确实保存的历史字段，不从取整金额倒推人数或数量。"""
    active = [c for c in configs if c.get("计入均摊")]
    if not any(c.get("单份均摊") not in (None, "") for c in active):
        return None
    types = set()
    for c in active:
        try:
            types.add(normalize_share_type(c.get("均摊类型") or ""))
        except RuntimeError:
            types.add("unknown")
    lines = ["历史配置记录，尚未校验是否适用于当前订单。", ""]
    if len(types) != 1 or "unknown" in types:
        lines.extend(["均摊类型：历史配置不完整或存在不同类型", "计算方式：见商品明细",
                      "总均摊：无法确定", "参摊人数／个数：历史记录未保存"])
        for c in active:
            lines.append(f"- {c['商品名称']}：{c.get('均摊类型') or '未填写'}；"
                         f"商品均摊：{format_share_money(c.get('商品均摊'))}；"
                         f"单份均摊：{format_share_money(c.get('单份均摊'))}")
        return "\n".join(lines)
    share_type = next(iter(types))
    mode, scope = share_type.split("_", 1)
    lines.extend([f"均摊类型：{'人头摊' if mode == 'head' else '个数摊'}",
                  f"计算方式：{'独立' if scope == 'independent' else '拉通'}"])
    amounts = [c.get("商品均摊") for c in active]
    if all(a not in (None, "") for a in amounts):
        if scope == "independent":
            total = format_share_money(sum((Decimal(a) for a in amounts), Decimal(0)))
        else:
            total = format_share_money(amounts[0]) if len(set(amounts)) == 1 else "历史配置金额不一致"
    else:
        total = "历史记录不完整"
    lines.append(f"总均摊：{total}")
    if scope == "independent":
        lines.extend(f"- {c['商品名称']}独立均摊：{format_share_money(c.get('商品均摊'))}" for c in active)
    lines.append("参摊人数：历史记录未保存" if mode == "head" else "参摊个数：历史记录未保存")
    if mode == "quantity" or scope == "independent":
        for c in configs:
            lines.append(f"- {c['商品名称']}：历史记录未保存" if c.get("计入均摊")
                         else f"- {c['商品名称']}：不参摊")
    label = "单人均摊" if mode == "head" else "单个商品均摊"
    units = [c.get("单份均摊") for c in active]
    if scope == "flat" and len(set(units)) == 1 and units[0] not in (None, ""):
        lines.append(f"{label}：{format_share_money(units[0])}")
    else:
        lines.append(f"各商品{label}：")
        for c in active:
            value = c.get("单份均摊")
            lines.append(f"- {c['商品名称']}：{format_share_money(value) if value not in (None, '') else '历史记录未保存'}")
    return "\n".join(lines)


def format_share_summary(result: dict[str, Any]) -> str:
    """查询和计算完成共享同一摘要，统计由计算器提供。"""
    mode = result.get("share_mode")
    scope = result.get("calculation_scope")
    lines = [f"均摊类型：{'人头摊' if mode == 'head' else '个数摊'}",
             f"计算方式：{'独立' if scope == 'independent' else '拉通'}",
             f"总均摊：{result['total_amount']}"]
    products = result.get("product_statistics") or []
    if scope == "independent":
        for c in products:
            if c["included"]:
                lines.append(f"- {c['product_name']}独立均摊：{c['amount']}")
    if mode == "head":
        lines.append(f"参摊人数：{result['participant_count']} 人")
        if scope == "independent":
            lines.extend(f"- {c['product_name']}：{c['participant_count']} 人" for c in products if c["included"])
    else:
        lines.append(f"参摊个数：{result['total_share_quantity']} 个")
        for c in products:
            suffix = "" if c["included"] else "（不参摊）"
            lines.append(f"- {c['product_name']}：{c['quantity']} 个{suffix}")
    if scope == "independent":
        lines.append("各商品单人均摊：" if mode == "head" else "各商品单个均摊：")
        for c in products:
            if c["included"]:
                value = c['unit_amount']
                lines.append(f"- {c['product_name']}：{value}" if value is not None else f"- {c['product_name']}：无参摊订单")
    else:
        label = "单人均摊" if mode == "head" else "单个商品均摊"
        lines.append(f"{label}：{result['unit_share_amount']}")
    return "\n".join(lines)


def format_pending_share_summary(ctx: SessionToolContext, configs: list[dict[str, Any]] | None = None) -> str:
    if configs is None:
        configs = ctx.product_configs or []
    req = ctx.share_request
    active = [c for c in configs if c.get("计入均摊")]
    share_mode = req.share_mode
    calculation_scope = req.calculation_scope
    # 旧对话可能只在 CSV 保存过方式；只读补全展示，不写回会话参数。
    if not share_mode or not calculation_scope:
        try:
            types = {normalize_share_type(c.get("均摊类型") or "") for c in active}
            if len(types) == 1:
                saved_mode, saved_scope = next(iter(types)).split("_", 1)
                share_mode = share_mode or saved_mode
                calculation_scope = calculation_scope or saved_scope
        except RuntimeError:
            pass
    mode = {"head": "人头摊", "quantity": "个数摊"}.get(share_mode, "未设置")
    scope = "独立" if calculation_scope == "independent" else "拉通"
    total = req.amount
    if scope == "独立" and total is None and active and all(c.get("商品均摊") not in (None, "") for c in active):
        total = f"{sum((Decimal(str(c['商品均摊'])) for c in active), Decimal(0)):.2f}"
    if scope == "拉通" and total is None and active:
        amounts = {c.get("商品均摊") for c in active}
        if len(amounts) == 1 and next(iter(amounts)) not in (None, ""):
            total = next(iter(amounts))
    lines = [f"均摊类型：{mode}", f"计算方式：{scope}",
             f"总均摊：{format_share_money(total)}"]
    if scope == "独立":
        lines.extend(f"- {c['商品名称']}独立均摊：{c.get('商品均摊') or '未填写'}" for c in active)
    lines.append("参摊人数：待计算" if share_mode == "head" else "参摊个数：待计算")
    if share_mode == "quantity":
        lines.extend(f"- {c['商品名称']}：{'待计算' if c.get('计入均摊') else '0 个（不参摊）'}" for c in configs)
    lines.append("单人均摊：待计算" if share_mode == "head" else "单个商品均摊：待计算")
    return "\n".join(lines)


def format_complete_calculation_preview(ctx: SessionToolContext, *, include_share: bool = True) -> str:
    """计算确认统一展示当前配置、全部商品和全部特殊成员。"""
    from app.analysis.participation import product_preview
    lines = [f'群聊：{ctx.group_name or "未设置"}',
             f'订单：{format_order_path(ctx.new_order_file)}',
             ]
    if include_share:
        lines.append(format_pending_share_summary(ctx))
    lines.extend(['', '全部商品配置：'])
    for item in ctx.product_configs or []:
        if include_share:
            lines.append(product_preview(item))
        else:
            fields = ('商品数量', '商品单价', '商品大货总价')
            lines.append(f"商品{item.get('商品序号')}：{item['商品名称']}" + ''.join(
                f"｜{field}={item.get(field) if item.get(field) not in (None, '') else '未设置'}"
                for field in fields))
    lines.extend(['', format_special_members(ctx.special_members)])
    return '\n'.join(lines)


def format_special_members(
    special_members: list[dict[str, Any]],
) -> str:
    if not special_members:
        return "当前没有设置特殊成员。"

    role_order = {
        "车主": 0,
        "工具人": 1,
        "供稿人": 2,
        "画师": 3,
        "章稿画师": 4,
        "其他不参摊成员": 5,
    }

    sorted_members = sorted(
        special_members,
        key=lambda item: (
            role_order.get(
                str(item.get("角色") or ""),
                99,
            ),
            int(item["单号"])
            if str(
                item.get("单号") or ""
            ).isdigit()
            else 999999,
        ),
    )

    lines = ["当前特殊成员："]

    for member in sorted_members:
        share_text = (
            "不参摊"
            if member.get("参摊") is False
            else "参摊"
        )

        lines.append(
            f"- {member.get('角色')}｜"
            f"昵称：{member.get('昵称') or '未设置'}｜"
            f"群昵称："
            f"{member.get('群昵称') or '未设置'}｜"
            f"单号：{member.get('单号') or '未设置'}｜"
            f"{share_text}"
        )

    return "\n".join(lines)


def get_special_member_display_name(member: dict[str, Any]) -> str:
    return str(
        member.get("昵称")
        or member.get("群昵称")
        or member.get("单号")
        or "未命名成员"
    ).strip()


def format_non_share_special_member_note(
    special_members: list[dict[str, Any]],
) -> str:
    """
    生成“不参摊说明”。

    规则：
    1. 参摊=True 的特殊成员不显示。
    2. 工具人有单号且参摊=True，不显示。
    3. 工具人没有单号且不参摊，显示“工具人xxx不买不参摊”。
    4. 其他不参摊成员显示“角色xxx（单号x）不参摊”。
    """
    notes: list[str] = []

    role_order = {
        "车主": 0,
        "工具人": 1,
        "供稿人": 2,
        "画师": 3,
        "章稿画师": 4,
        "其他不参摊成员": 5,
    }

    sorted_members = sorted(
        special_members or [],
        key=lambda item: (
            role_order.get(str(item.get("角色") or ""), 99),
            int(item["单号"])
            if str(item.get("单号") or "").isdigit()
            else 999999,
        ),
    )

    for member in sorted_members:
        role = str(member.get("角色") or "").strip()
        name = get_special_member_display_name(member)
        order_no = str(member.get("单号") or "").strip()
        include_share = member.get("参摊")

        # 明确参摊的特殊成员，不进入“不参摊说明”。
        if include_share is True:
            continue

        # 只说明不参摊成员。
        if include_share is not False:
            continue

        if role == "工具人" and not order_no:
            notes.append(f"工具人{name}不买不参摊")
            continue

        if order_no:
            notes.append(f"{role}{name}（单号{order_no}）不参摊")
        else:
            notes.append(f"{role}{name}不参摊")

    if not notes:
        return "无"

    return "，".join(notes) + "。"


def format_member_check_summary_for_share(
    member_check_result: dict[str, Any] | None,
) -> str:
    if not member_check_result:
        return "群成员与订单检查结果未知"

    if (
        member_check_result.get("ok")
        and not get_blocking_member_issues(member_check_result)
    ):
        return "群成员与订单检查没问题"

    return "群成员与订单已检查，已按“先算”强制继续"


def get_blocking_member_issues(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    if not result.get("ok"):
        issues.append("成员核对失败")

    if result.get("members_without_serial"):
        issues.append("存在群昵称前没有数字的成员")

    if result.get("duplicate_member_serials"):
        issues.append("群昵称中存在重复标注的序号")

    if result.get("serials_in_group_not_in_orders"):
        issues.append("群昵称有、但是订单没有的序号")

    if result.get("serials_in_orders_not_in_group"):
        issues.append("订单里有、但是群昵称没有的序号")

    only_non_share_orders = (result.get("only_non_share_orders") or [])
    if only_non_share_orders:
        issues.append("存在只购买不参摊商品的订单")

    return issues


def format_member_check_result(
    result: dict[str, Any],
) -> str:
    if result.get("need_special_member_setup"):
        lines = [
            "查成员前需要先完成特殊成员设置。",
        ]

        errors = result.get("errors") or []

        if errors:
            lines.append("")
            lines.append("当前问题：")

            for error in errors:
                lines.append(f"- {error}")

        current_members = (
            result.get("special_members")
            or []
        )

        if current_members:
            lines.append("")
            lines.append(
                format_special_members(current_members)
            )

        lines.append("")
        lines.append("至少需要设置1名车主。")
        lines.append(
            "例如：车主：昵称=Yann，"
            "群昵称=001 Yann，单号=1，不参摊"
        )

        return "\n".join(lines)

    if not result.get("ok"):
        return (
            "成员与订单核对失败："
            f"{result.get('message')}"
        )

    lines: list[str] = []

    lines.append("成员与订单核对完成。")
    lines.append(f"群聊名称：{result.get('群聊名称')}")
    lines.append(f"群成员数量：{result.get('member_count')}")
    lines.append(f"简化后的订单文件：{result.get('parsed_order_file')}")
    share_config_file = result.get("share_config_file")
    if share_config_file:
        lines.append(f"商品均摊配置表：{share_config_file}")

    auto_added_members = result.get("auto_added_special_members") or []
    if auto_added_members:
        lines.append("")
        lines.append(
            "检测到未录入身份的特殊成员，"
            "已自动加入“其他不参摊成员”："
        )

        for member in auto_added_members:
            order_no = str(member.get("单号") or "").strip()
            nickname = str(member.get("昵称") or "").strip()
            display_name = (nickname or "未识别昵称")

            if order_no:
                lines.append(f"- {order_no}｜{display_name}")
            else:
                lines.append(f"- {display_name}")

    members_without_serial = result.get("members_without_serial") or []
    if members_without_serial:
        lines.append("")
        lines.append(f"群昵称前没有数字的成员数量：{len(members_without_serial)}")
        for member in members_without_serial:
            lines.append(f"- {member.get('群昵称') or member.get('昵称') or member.get('wxid')}")

    duplicate_member_serials = result.get("duplicate_member_serials") or []
    if duplicate_member_serials:
        lines.append("")
        lines.append(f"群昵称中重复标注的序号数量：{len(duplicate_member_serials)}")
        for item in duplicate_member_serials:
            serial = item.get("序号")
            members = item.get("members") or []
            names = "，".join(
                str(m.get("群昵称") or m.get("昵称") or m.get("wxid"))
                for m in members
            )
            lines.append(f"- 序号 {serial}：{names}")

    serials_in_group_not_in_orders = result.get("serials_in_group_not_in_orders") or []
    if serials_in_group_not_in_orders:
        lines.append("")
        lines.append("群昵称有、但是订单没有的序号：")
        lines.append("，".join(serials_in_group_not_in_orders))

    serials_in_orders_not_in_group = result.get("serials_in_orders_not_in_group") or []
    if serials_in_orders_not_in_group:
        lines.append("")
        lines.append("订单里有、但是群昵称没有的序号：")
        lines.append("，".join(serials_in_orders_not_in_group))

    only_non_share_orders = result.get("only_non_share_orders") or []
    if only_non_share_orders:
        lines.append("")
        lines.append(
            "以下成员只含有不参摊商品，"
            "请检查订单是否异常："
        )

        for order in only_non_share_orders:
            order_no = str(order.get("单号") or "").strip()
            nickname = str(order.get("昵称") or "").strip()
            product_parts = []

            for product in (order.get("不参摊商品") or []):
                product_name = str(product.get("商品名称") or "").strip()
                quantity = product.get("数量")
                if not product_name:
                    continue

                product_parts.append(f"{product_name} × {quantity}")

            member_text = "｜".join(part for part in (order_no, nickname) if part)
            product_text = "，".join(product_parts)
            if product_text:
                lines.append(f"- {member_text}｜{product_text}")
            else:
                lines.append(f"- {member_text}")

    return "\n".join(lines)


def format_share_result(
    result: dict,
    group_name: str | None = None,
    member_check_result: dict[str, Any] | None = None,
    special_members: list[dict[str, Any]] | None = None,
) -> str:
    if not result.get("ok"):
        if result.get("need_user_input"):
            return format_share_need_user_input(result)

        return result.get("message", "均摊计算失败。")

    lines: list[str] = []

    lines.append(str(group_name or result.get("group_name") or "未设置群名称"))
    lines.append(format_member_check_summary_for_share(member_check_result))
    lines.append("")

    lines.append(format_share_summary(result))

    lines.append(f"实际总收款：{result['total_collected']}")
    lines.append(f"向上取整多收：{result['over_collected']}")

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("提醒：")

        for warning in warnings:
            lines.append(f"- {warning}")

    result_file = result.get("result_file")

    if result_file:
        lines.append("")
        lines.append(f"结果文件：{result_file}")

    lines.append("")
    lines.append("前 10 条结果预览：")

    for item in result.get("items", [])[:10]:
        lines.append(
            f"- {item['单号']}｜{item['昵称']}｜"
            f"商品总数 {item['商品总数']}｜应收 {item['应收金额']}"
        )

    if len(result.get("items", [])) > 10:
        lines.append(f"... 共 {len(result['items'])} 条，完整结果见结果文件。")

    return "\n".join(lines)


def format_share_need_user_input(result: dict) -> str:
    lines = []

    lines.append(result.get("message", "需要补充均摊信息。"))

    missing_fields = result.get("missing_fields") or []

    if missing_fields:
        lines.append("")
        lines.append("缺少的信息：")

        for item in missing_fields:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('商品名称')}：{item.get('缺少字段')}"
                )
            else:
                lines.append(f"- {item}")

    product_configs = result.get("product_configs") or []

    used_configs = [
        cfg for cfg in product_configs
        if cfg.get("商品名称")
    ]

    if used_configs:
        lines.append("")
        lines.append("当前商品配置：")

        for cfg in used_configs:
            lines.append(
                f"- {cfg.get('商品序号')}｜"
                f"{cfg.get('商品名称')}｜"
                f"商品数量：{cfg.get('商品数量')}｜"
                f"计入均摊：{cfg.get('计入均摊')}｜"
                f"均摊类型：{cfg.get('均摊类型') or '未填写'}｜"
                f"商品均摊：{cfg.get('商品均摊') or '未填写'}｜"
                f"单份均摊：{cfg.get('单份均摊') or '未计算'}"
            )

    return "\n".join(lines)


def _to_json_safe(value: Any) -> Any:
    """递归转换 Path 等对象，保证结果可以交给 json.dumps。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, dict):
        return {
            str(key): _to_json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(item) for item in value]

    raise TypeError(
        f"会话上下文包含无法保存的类型：{type(value).__name__}"
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None

    if not isinstance(value, (str, int, float)):
        return None

    normalized = str(value).strip()
    return normalized or None


def _dict_list_or_empty(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    return [
        _to_json_safe(item)
        for item in value
        if isinstance(item, dict)
    ]


def reset_bulk_goods_context(ctx: SessionToolContext) -> None:
    ctx.bulk_request.pending_confirmation = False
    ctx.bulk_request.confirmed = False


def format_context_update_result(ctx: SessionToolContext) -> str:
    lines = ["已更新当前处理上下文。"]

    lines.append(f"群聊名称：{ctx.group_name or '未设置'}")
    lines.append(f"新订单：{format_order_path(ctx.new_order_file)}")
    lines.append(f"旧订单：{format_order_path(ctx.old_order_file)}")
    lines.append(f"缓存1：{format_order_path(ctx.order_cache_1_file)}")
    lines.append(f"缓存2：{format_order_path(ctx.order_cache_2_file)}")

    return "\n".join(lines)
