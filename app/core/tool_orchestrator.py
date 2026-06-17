# app/core/tool_orchestrator.py

"""

1. 判断当前 session 有没有群聊名称和订单文件
2. 如果没核对过，先调用 parse_group_member_orders()
3. 如果名单有严重问题，先返回问题，不计算
4. 如果可以计算，再调用 calculate_share()

"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.analysis.member_parser import parse_group_member_orders
from app.analysis.share_calculator import calculate_share
from app.core.intent_parser import parse_user_intent


@dataclass
class SessionToolContext:
    group_name: str | None = None
    order_input: str | Path | dict[str, Any] | None = None
    order_output_dir: str | Path | None = None

    member_checked: bool = False
    member_check_result: dict[str, Any] | None = None
    parsed_order_file: str | None = None


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
            ctx.order_input = order_input
            ctx.member_checked = False
            ctx.member_check_result = None
            ctx.parsed_order_file = None

        if order_output_dir is not None:
            ctx.order_output_dir = order_output_dir

    def handle(self, session_id: int, user_text: str) -> str | None:
        """
        如果用户输入需要调用工具，则返回工具结果文本。
        如果不需要调用工具，则返回 None，让 ChatService 继续走普通 LLM 对话。
        """
        intent = parse_user_intent(user_text)

        if intent["intent"] == "chat":
            return None

        ctx = self.contexts.setdefault(session_id, SessionToolContext())

        if intent["intent"] == "member_check":
            check_result = self.ensure_member_checked(ctx, force=True)
            return format_member_check_result(check_result)

        if intent["intent"] == "calculate_share":
            return self.handle_calculate_share(ctx, intent)

        return None

    def handle_calculate_share(
        self,
        ctx: SessionToolContext,
        intent: dict[str, Any],
    ) -> str:
        if not ctx.group_name:
            return "需要先设置待处理的群聊名称。"

        if not ctx.order_input:
            return "需要先设置当前订单文件路径。"

        if not intent.get("share_mode"):
            return (
                "请说明均摊方式：\n"
                "1. 人头摊：每个有订单的人平均分摊\n"
                "2. 个数摊：按每个人的商品总件数分摊"
            )

        if not intent.get("amount"):
            return "请补充需要均摊的总金额，例如：计算均摊，金额 120，按人头摊。"

        check_result = self.ensure_member_checked(ctx)

        if not check_result.get("ok"):
            return format_member_check_result(check_result)

        blocking_issues = get_blocking_member_issues(check_result)

        if blocking_issues and not intent.get("force"):
            return (
                "计算均摊前发现名单核对问题，暂不计算。\n\n"
                + format_member_check_result(check_result)
                + "\n\n如果确认要忽略这些问题继续计算，可以输入：忽略名单问题，继续计算均摊，金额 xxx，按人头摊/个数摊。"
            )

        parsed_order_file = check_result.get("parsed_order_file")

        if not parsed_order_file:
            return "没有找到简化后的订单文件，无法计算均摊。"

        result = calculate_share(
            parsed_order_file=parsed_order_file,
            total_amount=intent.get("amount"),
            share_mode=intent["share_mode"],
            calculation_scope=intent.get("calculation_scope", "flat"),
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

        return result


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
                f"- {cfg.get('商品名称')}｜"
                f"商品数量：{cfg.get('商品数量')}｜"
                f"参摊：{cfg.get('参摊')}｜"
                f"均摊类型：{cfg.get('均摊类型')}｜"
                f"商品均摊：{cfg.get('商品均摊') or '未填写'}｜"
                f"{cfg.get('说明') or ''}"
            )

    return "\n".join(lines)