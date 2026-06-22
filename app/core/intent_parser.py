# app/core/intent_parser.py

from __future__ import annotations

import re
from typing import Any


# 不允许被识别成商品名称的字段词
RESERVED_PRODUCT_NAMES = {
    "金额",
    "总额",
    "总金额",
    "总均摊",
    "总均摊金额",
    "均摊金额",
    "费用",
    "邮费",
    "手续费",
    "群聊",
    "群名",
    "群聊名称",
    "订单",
    "订单文件",
    "订单表",
    "订单路径",
    "输入文件",
    "输入目录",
    "输出目录",
    "保存目录",
    "结果目录",
}


def parse_user_intent(user_text: str) -> dict[str, Any]:
    """
    从用户当前这一句话中提取动作和参数槽位。

    注意：
        这里只提取当前消息明确出现的信息。
        多轮对话中的旧信息保留、最新信息覆盖，应由
        tool_orchestrator.py 的 SessionToolContext 负责。

    返回示例：
        {
            "intent": (
                "calculate_share"
                | "update_share_config"
                | "confirm_share_config"
                | "member_check"
                | "set_context"
                | "chat"
            ),
            "share_mode": "head" | "quantity" | None,
            "calculation_scope": "flat" | "independent" | None,
            "amount": "120.00" | None,
            "product_share_amounts": [
                {
                    "商品序号": 1,
                    "商品均摊": "10"
                },
                {
                    "商品名称": "小猫徽章",
                    "商品均摊": "20"
                }
            ],
            "group_name": str | None,
            "order_input": str | None,
            "order_output_dir": str | None,
            "force": bool,
            "confirm": bool,
        }
    """
    text = normalize_text(user_text)

    result: dict[str, Any] = {
        "intent": "chat",
        "share_mode": None,
        "calculation_scope": None,
        "amount": None,
        "product_share_amounts": [],
        "group_name": None,
        "order_input": None,
        "order_output_dir": None,
        "force": False,
        "confirm": False,
    }

    if not text:
        return result

    # 必须先解析各商品金额，再解析总金额。
    # 否则“1号10元，2号20元”可能把 10元 误认成总金额。
    product_share_amounts = parse_product_share_amounts(text)

    share_mode = parse_share_mode(text)
    calculation_scope = parse_calculation_scope(text)

    amount = parse_amount(
        text=text,
        allow_unlabeled_amount=not bool(product_share_amounts),
    )

    group_name = parse_group_name(text)
    order_input = parse_order_input(text)
    order_output_dir = parse_order_output_dir(text)

    force = has_force_words(text)
    confirm = has_confirm_words(text)

    result.update(
        {
            "share_mode": share_mode,
            "calculation_scope": calculation_scope,
            "amount": amount,
            "product_share_amounts": product_share_amounts,
            "group_name": group_name,
            "order_input": order_input,
            "order_output_dir": order_output_dir,
            "force": force,
            "confirm": confirm,
        }
    )

    # 意图优先级很重要。
    #
    # 1. 确认配置
    # 2. 更新商品独立均摊配置
    # 3. 核对成员
    # 4. 发起/补充均摊参数，或强制继续计算均摊
    # 5. 更新群聊、订单、输出目录
    # 6. 普通聊天

    if confirm:
        result["intent"] = "confirm_share_config"
        return result

    if product_share_amounts:
        result["intent"] = "update_share_config"
        return result

    if has_member_check_words(text):
        result["intent"] = "member_check"
        return result

    # 即使没有重新输入金额和均摊方式，
    # “先算”“继续算”“忽略名单问题”等 force 指令，
    # 也应当进入当前会话已有的均摊任务。
    if (
        has_share_words(text)
        or share_mode is not None
        or calculation_scope is not None
        or amount is not None
        or force
    ):
        result["intent"] = "calculate_share"
        return result

    if group_name or order_input or order_output_dir:
        result["intent"] = "set_context"
        return result

    return result


def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


# ----------------------------------------------------------------------
# 均摊动作
# ----------------------------------------------------------------------

