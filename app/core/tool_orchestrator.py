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
from typing import Any

from app.analysis.member_parser import parse_group_member_orders
from app.analysis.share_calculator import calculate_share
from app.analysis.share_config import (
    create_product_share_config_file,
    load_product_share_config_file,
)
from app.core.intent_parser import parse_user_intent


DEFAULT_ORDER_INPUT_DIR = Path("./orders")
DEFAULT_ORDER_OUTPUT_DIR = Path("./orders/output")


@dataclass
class ShareRequestState:
    share_mode: str | None = None
    calculation_scope: str | None = None
    amount: str | None = None
    force: bool = False


@dataclass
class SessionToolContext:
    group_name: str | None = None
    order_input: str | Path | dict[str, Any] | None = None
    order_output_dir: str | Path | None = None

    member_checked: bool = False
    member_check_result: dict[str, Any] | None = None
    parsed_order_file: str | None = None

    share_config_file: str | None = None
    product_configs: list[dict[str, Any]] | None = None

    share_request: ShareRequestState = field(default_factory=ShareRequestState)


class ToolOrchestrator:
    def __init__(self):
        self.contexts: dict[int, SessionToolContext] = {}

    def set_context(
        self,
        session_id: int,
        group_name: str | None = None,
        order_input: str | Path | dict[str, Any] | None = None,
        order_output_dir: str | Path | None = None,
    ) -> None:
        ctx = self.contexts.setdefault(session_id, SessionToolContext())

        if group_name is not None:
            ctx.group_name = group_name

        if order_input is not None:
            ctx.order_input = normalize_order_input_path(order_input)
            ctx.member_checked = False
            ctx.member_check_result = None
            ctx.parsed_order_file = None

            # 如果已经加了配置表缓存，也一起清空
            if hasattr(ctx, "share_config_file"):
                ctx.share_config_file = None
            if hasattr(ctx, "product_configs"):
                ctx.product_configs = None

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
            ctx.group_name = intent["group_name"]

        if intent.get("order_input"):
            ctx.order_input = normalize_order_input_path(intent["order_input"])

            ctx.member_checked = False
            ctx.member_check_result = None
            ctx.parsed_order_file = None
            ctx.share_config_file = None
            ctx.product_configs = None

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

        if intent.get("calculation_scope"):
            req.calculation_scope = intent["calculation_scope"]

        if intent.get("amount"):
            req.amount = intent["amount"]

        if intent.get("force"):
            req.force = True


    def handle(self, session_id: int, user_text: str) -> str | None:
        """
        如果用户输入需要调用工具，则返回工具结果文本。
        如果不需要调用工具，则返回 None，让 ChatService 继续走普通 LLM 对话。
        """
        intent = parse_user_intent(user_text)
        ctx = self.contexts.setdefault(session_id, SessionToolContext())

        self.update_context_from_intent(ctx, intent)

        if intent["intent"] == "chat":
            return None

        if intent["intent"] == "set_context":
            return format_context_update_result(ctx)

        if intent["intent"] == "member_check":
            check_result = self.ensure_member_checked(ctx, force=True)
            return format_member_check_result(check_result)

        if intent["intent"] == "calculate_share":
            self.update_share_request_from_intent(ctx, intent)
            return self.handle_calculate_share(ctx, intent)

        return None

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

        if not ctx.order_input:
            return (
                "需要先设置当前订单文件或输入目录。\n"
                "例如：订单文件：D:\\xxx\\订单.xlsx\n"
                "或：输入目录：D:\\xxx\\input"
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

        result = calculate_share(
            parsed_order_file=parsed_order_file,
            total_amount=req.amount,
            share_mode=req.share_mode,
            calculation_scope=calculation_scope,
            product_configs=ctx.product_configs,
            output_dir=ctx.order_output_dir,
        )

        if not result.get("ok") and result.get("need_user_input"):
            return format_share_need_user_input(result)

        return format_share_result(result)


    def ensure_member_checked(
        self,
        ctx: SessionToolContext,
        force: bool = False,
    ) -> dict[str, Any]:
        if ctx.member_checked and ctx.member_check_result and not force:
            return ctx.member_check_result

        if not ctx.group_name:
            return {
                "ok": False,
                "message": "缺少群聊名称。",
            }

        if not ctx.order_input:
            return {
                "ok": False,
                "message": "缺少订单文件。",
            }

        result = parse_group_member_orders(
            group_name=ctx.group_name,
            order_input=ctx.order_input,
            order_output_dir=ctx.order_output_dir,
        )

        ctx.member_checked = True
        ctx.member_check_result = result
        ctx.parsed_order_file = result.get("parsed_order_file")

        if result.get("ok") and ctx.parsed_order_file:
            self.ensure_share_config_loaded(
                ctx=ctx,
                parsed_order_file=ctx.parsed_order_file,
            )

        return result


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
                overwrite=False,
            )

        ctx.product_configs = load_product_share_config_file(
            ctx.share_config_file
        )


