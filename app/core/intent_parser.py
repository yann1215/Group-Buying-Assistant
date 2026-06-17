# app/core/intent_parser.py

from __future__ import annotations

import re
from typing import Any


def parse_user_intent(user_text: str) -> dict[str, Any]:
    """
    解析用户意图。

    返回：
        {
            "intent": "calculate_share" | "member_check" | "chat",
            "share_mode": "head" | "quantity" | None,
            "amount": "120" | None,
            "force": bool
        }
    """
    text = str(user_text or "").strip()

    if not text:
        return {
            "intent": "chat",
            "share_mode": None,
            "amount": None,
            "force": False,
        }

    force = has_force_words(text)

    if has_member_check_words(text):
        return {
            "intent": "member_check",
            "share_mode": None,
            "amount": None,
            "force": force,
        }

    if has_share_words(text):
        return {
            "intent": "calculate_share",
            "share_mode": parse_share_mode(text),
            "calculation_scope": parse_calculation_scope(text),
            "amount": parse_amount(text),
            "force": force,
        }

    return {
        "intent": "chat",
        "share_mode": None,
        "amount": None,
        "force": force,
    }


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


def parse_calculation_scope(text: str) -> str:
    flat_words = [
        "拉通",
        "拉通摊",
        "整体摊",
        "统一摊",
    ]

    independent_words = [
        "独立",
        "独立摊",
        "单独摊",
        "分别摊",
        "每个商品单独",
    ]

    if any(word in text for word in independent_words):
        return "independent"

    if any(word in text for word in flat_words):
        return "flat"

    # 未说明时，第一版建议默认拉通
    return "flat"


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
    head_words = [
        "人头摊",
        "按人头",
        "按人",
        "每人",
        "平均到人",
        "人均",
    ]

    quantity_words = [
        "个数摊",
        "按个数",
        "按数量",
        "按件",
        "按件数",
        "按商品数",
    ]

    if any(word in text for word in quantity_words):
        return "quantity"

    if any(word in text for word in head_words):
        return "head"

    return None


def parse_amount(text: str) -> str | None:
    """
    从用户输入中提取金额。

    支持：
        金额120
        120元
        ￥120.5
        ¥120.50
        总额 120
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

    return None