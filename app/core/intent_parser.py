# app/core/intent_parser.py

from __future__ import annotations

import re
from typing import Any


def parse_user_intent(user_text: str) -> dict[str, Any]:
    """
    解析用户输入中的动作和槽位。

    返回：
        {
            "intent": "calculate_share" | "member_check" | "set_context" | "chat",
            "share_mode": "head" | "quantity" | None,
            "calculation_scope": "flat" | "independent" | None,
            "amount": "120" | None,
            "group_name": str | None,
            "order_input": str | None,
            "order_output_dir": str | None,
            "force": bool,
        }
    """
    text = str(user_text or "").strip()

    base = {
        "intent": "chat",
        "share_mode": None,
        "calculation_scope": None,
        "amount": None,
        "group_name": None,
        "order_input": None,
        "order_output_dir": None,
        "force": False,
    }

    if not text:
        return base

    share_mode = parse_share_mode(text)
    calculation_scope = parse_calculation_scope(text)
    amount = parse_amount(text)
    group_name = parse_group_name(text)
    order_input = parse_order_input(text)
    order_output_dir = parse_order_output_dir(text)
    force = has_force_words(text)

    base.update(
        {
            "share_mode": share_mode,
            "calculation_scope": calculation_scope,
            "amount": amount,
            "group_name": group_name,
            "order_input": order_input,
            "order_output_dir": order_output_dir,
            "force": force,
        }
    )

    if has_member_check_words(text):
        base["intent"] = "member_check"
        return base

    # 有明确均摊动作词，或出现了均摊相关槽位，都认为是在继续/发起均摊任务。
    if has_share_words(text) or share_mode or calculation_scope or amount:
        base["intent"] = "calculate_share"
        return base

    # 只提供群聊名称、订单文件、输入目录、输出目录时，更新上下文。
    if group_name or order_input or order_output_dir:
        base["intent"] = "set_context"
        return base

    return base


def has_share_words(text: str) -> bool:
    keywords = [
        "均摊",
        "分摊",
        "摊钱",
        "摊一下",
        "查均摊",
        "计算均摊",
        "算均摊",
        "算一下均摊",
    ]
    return any(word in text for word in keywords)


def has_member_check_words(text: str) -> bool:
    keywords = [
        "核对成员",
        "核对名单",
        "检查成员",
        "检查名单",
        "查成员",
        "查名单",
        "比对成员",
        "比对名单",
    ]
    return any(word in text for word in keywords)


def has_force_words(text: str) -> bool:
    keywords = [
        "忽略问题",
        "忽略名单问题",
        "继续计算",
        "强制计算",
        "先算",
        "不管",
    ]
    return any(word in text for word in keywords)


def parse_share_mode(text: str) -> str | None:
    """
    识别人头摊 / 个数摊。

    顺序无关：
        拉通人头
        人头拉通
        金额120，按人头拉通
    都可以识别出 head。
    """
    head_words = [
        "人头摊",
        "按人头",
        "人头",
        "按人",
        "每人",
        "平均到人",
        "人均",
    ]

    quantity_words = [
        "个数摊",
        "按个数",
        "个数",
        "按数量",
        "按件",
        "按件数",
        "按商品数",
        "数量摊",
        "件数摊",
    ]

    if any(word in text for word in quantity_words):
        return "quantity"

    if any(word in text for word in head_words):
        return "head"

    return None


def parse_calculation_scope(text: str) -> str | None:
    """
    识别拉通 / 独立。

    注意：
        这里不要默认返回 flat。
        默认 flat 应该放在 tool_orchestrator.py 里处理，
        否则用户后续只输入“金额120”时，会把之前的 independent 覆盖成 flat。
    """
    flat_words = [
        "拉通",
        "拉通摊",
        "整体摊",
        "统一摊",
        "整体",
        "统一",
    ]

    independent_words = [
        "独立",
        "独立摊",
        "单独摊",
        "分别摊",
        "每个商品单独",
        "单独",
        "分别",
    ]

    if any(word in text for word in independent_words):
        return "independent"

    if any(word in text for word in flat_words):
        return "flat"

    return None


def parse_amount(text: str) -> str | None:
    """
    从用户输入中提取金额。

    支持：
        金额120
        金额：120
        总额 120
        费用 120
        邮费 120
        手续费 120
        均摊金额 120
        ￥120
        ¥120.50
        120元
        120
    """
    patterns = [
        r"(?:金额|总额|费用|邮费|手续费|均摊金额)\s*[:：]?\s*[￥¥]?\s*(\d+(?:\.\d{1,2})?)",
        r"[￥¥]\s*(\d+(?:\.\d{1,2})?)",
        r"(\d+(?:\.\d{1,2})?)\s*元",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    # 支持用户在多轮对话中只输入：120
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return text

    return None


def parse_group_name(text: str) -> str | None:
    """
    支持：
        群聊：xxx
        群名：xxx
        群聊名称是xxx
        当前群聊 xxx
    """
    patterns = [
        r"(?:群聊名称|群聊|群名|当前群聊)\s*(?:是|为|=|：|:)?\s*([^，,。；;\n]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value:
                return value

    return None


def parse_order_input(text: str) -> str | None:
    patterns = [
        r"(?:当前订单|订单文件|订单文件|订单表|订单路径|输入文件|输入目录|订单目录)\s*(?:是|为|=|：|:)?\s*([^，,。；;\n]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            if value:
                return value

    return None


def parse_order_output_dir(text: str) -> str | None:
    patterns = [
        r"(?:输出目录|保存目录|结果目录)\s*(?:是|为|=|：|:)?\s*([^，,。；;\n]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
            if value:
                return value

    return None