def get_blocking_member_issues(result: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    if not result.get("ok"):
        issues.append("成员核对失败")

    if result.get("duplicate_member_serials"):
        issues.append("群昵称中存在重复标注的序号")

    if result.get("serials_in_group_not_in_orders"):
        issues.append("群昵称有、但是订单没有的序号")

    if result.get("serials_in_orders_not_in_group"):
        issues.append("订单里有、但是群昵称没有的序号")

    return issues


def format_member_check_result(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"成员与订单核对失败：{result.get('message')}"

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


def format_share_result(result: dict) -> str:
    if not result.get("ok"):
        if result.get("need_user_input"):
            return format_share_need_user_input(result)
        return result.get("message", "均摊计算失败。")

    lines = []

    lines.append("均摊计算完成。")
    lines.append(f"均摊方式：{result['share_mode_text']}")
    lines.append(f"计算方式：{result['calculation_scope_text']}")
    lines.append(f"原始均摊金额：{result['total_amount']}")
    lines.append(f"实际总收款：{result['total_collected']}")
    lines.append(f"向上取整多收：{result['over_collected']}")
    lines.append(f"参与人数：{result['participant_count']}")
    lines.append(f"结果文件：{result['result_file']}")

    lines.append("")
    lines.append("前 10 条结果预览：")

    for item in result["items"][:10]:
        lines.append(
            f"- {item['单号']}｜{item['昵称']}｜"
            f"商品总数 {item['商品总数']}｜应收 {item['应收金额']}"
        )

    if len(result["items"]) > 10:
        lines.append(f"... 其余 {len(result['items']) - 10} 条见结果文件。")

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


def format_context_update_result(ctx: SessionToolContext) -> str:
    lines = ["已更新当前处理上下文。"]

    lines.append(f"群聊名称：{ctx.group_name or '未设置'}")
    lines.append(f"订单文件/输入目录：{ctx.order_input or '未设置'}")
    lines.append(f"输出目录：{ctx.order_output_dir or '未设置'}")

    return "\n".join(lines)


def normalize_order_input_path(value: str | Path | dict[str, Any]) -> str | Path | dict[str, Any]:
    """
    规范化订单输入路径。

    规则：
        1. 如果是 dict，尝试规范化其中的 file_path / order_file / path。
        2. 如果是绝对路径，原样返回。
        3. 如果只是文件名，例如“订单1.xlsx”，默认变成 ./orders/订单1.xlsx。
        4. 如果是相对路径但已经包含目录，例如 orders/订单1.xlsx，则原样返回。
    """
    if isinstance(value, dict):
        result = dict(value)

        for key in ("file_path", "order_file", "path"):
            if result.get(key):
                result[key] = normalize_order_input_path(result[key])
                break

        return result

    path = Path(str(value).strip().strip('"').strip("'"))

    # 绝对路径：D:\xxx\订单1.xlsx 或 /xxx/订单1.xlsx
    if path.is_absolute():
        return str(path)

    # 只有文件名，没有目录，例如：订单1.xlsx
    if path.parent == Path(".") and path.suffix.lower() in {".xlsx", ".xlsm"}:
        return str(DEFAULT_ORDER_INPUT_DIR / path)

    # 已经包含相对目录，例如：orders/订单1.xlsx
    return str(path)


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