def has_share_words(text: str) -> bool:
    """
    判断用户是否正在要求进行均摊计算。

    除了“计算均摊”这类完整说法，也需要识别：
    个数摊 ...
    人头摊 ...
    拉通个数 ...
    个数拉通 ...
    独立个数
    """
    keywords = [
        # 通用均摊词
        "均摊",
        "分摊",
        "摊钱",
        "摊一下",
        "查均摊",
        "计算均摊",
        "算均摊",
        "算一下均摊",

        # 人头摊
        "人头摊",
        "按人头",
        "人头拉通",
        "拉通人头",
        "人头独立",
        "独立人头",

        # 个数摊
        "个数摊",
        "按个数",
        "按数量",
        "按件数",
        "个数拉通",
        "拉通个数",
        "个数独立",
        "独立个数",
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
    """
    用于忽略名单核对问题，强制继续计算。

    不要把普通“确认计算”当成 force。
    """
    keywords = [
        "先算",
        "继续算",
        "忽略问题",
        "无视问题",
        "忽略名单",
        "忽略名单问题",
        "无视名单",
        "忽略核对问题",
        "强制计算",
        "不管名单",
        "跳过名单核对",
        "忽略后继续",
    ]

    return any(word in text for word in keywords)


def has_confirm_words(text: str) -> bool:
    """
    识别用户对商品独立均摊配置的确认。

    不建议仅使用“确认”作为包含式关键词，
    否则“需要确认一下”也可能被误判。
    """
    normalized = re.sub(r"\s+", "", text)

    exact_words = {
        "确认",
        "确认计算",
        "确认均摊",
        "配置确认",
        "确认配置",
        "确认无误",
        "没问题",
        "没问题计算",
        "可以计算",
        "开始计算",
        "按这个计算",
        "就这样计算",
    }

    if normalized in exact_words:
        return True

    patterns = [
        r"确认.{0,6}(?:计算|均摊|配置)",
        r"(?:配置|均摊).{0,6}确认",
        r"(?:没问题|无误).{0,6}(?:开始)?计算",
        r"按(?:这个|以上|当前配置).{0,6}计算",
    ]

    return any(re.search(pattern, normalized) for pattern in patterns)


# ----------------------------------------------------------------------
# 人头摊 / 个数摊
# ----------------------------------------------------------------------

def parse_share_mode(text: str) -> str | None:
    """
    提取人头摊或个数摊。

    与关键词顺序无关，例如：
        拉通人头
        人头拉通
        金额120，按人头拉通
    都会提取为 head。
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

    has_head = any(word in text for word in head_words)
    has_quantity = any(word in text for word in quantity_words)

    if has_head and has_quantity:
        # 不要静默选择其中一个，否则可能造成账目错误。
        # 当前返回 None，让 orchestrator 提示用户明确选择。
        return None

    if has_quantity:
        return "quantity"

    if has_head:
        return "head"

    return None


# ----------------------------------------------------------------------
# 拉通 / 独立
# ----------------------------------------------------------------------

def parse_calculation_scope(text: str) -> str | None:
    """
    提取拉通或独立。

    规则：
    1. 明确出现“独立”时，返回 independent。
    2. 明确出现“拉通”时，返回 flat。
    3. 只说“人头摊”或“个数摊”时，默认理解为拉通。
    4. 只说“按人头”或“按个数”时，不修改已有计算范围。
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
        "每款单独",
        "各款单独",
        "单独",
        "分别",
    ]

    has_flat = any(word in text for word in flat_words)
    has_independent = any(word in text for word in independent_words)

    # 同时出现两种互相冲突的范围，不静默选择
    if has_flat and has_independent:
        return None

    # 明确范围优先
    if has_independent:
        return "independent"

    if has_flat:
        return "flat"

    # “人头摊”“个数摊”作为拉通均摊的简写
    default_flat_words = [
        "人头摊",
        "个数摊",
        "数量摊",
        "件数摊",
    ]

    if any(word in text for word in default_flat_words):
        return "flat"

    # 没有明确范围时不覆盖会话中的旧值
    return None


# ----------------------------------------------------------------------
# 总均摊金额
# ----------------------------------------------------------------------

