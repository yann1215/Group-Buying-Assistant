# app/core/tool_orchestrator.py

"""

1. 判断当前 session 有没有群聊名称和订单文件
2. 如果没核对过，先调用 parse_group_member_orders()
3. 如果名单有严重问题，先返回问题，不计算
4. 如果可以计算，再调用 calculate_share()

"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.analysis.special_member import (
    SpecialMemberError,
    update_special_member_cache,
    validate_special_member_cache,
)
from app.analysis.member_parser import parse_group_member_orders
from app.analysis.special_member import (
    get_non_share_order_nos,
)
from app.analysis.share_calculator import calculate_share
from app.analysis.share_config import (
    create_product_share_config_file,
    load_product_share_config_file,
    summarize_product_share_config,
    update_product_share_config_file,
)
from app.analysis.bulk_calculator import (
    create_bulk_receivable_orders,
    find_only_non_share_orders,
)
from app.core.intent_parser import (
    has_affirmative_words,
    has_negative_words,
    parse_user_intent,
)


DEFAULT_ORDER_OUTPUT_DIR = Path("./orders/output")


@dataclass
class ShareRequestState:
    share_mode: str | None = None
    calculation_scope: str | None = None
    amount: str | None = None
    force: bool = False

    pending_config_confirmation: bool = False
    config_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "share_mode": self.share_mode,
            "calculation_scope": self.calculation_scope,
            "amount": self.amount,
            "force": self.force,
            "pending_config_confirmation": self.pending_config_confirmation,
            "config_confirmed": self.config_confirmed,
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
class SessionToolContext:
    group_name: str | None = None

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

    order_output_dir: str | Path | None = None

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
            "context_version": 1,
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
            "order_output_dir": _to_json_safe(self.order_output_dir),
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
            order_output_dir=_optional_string(
                data.get("order_output_dir")
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


class ToolOrchestrator:

    def __init__(
        self,
        key_input_func: Callable[[str], str] | None = None,
    ) -> None:
        self.contexts: dict[int, SessionToolContext] = {}

        self.key_input_func = key_input_func

    def get_context(self, session_id: int) -> SessionToolContext:
        return self.contexts.setdefault(
            session_id,
            SessionToolContext(),
        )

    def get_context_data(self, session_id: int) -> dict[str, Any]:
        return self.get_context(session_id).to_dict()

    def load_context(
        self,
        session_id: int,
        context_data: dict[str, Any] | None,
    ) -> SessionToolContext:
        ctx = SessionToolContext.from_dict(context_data)
        self.contexts[session_id] = ctx
        return ctx

    def remove_context(self, session_id: int) -> None:
        self.contexts.pop(session_id, None)

    def set_context(
        self,
        session_id: int,
        group_name: str | None = None,
        order_output_dir: str | Path | None = None,
    ) -> None:
        ctx = self.contexts.setdefault(session_id, SessionToolContext())

        if group_name is not None:
            new_group_name = str(group_name).strip()

            if (
                    ctx.group_name is not None
                    and ctx.group_name != new_group_name
            ):
                ctx.special_members.clear()
                ctx.member_checked = False
                ctx.member_check_result = None
                ctx.parsed_order_file = None
                ctx.share_config_file = None
                ctx.product_configs = None
                reset_bulk_goods_context(ctx)

            ctx.group_name = new_group_name

        if order_output_dir is not None:
            ctx.order_output_dir = normalize_output_dir(order_output_dir)
        elif ctx.order_output_dir is None:
            ctx.order_output_dir = str(DEFAULT_ORDER_OUTPUT_DIR)


    def update_context_from_intent(
        self,
        ctx: SessionToolContext,
        intent: dict[str, Any],
    ) -> None:
        """
        从用户当前这句话中更新群聊、订单文件、输出目录。
        只更新本轮明确提到的字段。
        """
        if intent.get("group_name"):
            new_group_name = intent["group_name"]

            group_changed = (
                    ctx.group_name is not None
                    and ctx.group_name != new_group_name
            )

            if group_changed:
                ctx.special_members.clear()

                ctx.member_checked = False
                ctx.member_check_result = None
                ctx.parsed_order_file = None
                ctx.share_config_file = None
                ctx.product_configs = None

            ctx.group_name = new_group_name
            reset_bulk_goods_context(ctx)

        if intent.get("order_output_dir"):
            ctx.order_output_dir = normalize_output_dir(intent["order_output_dir"])
        elif ctx.order_output_dir is None:
            ctx.order_output_dir = str(DEFAULT_ORDER_OUTPUT_DIR)

    def update_share_request_from_intent(
        self,
        ctx: SessionToolContext,
        intent: dict[str, Any],
    ) -> None:
        """
        从用户当前这句话中更新均摊参数槽位。
        只更新非空字段，所以“最新明确输入的信息”会覆盖旧信息。
        """
        req = ctx.share_request

        if intent.get("share_mode"):
            req.share_mode = intent["share_mode"]

        new_scope = intent.get("calculation_scope")

        if new_scope:
            if new_scope != req.calculation_scope:
                # 切换拉通/独立模式后，旧的独立配置确认状态不再有效
                req.pending_config_confirmation = False
                req.config_confirmed = False

            req.calculation_scope = new_scope

        if intent.get("amount"):
            req.amount = intent["amount"]

        if intent.get("force"):
            req.force = True


    def handle(
            self,
            session_id: int,
            user_text: str,
    ) -> str | None:
        intent = parse_user_intent(user_text)
        ctx = self.contexts.setdefault(
            session_id,
            SessionToolContext(),
        )

        self.update_context_from_intent(ctx, intent)

        # 只有处于“大货等待确认”状态时，
        # 才把“是”“没问题”等识别成大货确认。
        if ctx.bulk_request.pending_confirmation:
            if has_affirmative_words(user_text):
                return self.handle_confirm_bulk_goods(ctx)

            if has_negative_words(user_text):
                ctx.bulk_request.pending_confirmation = False
                ctx.bulk_request.confirmed = False
                return (
                    "已取消本次大货计算。\n"
                    "请修改或同步订单信息后，重新输入“查大货”或“算大货”。"
                )

        if intent["intent"] == "chat":
            return None

        if intent["intent"] == "set_context":
            return format_context_update_result(ctx)

        if intent["intent"] == "calculate_bulk_goods":
            return self.handle_calculate_bulk_goods(ctx)

        if intent["intent"] == "update_special_members":
            try:
                ctx.special_members = update_special_member_cache(
                    current_members=ctx.special_members,
                    updates=(
                            intent.get("special_member_updates")
                            or []
                    ),
                )
            except SpecialMemberError as exc:
                return f"特殊成员信息设置失败：{exc}"

            # 特殊成员信息改变后，原名单检查结果已经失效。
            ctx.member_checked = False
            ctx.member_check_result = None

            return format_special_members(
                ctx.special_members
            )

        if intent["intent"] == "show_special_members":
            return format_special_members(
                ctx.special_members
            )

        if intent["intent"] == "member_check":
            check_result = self.ensure_member_checked(
                ctx,
                force=True,
            )
            return format_member_check_result(check_result)

        if intent["intent"] == "calculate_share":
            self.update_share_request_from_intent(ctx, intent)
            return self.handle_calculate_share(ctx, intent)

        if intent["intent"] == "update_share_config":
            self.update_share_request_from_intent(ctx, intent)
            return self.handle_update_share_config(ctx, intent)

        if intent["intent"] == "confirm_share_config":
            return self.handle_confirm_share_config(ctx, intent)

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
            ctx.special_members = (
                update_special_member_cache(
                    current_members=ctx.special_members,
                    updates=updates,
                )
            )
        except SpecialMemberError as exc:
            return (
                "特殊成员信息设置失败：\n"
                f"{exc}"
            )

        # 特殊成员发生变化后，旧名单检查结果必须失效。
        ctx.member_checked = False
        ctx.member_check_result = None
        ctx.parsed_order_file = None

        return (
                "特殊成员信息已更新。\n\n"
                + format_special_members(
            ctx.special_members
        )
        )

    def ensure_member_checked(
            self,
            ctx: SessionToolContext,
            force: bool = False,
    ) -> dict[str, Any]:
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

        # 普通订单输出
        result = parse_group_member_orders(
            group_name=ctx.group_name,
            order_input=ctx.new_order_file,
            order_output_dir=ctx.order_output_dir,
            special_members=ctx.special_members,
            key_input_func=self.key_input_func,
        )

        # member_parser 可能根据群昵称和订单补全特殊成员信息。
        resolved_special_members = result.get(
            "special_members"
        )

        if resolved_special_members is not None:
            ctx.special_members = (
                resolved_special_members
            )

        ctx.member_checked = True
        ctx.member_check_result = result
        ctx.parsed_order_file = result.get(
            "parsed_order_file"
        )

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

        # 1. 检查群成员与新订单
        check_result = self.ensure_member_checked(
            ctx,
            force=True,
        )

        if not check_result.get("ok"):
            return format_member_check_result(check_result)

        blocking_issues = get_blocking_member_issues(
            check_result
        )

        if blocking_issues:
            return (
                    "大货计算前发现群成员与订单不一致，"
                    "暂不继续。\n\n"
                    + format_member_check_result(check_result)
                    + "\n\n请修正群昵称序号或订单后，"
                      "重新输入“查大货”。"
            )

        parsed_order_file = (
                check_result.get("parsed_order_file")
                or ctx.parsed_order_file
        )

        if not parsed_order_file:
            return "没有找到订单的简化文件。"

        # 2. 检查订单是否只有不参摊商品
        abnormal_orders = find_only_non_share_orders(
            parsed_order_file
        )

        if abnormal_orders:
            lines = [
                "发现订单异常：以下订单只包含不参摊商品，"
                "暂不继续计算。",
                "",
            ]

            for item in abnormal_orders:
                products = "、".join(item.get("商品") or [])
                lines.append(
                    f"- 单号 {item.get('单号')}｜"
                    f"{item.get('昵称')}｜"
                    f"商品：{products}"
                )

            lines.extend(
                [
                    "",
                    "请检查这些订单是否漏拍了参摊商品，"
                    "或商品参摊规则是否设置正确。",
                ]
            )

            return "\n".join(lines)

        # 3. 所有自动检查通过，进入人工确认
        ctx.bulk_request.pending_confirmation = True
        ctx.bulk_request.confirmed = False

        return (
            "群成员与订单核对完成，订单合规检查通过。\n\n"
            "在生成大货应收结果前，请再次确认：\n"
            "1. 订单内商品价格是否准确？是否有满百减一等单价变化？\n"
            "2. 漏收、补收的均摊是否已经计入订单金额？\n"
            "3. 订单信息是否已经全部同步？商品单价与订单应收金额是否一致？\n\n"
            "以上内容全部确认无误后，请回复“是”。"
        )

    def handle_confirm_bulk_goods(
            self,
            ctx: SessionToolContext,
    ) -> str:
        if not ctx.bulk_request.pending_confirmation:
            return "当前没有等待确认的大货计算。"

        # 用户确认时再强制重新读取一次订单，
        # 防止两次消息之间订单文件被修改。
        check_result = self.ensure_member_checked(
            ctx,
            force=True,
        )

        if not check_result.get("ok"):
            ctx.bulk_request.pending_confirmation = False
            return format_member_check_result(check_result)

        blocking_issues = get_blocking_member_issues(
            check_result
        )

        if blocking_issues:
            ctx.bulk_request.pending_confirmation = False

            return (
                    "确认时重新检查发现群成员或订单已发生变化，"
                    "本次大货计算已停止。\n\n"
                    + format_member_check_result(check_result)
            )

        parsed_order_file = (
                check_result.get("parsed_order_file")
                or ctx.parsed_order_file
        )

        if not parsed_order_file:
            ctx.bulk_request.pending_confirmation = False
            return "没有找到订单的简化文件。"

        abnormal_orders = find_only_non_share_orders(
            parsed_order_file
        )

        if abnormal_orders:
            ctx.bulk_request.pending_confirmation = False

            order_numbers = "、".join(
                str(item.get("单号"))
                for item in abnormal_orders
            )

            return (
                "确认时重新检查发现订单异常，"
                "本次大货计算已停止。\n"
                f"只有不参摊商品的订单号：{order_numbers}"
            )

        result = create_bulk_receivable_orders(
            parsed_order_file=parsed_order_file,
            output_dir=ctx.order_output_dir,
        )

        ctx.bulk_request.pending_confirmation = False
        ctx.bulk_request.confirmed = True

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
        确保当前会话已经有商品均摊配置表，并读取成 product_configs。

        说明：
            - 如果还没有配置表，则根据简化订单表生成配置表。
            - 每次计算前都重新读取配置表，方便用户手动编辑 CSV 后重新计算。
        """
        if not ctx.share_config_file:
            ctx.share_config_file = create_product_share_config_file(
                parsed_order_file=parsed_order_file,
                output_dir=ctx.order_output_dir,
                overwrite=True,
            )

        ctx.product_configs = load_product_share_config_file(
            ctx.share_config_file
        )


    def handle_calculate_share(
            self,
            ctx: SessionToolContext,
            intent: dict[str, Any],
    ) -> str:
        req = ctx.share_request

        if not ctx.group_name:
            return (
                "需要先设置待处理的群聊名称。\n"
                "例如：群聊名称：xxx"
            )

        if not ctx.new_order_file:
            return (
                "需要先设置订单。\n"
                "例如：订单 D:\\xxx\\订单.xlsx"
            )

        if not req.share_mode:
            return (
                "请说明均摊方式：\n"
                "1. 人头摊：例如“按人头”或“人头摊”\n"
                "2. 个数摊：例如“按个数”或“按件数”"
            )

        calculation_scope = req.calculation_scope or "flat"

        if calculation_scope == "flat" and not req.amount:
            return (
                "请补充需要均摊的总金额。\n"
                "例如：金额120\n"
                "也可以直接说：拉通人头，金额120"
            )

        check_result = self.ensure_member_checked(ctx)

        if not check_result.get("ok"):
            return format_member_check_result(check_result)

        blocking_issues = get_blocking_member_issues(check_result)

        if blocking_issues and not req.force:
            return (
                    "计算均摊前发现名单核对问题，暂不计算。\n\n"
                    + format_member_check_result(check_result)
                    + "\n\n如果确认要忽略这些问题继续计算，可以输入：忽略名单问题，继续计算。"
            )

        parsed_order_file = check_result.get("parsed_order_file") or ctx.parsed_order_file

        if not parsed_order_file:
            return "没有找到简化后的订单文件，无法计算均摊。"

        self.ensure_share_config_loaded(ctx, parsed_order_file)

        if calculation_scope == "independent" and req.pending_config_confirmation:
            return (
                "商品独立均摊配置还未确认。\n"
                "确认无误后请输入：确认计算"
            )

        result = calculate_share(
            parsed_order_file=parsed_order_file,
            total_amount=req.amount,
            share_mode=req.share_mode,
            calculation_scope=calculation_scope,
            product_configs=ctx.product_configs,
            output_dir=ctx.order_output_dir,
            excluded_order_nos=get_non_share_order_nos(
                ctx.special_members
            ),
        )

        if not result.get("ok") and result.get("need_user_input"):
            return format_share_need_user_input(result)

        return format_share_result(
            result=result,
            group_name=ctx.group_name,
            member_check_result=check_result,
            special_members=ctx.special_members,
        )


    def handle_update_share_config(
        self,
        ctx: SessionToolContext,
        intent: dict[str, Any],
    ) -> str:
        req = ctx.share_request

        if not ctx.group_name:
            return "需要先设置待处理的群聊名称。例如：群聊名称：xxx"

        if not ctx.new_order_file:
            return "需要先设置订单。例如：订单 订单1.xlsx"

        calculation_scope = req.calculation_scope or intent.get("calculation_scope")

        if calculation_scope != "independent":
            return (
                "检测到你输入了各商品均摊金额，但当前不是独立均摊模式。\n"
                "请先输入：按人头独立 或 按个数独立。"
            )

        if not req.share_mode:
            return (
                "请先说明独立均摊方式：\n"
                "1. 按人头独立\n"
                "2. 按个数独立"
            )

        check_result = self.ensure_member_checked(ctx)

        if not check_result.get("ok"):
            return format_member_check_result(check_result)

        parsed_order_file = check_result.get("parsed_order_file") or ctx.parsed_order_file

        if not parsed_order_file:
            return "没有找到简化后的订单文件，无法写入商品均摊配置表。"

        self.ensure_share_config_loaded(ctx, parsed_order_file)

        share_type = f"{req.share_mode}_independent"

        update_result = update_product_share_config_file(
            config_file=ctx.share_config_file,
            updates=intent.get("product_share_amounts") or [],
            share_type=share_type,
        )

        ctx.product_configs = load_product_share_config_file(ctx.share_config_file)

        summary = summarize_product_share_config(
            config_file=ctx.share_config_file,
            total_amount=req.amount,
        )

        req.pending_config_confirmation = True
        req.config_confirmed = False

        return format_product_share_config_confirmation(
            summary=summary,
            updated_items=update_result.get("updated_items") or [],
            unmatched_updates=update_result.get("unmatched_updates") or [],
        )


    def handle_confirm_share_config(
        self,
        ctx: SessionToolContext,
        intent: dict[str, Any],
    ) -> str:
        req = ctx.share_request

        if not req.pending_config_confirmation:
            return "当前没有待确认的商品独立均摊配置。"

        if not ctx.share_config_file:
            return "没有找到商品均摊配置表，无法确认。"

        summary = summarize_product_share_config(
            config_file=ctx.share_config_file,
            total_amount=req.amount,
        )

        if summary.get("matched") is False:
            return (
                "各商品均摊合计与用户输入的总均摊不一致，暂不计算。\n\n"
                + format_product_share_config_confirmation(
                    summary=summary,
                    updated_items=[],
                    unmatched_updates=[],
                )
                + "\n\n请修改各商品均摊，或重新输入正确的总均摊。"
            )

        req.config_confirmed = True
        req.pending_config_confirmation = False

        return self.handle_calculate_share(ctx, intent)