def parse_amount(
    text: str,
    allow_unlabeled_amount: bool = True,
) -> str | None:
    """
    提取总均摊金额。

    明确标注格式：
        总均摊30
        总均摊金额：30
        总金额30
        金额30
        总额30
        均摊金额30
        ￥30
        ¥30.50

    在没有商品独立金额时，还支持：
        30元
        30
        个数摊 517
        拉通个数 517
        按个数拉通 517
        人头摊 300

    当一句话里已经识别到商品独立均摊金额时，
    allow_unlabeled_amount=False，避免把：
        1号10元，2号20元
    中的10元误识别为总均摊。
    """
    text = text.strip()

    explicit_patterns = [
        (
            r"(?:总均摊金额|总均摊|均摊总额|均摊金额|"
            r"总金额|金额合计|合计金额|总额|金额)"
            r"\s*(?:是|为|=|：|:)?\s*[￥¥]?"
            r"\s*(\d+(?:\.\d{1,4})?)"
        ),
        r"[￥¥]\s*(\d+(?:\.\d{1,4})?)",
    ]

    # 1. 优先识别明确标注的总金额
    for pattern in explicit_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    # 已识别到商品独立均摊金额时，
    # 不再尝试解析无标签总金额。
    if not allow_unlabeled_amount:
        return None

    # 2. 识别“30元”
    yuan_match = re.search(
        r"(?<![\d.])(\d+(?:\.\d{1,4})?)\s*元",
        text,
    )
    if yuan_match:
        return yuan_match.group(1)

    # 3. 识别“均摊模式 + 裸金额”
    #
    # 例如：
    # 个数摊 517
    # 拉通个数：517
    # 按个数拉通，517
    # 人头摊为300
    share_mode_amount_patterns = [
        (
            r"(?:"
            r"按个数拉通|按数量拉通|按件数拉通|"
            r"拉通个数|拉通数量|拉通件数|"
            r"个数拉通|数量拉通|件数拉通|"
            r"个数摊|数量摊|件数摊|按个数|按数量|按件数|"
            r"按人头拉通|拉通人头|人头拉通|"
            r"人头摊|按人头"
            r")"
            r"\s*(?:是|为|=|：|:|，|,)?\s*"
            r"[￥¥]?\s*(\d+(?:\.\d{1,4})?)"
            r"\s*(?:元)?"
        ),
    ]

    for pattern in share_mode_amount_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)

    # 4. 识别“裸金额 + 均摊模式”
    #
    # 例如：
    # 517个数摊
    # 300按人头拉通
    amount_share_mode_pattern = (
        r"(?<![\d.])(\d+(?:\.\d{1,4})?)"
        r"\s*(?:元)?\s*"
        r"(?:"
        r"按个数拉通|按数量拉通|按件数拉通|"
        r"拉通个数|拉通数量|拉通件数|"
        r"个数拉通|数量拉通|件数拉通|"
        r"个数摊|数量摊|件数摊|按个数|按数量|按件数|"
        r"按人头拉通|拉通人头|人头拉通|"
        r"人头摊|按人头"
        r")"
    )

    match = re.search(amount_share_mode_pattern, text)
    if match:
        return match.group(1)

    # 5. 支持多轮对话中用户只输入一个数字
    if re.fullmatch(r"\d+(?:\.\d{1,4})?", text):
        return text

    return None


# ----------------------------------------------------------------------
# 各商品独立均摊金额
# ----------------------------------------------------------------------

def parse_product_share_amounts(text: str) -> list[dict[str, Any]]:
    """
    提取独立均摊中各商品的均摊金额。

    支持按商品序号：
        1号10
        1号 10元
        商品1=10
        商品1：10
        第1款10
        1款10
        1号商品均摊10

    支持按商品名称：
        小猫徽章均摊10
        小猫徽章摊10
        小猫徽章：10
        小猫徽章=10元

    多个商品可用中文或英文标点分开：
        1号10，2号20，3号5
        小猫徽章：10；小狗徽章：20
        总均摊30，1号10，2号20

    同一句话里同一商品重复出现时，最后一个值优先。
    """
    clauses = split_input_clauses(text)
    parsed_items: list[dict[str, Any]] = []

    for clause_index, clause in enumerate(clauses):
        clause = clause.strip()

        if not clause:
            continue

        parsed = parse_product_share_clause(
            clause=clause,
            clause_index=clause_index,
        )

        if parsed is not None:
            parsed_items.append(parsed)

    return dedupe_product_share_updates(parsed_items)


def split_input_clauses(text: str) -> list[str]:
    """
    按常见列表分隔符拆分。

    不按冒号拆分，因为“小猫徽章：10”需要保留在同一个片段。
    """
    return [
        part.strip()
        for part in re.split(r"[，,；;\n]+", text)
        if part.strip()
    ]


def parse_product_share_clause(
    clause: str,
    clause_index: int,
) -> dict[str, Any] | None:
    """
    解析单个商品金额片段。

    clause_index 用于保持原顺序，之后实现“后出现覆盖前面”。
    """
    normalized = clause.strip()

    # 跳过明显属于总金额的片段
    if re.search(
        r"(?:总均摊金额|总均摊|均摊总额|总金额|总额|金额合计|合计金额)",
        normalized,
    ):
        return None

    # --------------------------------------------------------------
    # 1. 按商品序号匹配
    # --------------------------------------------------------------

    serial_patterns = [
        # 商品1=10 / 商品1：10 / 商品1均摊10
        (
            r"^(?:第)?(?:商品)?\s*(?P<no>\d+)"
            r"\s*(?:号|款)?"
            r"\s*(?:商品均摊|均摊|分摊|摊|=|：|:)?"
            r"\s*[￥¥]?"
            r"(?P<amount>\d+(?:\.\d{1,4})?)"
            r"\s*元?$"
        ),
    ]

    for pattern in serial_patterns:
        match = re.fullmatch(pattern, normalized)

        if match:
            return {
                "商品序号": int(match.group("no")),
                "商品均摊": match.group("amount"),
                "_order": clause_index,
            }

    # --------------------------------------------------------------
    # 2. 按商品名称匹配
    # --------------------------------------------------------------

    name_patterns = [
        # 小猫徽章均摊10 / 小猫徽章摊10
        (
            r"^(?P<name>.+?)"
            r"\s*(?:商品均摊|均摊|分摊|摊)"
            r"\s*[￥¥]?"
            r"(?P<amount>\d+(?:\.\d{1,4})?)"
            r"\s*元?$"
        ),

        # 小猫徽章：10 / 小猫徽章=10
        (
            r"^(?P<name>.+?)"
            r"\s*(?:=|：|:)"
            r"\s*[￥¥]?"
            r"(?P<amount>\d+(?:\.\d{1,4})?)"
            r"\s*元?$"
        ),
    ]

    for pattern in name_patterns:
        match = re.fullmatch(pattern, normalized)

        if not match:
            continue

        product_name = clean_product_name(match.group("name"))

        if not product_name:
            return None

        if product_name in RESERVED_PRODUCT_NAMES:
            return None

        # 防止把“订单文件：D:\xxx”一类路径误判成商品
        if looks_like_path(product_name):
            return None

        return {
            "商品名称": product_name,
            "商品均摊": match.group("amount"),
            "_order": clause_index,
        }

    return None


def clean_product_name(value: str) -> str:
    name = str(value or "").strip()

    # 去除常见引导词
    prefixes = [
        "商品名称",
        "商品名",
        "款式",
        "款",
    ]

    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):].strip()

    return name


def looks_like_path(value: str) -> bool:
    """
    简单排除 Windows/Linux 路径。
    """
    text = str(value or "").strip()

    return (
        "\\" in text
        or "/" in text
        or bool(re.fullmatch(r"[A-Za-z]", text))
    )


def dedupe_product_share_updates(
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    对同一句话中的重复商品配置去重。

    规则：
        后出现的值覆盖前面的值。

    示例：
        1号10，1号12
    最终：
        1号12
    """
    latest: dict[tuple[str, str], dict[str, Any]] = {}

    for item in updates:
        if item.get("商品序号") is not None:
            key = ("商品序号", str(item["商品序号"]))
        else:
            key = ("商品名称", str(item.get("商品名称") or ""))

        latest[key] = item

    # 按最后一次出现的位置重新排序
    result = sorted(
        latest.values(),
        key=lambda item: int(item.get("_order", 0)),
    )

    for item in result:
        item.pop("_order", None)

    return result


# ----------------------------------------------------------------------
# 群聊和文件路径
# ----------------------------------------------------------------------

def parse_group_name(text: str) -> str | None:
    """
    支持：
        群聊：xxx
        群名：xxx
        群聊名称是xxx
        当前群聊 xxx
    """
    patterns = [
        (
            r"(?:群聊名称|当前群聊|群聊|群名)"
            r"\s*(?:是|为|=|：|:)?\s*"
            r"([^，,。；;\n]+)"
        ),
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            value = match.group(1).strip().strip('"').strip("'")

            if value:
                return value

    return None


def parse_order_input(text: str) -> str | None:
    """
    支持：
        当前订单：订单1.xlsx
        订单文件：订单1.xlsx
        订单表：订单1.xlsx
        订单路径：D:\\orders\\订单1.xlsx
        输入文件：订单1.xlsx
        输入目录：D:\\orders
        订单目录：D:\\orders
    """
    patterns = [
        (
            r"(?:订单|当前订单文件|当前订单|订单文件|订单表|订单路径|"
            r"输入文件|输入目录|订单目录)"
            r"\s*(?:是|为|=|：|:)?\s*"
            r"([^，,。；;\n]+)"
        ),
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            value = match.group(1).strip().strip('"').strip("'")

            if value:
                return value

    return None


def parse_order_output_dir(text: str) -> str | None:
    """
    支持：
        输出目录：D:\\orders\\output
        保存目录：D:\\orders\\output
        结果目录：D:\\orders\\output
    """
    patterns = [
        (
            r"(?:输出目录|保存目录|结果目录)"
            r"\s*(?:是|为|=|：|:)?\s*"
            r"([^，,。；;\n]+)"
        ),
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            value = match.group(1).strip().strip('"').strip("'")

            if value:
                return value

    return None