def format_special_members(
    special_members: list[dict[str, Any]],
) -> str:
    if not special_members:
        return "当前没有设置特殊成员。"

    role_order = {
        "车主": 0,
        "画师": 1,
        "章稿画师": 2,
        "供稿人": 3,
        "工具人": 4,
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
                format_special_members(
                    current_members
                )
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

    members_without_serial = result.get("members_without_serial") or []
    duplicate_member_serials = result.get("duplicate_member_serials") or []
    serials_in_group_not_in_orders = result.get("serials_in_group_not_in_orders") or []
    serials_in_orders_not_in_group = result.get("serials_in_orders_not_in_group") or []

    lines.append("")
    lines.append(f"群昵称前没有数字的成员数量：{len(members_without_serial)}")
    if members_without_serial:
        for member in members_without_serial:
            lines.append(f"- {member.get('群昵称') or member.get('昵称') or member.get('wxid')}")

    lines.append("")
    lines.append(f"群昵称中重复标注的序号数量：{len(duplicate_member_serials)}")
    if duplicate_member_serials:
        for item in duplicate_member_serials:
            serial = item.get("序号")
            members = item.get("members") or []
            names = "、".join(
                str(m.get("群昵称") or m.get("昵称") or m.get("wxid"))
                for m in members
            )
            lines.append(f"- 序号 {serial}：{names}")

    lines.append("")
    lines.append("群昵称有、但是订单没有的序号：")
    lines.append(str(serials_in_group_not_in_orders))

    lines.append("")
    lines.append("订单里有、但是群昵称没有的序号：")
    lines.append(str(serials_in_orders_not_in_group))

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

    lines.append(f"均摊方式：{result['share_mode_text']}")
    lines.append(f"计算方式：{result['calculation_scope_text']}")
    lines.append(f"原始均摊金额：{result['total_amount']}")

    if result.get("share_mode") == "quantity":
        total_share_quantity = result.get("total_share_quantity")

        if total_share_quantity is not None:
            lines.append(f"总参摊个数：{total_share_quantity}")
        else:
            lines.append("总参摊个数：未统计")

    lines.append(f"参与人数：{result['participant_count']}")

    lines.append(
        "不参摊说明："
        + format_non_share_special_member_note(special_members or [])
    )

    # 拉通个数摊：显示单个参摊商品需均摊。
    if (
        result.get("share_mode") == "quantity"
        and result.get("calculation_scope") == "flat"
        and result.get("unit_share_amount") is not None
    ):
        lines.append(
            "单个参摊商品需均摊："
            f"{result['unit_share_amount']} 元"
        )

    # 拉通人头摊：可显示单人需均摊。
    if (
        result.get("share_mode") == "head"
        and result.get("calculation_scope") == "flat"
        and result.get("unit_share_amount") is not None
    ):
        lines.append(
            "单人需均摊："
            f"{result['unit_share_amount']} 元"
        )

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


def format_product_share_config_confirmation(
    summary: dict[str, Any],
    updated_items: list[dict[str, Any]],
    unmatched_updates: list[dict[str, Any]],
) -> str:
    lines: list[str] = []

    lines.append("已写入商品独立均摊配置，请确认。")
    lines.append(f"配置文件：{summary.get('config_file')}")

    if updated_items:
        lines.append("")
        lines.append("本次写入：")
        for item in updated_items:
            lines.append(
                f"- {item.get('商品序号')}｜"
                f"{item.get('商品名称')}｜"
                f"商品数量：{item.get('商品数量')}｜"
                f"计入均摊：{item.get('计入均摊')}｜"
                f"均摊类型：{item.get('均摊类型')}｜"
                f"商品均摊：{item.get('商品均摊')}"
            )

    if unmatched_updates:
        lines.append("")
        lines.append("以下输入未匹配到配置表商品：")
        for item in unmatched_updates:
            lines.append(f"- {item}")

    items = summary.get("items") or []

    included_items = [
        item for item in items
        if item.get("计入均摊") and item.get("商品均摊")
    ]

    if included_items:
        lines.append("")
        lines.append("当前各商品均摊：")
        for item in included_items:
            lines.append(
                f"- {item.get('商品序号')}｜"
                f"{item.get('商品名称')}｜"
                f"商品均摊：{item.get('商品均摊')}"
            )

    lines.append("")
    lines.append(f"各商品均摊合计：{summary.get('config_total')}")

    expected_total = summary.get("expected_total")
    matched = summary.get("matched")

    if expected_total:
        lines.append(f"用户输入总均摊：{expected_total}")
        lines.append(f"差额：{summary.get('diff')}")

        if matched:
            lines.append("校验结果：各商品均摊合计与总均摊一致。")
        else:
            lines.append("校验结果：各商品均摊合计与总均摊不一致，请检查。")
    else:
        lines.append("用户未输入总均摊，将以各商品均摊合计作为总均摊。")

    lines.append("")
    lines.append("确认无误后请输入：确认计算")

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
    lines.append(f"新订单：{ctx.new_order_file or '未设置'}")
    lines.append(f"旧订单：{ctx.old_order_file or '未设置'}")
    lines.append(f"缓存1：{ctx.order_cache_1_file or '未设置'}")
    lines.append(f"缓存2：{ctx.order_cache_2_file or '未设置'}")
    lines.append(f"输出目录：{ctx.order_output_dir or '未设置'}")

    return "\n".join(lines)


def normalize_output_dir(value: str | Path | None) -> str:
    """
    规范化输出目录。

    规则：
        1. 用户没填 → ./orders/output
        2. 用户填了绝对路径 → 原样返回
        3. 用户填了相对路径 → 原样作为相对路径使用
    """
    if value is None or str(value).strip() == "":
        return str(DEFAULT_ORDER_OUTPUT_DIR)

    path = Path(str(value).strip().strip('"').strip("'"))
    return str(path)