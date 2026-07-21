# app/analysis/special_parser.py

from __future__ import annotations

import re
from typing import Any

from app.analysis.special_constants import (
    SPECIAL_MEMBER_ROLES,
    SPECIAL_MEMBER_ROLE_ALIASES,
)


# 对待检测名称进行排序
# 长角色名必须排在短角色名前面。
# 否则“章稿画师”可能先被识别成“画师”。
_SORTED_ROLES = sorted(
    SPECIAL_MEMBER_ROLE_ALIASES,
    key=len,
    reverse=True,
)

_ROLE_PATTERN_TEXT = "|".join(
    re.escape(role)
    for role in _SORTED_ROLES
)

ROLE_PATTERN = re.compile(
    rf"(?P<role>{_ROLE_PATTERN_TEXT})"
)


FIELD_NAMES = (
    "微信昵称",
    "群昵称",
    "群名片",
    "群备注",
    "订单号",
    "昵称",
    "单号",
    "序号",
)

EDITABLE_FIELD_ALIASES = {
    "微信昵称": "昵称",
    "昵称": "昵称",
    "群昵称": "群昵称",
    "群名片": "群昵称",
    "群备注": "群昵称",
    "订单号": "单号",
    "单号": "单号",
    "序号": "单号",
}

_EDITABLE_FIELD_PATTERN = "|".join(
    re.escape(name)
    for name in sorted(
        EDITABLE_FIELD_ALIASES,
        key=len,
        reverse=True,
    )
)

_FIELD_PATTERN_TEXT = "|".join(
    re.escape(name)
    for name in sorted(
        FIELD_NAMES,
        key=len,
        reverse=True,
    )
)


NON_SHARE_WORDS = (
    "不参与均摊",
    "不参加均摊",
    "不计入均摊",
    "无需参与均摊",
    "不用参与均摊",
    "无需参摊",
    "不用参摊",
    "排除均摊",
    "免于均摊",
    "免摊",
    "不参摊",
    "不摊",
)


SHARE_WORDS = (
    "参与均摊",
    "参加均摊",
    "计入均摊",
    "需要参摊",
    "正常参摊",
    "参摊",
)


QUESTION_OR_COMMAND_WORDS = (
    "必须",
    "需要",
    "是否",
    "是不是",
    "怎么",
    "如何",
    "为什么",
    "几个",
    "几人",
    "多少",
    "查看",
    "显示",
    "列出",
    "查询",
    "看看",
)


def normalize_text(value: Any) -> str:
    if value is None:
        return ""

    return str(value).strip()


def has_show_special_member_words(
    text: str,
) -> bool:
    """
    判断用户是否要求查看特殊成员。

    注意：
    不能因为文本里出现“车主”“画师”就判断为查看。
    必须同时出现明确的查看语义。
    """
    normalized = re.sub(
        r"\s+",
        "",
        normalize_text(text),
    )

    if not normalized:
        return False

    action_words = (
        "查看",
        "显示",
        "列出",
        "查询",
        "看看",
        "告诉我",
    )

    target_words = (
        "特殊成员",
        "角色信息",
        "角色列表",
        "当前角色",
        "已设置角色",
        "车主信息",
        "画师信息",
        "章稿画师信息",
        "供稿人信息",
        "工具人信息",
    )

    if (
        any(word in normalized for word in action_words)
        and any(word in normalized for word in target_words)
    ):
        return True

    exact_words = {
        "当前特殊成员",
        "特殊成员信息",
        "特殊成员列表",
        "当前角色信息",
        "当前角色列表",
        "已设置的特殊成员",
        "已设置的角色",
        "车主是谁",
        "画师是谁",
        "章稿画师是谁",
        "供稿人是谁",
        "工具人有哪些",
    }

    return normalized in exact_words


def parse_special_member_updates(
    text: str,
) -> list[dict[str, Any]]:
    """
    从用户输入中解析特殊成员设置。

    支持：
        车主：Yann
        车主 Yann
        车主是Yann
        车主为Yann
        车主叫Yann
        设置车主为Yann

        车主：昵称=Yann
        车主：群昵称=007 Yann
        车主：单号=7
        车主 7号

        车主 Yann 单号7 不参摊

        Yann是车主
        把Yann设为车主
    """
    normalized = normalize_text(text)

    if not normalized:
        return []

    # 查看命令不能被解析成设置命令。
    if has_show_special_member_words(normalized):
        return []

    # 必须优先解析明确修改语句。
    # 否则“修改工具人工具猫……”可能被普通设置语法识别。
    edit_update = parse_special_member_edit(
        normalized
    )

    if edit_update is not None:
        return [edit_update]

    reversed_update = parse_reversed_special_member(
        normalized
    )

    if reversed_update is not None:
        return [reversed_update]

    # 先处理“Yann是车主”“把Yann设为车主”。
    reversed_update = parse_reversed_special_member(
        normalized
    )

    if reversed_update is not None:
        return [reversed_update]

    role_matches = list(
        ROLE_PATTERN.finditer(normalized)
    )

    if not role_matches:
        return []

    updates: list[dict[str, Any]] = []

    for index, role_match in enumerate(role_matches):
        role = normalize_special_member_role(
            role_match.group("role")
        )

        segment_start = role_match.end()
        segment_end = (
            role_matches[index + 1].start()
            if index + 1 < len(role_matches)
            else len(normalized)
        )

        raw_segment = normalized[
            segment_start:segment_end
        ]

        segment = clean_role_segment(
            raw_segment
        )

        update = parse_special_member_segment(
            role=role,
            segment=segment,
        )

        # 只有确实包含身份信息或者参摊状态时，
        # 才将其作为更新操作。
        if has_special_member_update_content(update):
            updates.append(update)

    return updates


def parse_special_member_edit(
    text: str,
) -> dict[str, Any] | None:
    """
    解析特殊成员字段修改命令。

    支持角色在前：
        把工具人工具猫的昵称改为yann
        修改工具人工具猫的昵称为yann
        把工具人群昵称工具猫的昵称改为yann

    支持检索条件在前：
        把昵称为工具猫的工具人的昵称改为yann
        把群昵称工具猫的工具人昵称改为yann
        把单号12的工具人的群昵称改为001 yann
    """
    normalized = normalize_text(text)

    if not normalized:
        return None

    # ----------------------------------------------------------
    # 1. 检索条件在角色前面
    # ----------------------------------------------------------
    #
    # 例如：
    # 把昵称为工具猫的工具人的昵称改为yann
    # 把单号12的工具人群昵称改为001 yann
    #
    condition_first_pattern = re.compile(
        rf"^\s*"
        rf"(?:请\s*)?"
        rf"(?:把|修改)\s*"
        rf"(?P<match_field>{_EDITABLE_FIELD_PATTERN})"
        rf"\s*(?:是|为|=|：|:)?\s*"
        rf"(?P<match_value>.+?)"
        rf"\s*的?\s*"
        rf"(?P<role>{_ROLE_PATTERN_TEXT})"
        rf"\s*的?\s*"
        rf"(?P<target_field>{_EDITABLE_FIELD_PATTERN})"
        rf"\s*"
        rf"(?:修改为|改为|改成|修改成|为|成)"
        rf"\s*"
        rf"(?P<new_value>.+?)"
        rf"\s*$"
    )

    match = condition_first_pattern.fullmatch(
        normalized
    )

    if match:
        role = match.group("role")

        match_field = EDITABLE_FIELD_ALIASES[
            match.group("match_field")
        ]

        match_value = normalize_text(
            match.group("match_value")
        )

        target_field = EDITABLE_FIELD_ALIASES[
            match.group("target_field")
        ]

        new_value = normalize_text(
            match.group("new_value")
        )

    else:
        # ------------------------------------------------------
        # 2. 角色在前面
        # ------------------------------------------------------
        #
        # 例如：
        # 把工具人工具猫的昵称改为yann
        # 把工具人群昵称工具猫的昵称改为yann
        #
        role_first_pattern = re.compile(
            rf"^\s*"
            rf"(?:请\s*)?"
            rf"(?:把|修改)\s*"
            rf"(?P<role>{_ROLE_PATTERN_TEXT})"
            rf"\s*"
            rf"(?P<selector>.+?)"
            rf"\s*的?\s*"
            rf"(?P<target_field>{_EDITABLE_FIELD_PATTERN})"
            rf"\s*"
            rf"(?:修改为|改为|改成|修改成|为|成)"
            rf"\s*"
            rf"(?P<new_value>.+?)"
            rf"\s*$"
        )

        match = role_first_pattern.fullmatch(
            normalized
        )

        if not match:
            return None

        role = match.group("role")
        selector = normalize_text(
            match.group("selector")
        )

        target_field = EDITABLE_FIELD_ALIASES[
            match.group("target_field")
        ]

        new_value = normalize_text(
            match.group("new_value")
        )

        if not selector:
            return None

        # 默认在昵称、群昵称、单号中搜索。
        match_field = ""
        match_value = selector

        # 检查是否明确写了检索字段：
        # 昵称工具猫、群昵称工具猫、单号12
        selector_pattern = re.compile(
            rf"^(?P<field>{_EDITABLE_FIELD_PATTERN})"
            rf"\s*(?:是|为|=|：|:)?\s*"
            rf"(?P<value>.+?)$"
        )

        selector_match = selector_pattern.fullmatch(
            selector
        )

        if selector_match:
            possible_field = EDITABLE_FIELD_ALIASES[
                selector_match.group("field")
            ]

            possible_value = normalize_text(
                selector_match.group("value")
            )

            if possible_value:
                match_field = possible_field
                match_value = possible_value

    # ----------------------------------------------------------
    # 3. 清理和校验解析结果
    # ----------------------------------------------------------
    match_value = match_value.strip(
        " ，,。；;：:"
    )
    new_value = new_value.strip(
        " ，,。；;：:"
    )

    if not match_value or not new_value:
        return None

    if match_field == "单号":
        match_value = normalize_order_no(
            match_value
        )

        if not match_value:
            return {
                "角色": role,
                "_修改错误": (
                    "用于检索成员的单号必须是正整数。"
                ),
            }

    if target_field == "单号":
        new_value = normalize_order_no(
            new_value
        )

        if not new_value:
            return {
                "角色": role,
                "_修改错误": (
                    "修改后的单号必须是正整数。"
                ),
            }

    update = {
        "角色": role,
        "昵称": "",
        "群昵称": "",
        "单号": "",
        "参摊": None,

        "_匹配字段": match_field,
        "_匹配值": match_value,
        "_修改字段": target_field,
    }

    update[target_field] = new_value

    return update


def parse_reversed_special_member(
    text: str,
) -> dict[str, Any] | None:
    """
    解析角色词在后面的表达方式：

        Yann是车主
        Yann为车主
        Yann作为车主
        把Yann设为车主
        把Yann设置为车主
    """
    pattern = re.compile(
        rf"^\s*"
        rf"(?:请\s*)?"
        rf"(?:把\s*)?"
        rf"(?P<nickname>.+?)"
        rf"\s*"
        rf"(?:设置为|设为|作为|是|为)"
        rf"\s*"
        rf"(?P<role>{_ROLE_PATTERN_TEXT})"
        rf"(?P<tail>.*)"
        rf"$"
    )

    match = pattern.fullmatch(text)

    if not match:
        return None

    nickname = normalize_text(
        match.group("nickname")
    )

    nickname = re.sub(
        r"^(?:请|帮我|我要|我想)\s*",
        "",
        nickname,
    ).strip()

    if not is_valid_bare_nickname(nickname):
        return None

    tail = clean_role_segment(
        match.group("tail")
    )

    tail_update = parse_special_member_segment(
        role=match.group("role"),
        segment=tail,
    )

    tail_update["昵称"] = (
        tail_update["昵称"]
        or nickname
    )

    return tail_update


def clean_role_segment(
    segment: str,
) -> str:
    """
    去掉角色后面的常见连接词和标点。

    例如：
        ：Yann          -> Yann
        是 Yann         -> Yann
        信息：Yann      -> Yann
        叫做 Yann       -> Yann
    """
    value = normalize_text(segment)

    value = re.sub(
        r"^[，,；;\s]*",
        "",
        value,
    )

    value = re.sub(
        r"^(?:的信息|信息)?"
        r"\s*"
        r"(?:叫做|设置为|设为|是|为|叫|=|：|:|-)?"
        r"\s*",
        "",
        value,
    )

    return value.strip()


def parse_special_member_segment(
    role: str,
    segment: str,
) -> dict[str, Any]:
    nickname = extract_special_member_field(
        segment,
        field_names=(
            "微信昵称",
            "昵称",
        ),
    )

    group_nickname = extract_special_member_field(
        segment,
        field_names=(
            "群昵称",
            "群名片",
            "群备注",
        ),
    )

    order_no = extract_order_no(segment)
    include_share = parse_include_share(segment)

    if not nickname:
        nickname = extract_bare_nickname(
            segment
        )

    return {
        "角色": role,
        "昵称": nickname,
        "群昵称": group_nickname,
        "单号": order_no,
        "参摊": include_share,
    }


def extract_special_member_field(
    text: str,
    field_names: tuple[str, ...],
) -> str:
    """
    支持：

        昵称=Yann
        昵称：Yann
        昵称是Yann
        昵称Yann

        群昵称=007 Yann
        群昵称 007 Yann
    """
    names_pattern = "|".join(
        re.escape(name)
        for name in sorted(
            field_names,
            key=len,
            reverse=True,
        )
    )

    # “昵称”不能匹配“群昵称”“微信昵称”中的后半部分
    if "昵称" in field_names:
        names_pattern = (
            rf"(?:微信昵称|(?<!群)(?<!微信)昵称)"
        )

    stop_words = (
        list(FIELD_NAMES)
        + list(NON_SHARE_WORDS)
        + list(SHARE_WORDS)
    )

    stop_pattern = "|".join(
        re.escape(word)
        for word in sorted(
            stop_words,
            key=len,
            reverse=True,
        )
    )

    pattern = re.compile(
        rf"(?:{names_pattern})"
        rf"\s*"
        rf"(?:叫做|是|为|叫|=|：|:)?"
        rf"\s*"
        rf"(?P<value>.+?)"
        rf"(?="
        rf"\s*(?:"
        rf"[，,；;\n]"
        rf"|{stop_pattern}"
        rf"|$"
        rf")"
        rf")"
    )

    match = pattern.search(text)

    if not match:
        return ""

    value = normalize_text(
        match.group("value")
    )

    return value.strip(" ，,；;")


def extract_order_no(
    text: str,
) -> str:
    """
    支持：

        单号=7
        单号7
        订单号：7
        序号为7
        7号
        #7

    在特殊成员设置语境中，单独的纯数字也按单号处理。
    """
    explicit_patterns = (
        r"(?:订单号|单号|序号)"
        r"\s*(?:是|为|=|：|:)?\s*"
        r"#?\s*(\d+)",

        r"(?:^|[\s，,；;])"
        r"#?\s*(\d+)\s*号"
        r"(?=$|[\s，,；;])",
    )

    for pattern in explicit_patterns:
        match = re.search(pattern, text)

        if match:
            return normalize_order_no(
                match.group(1)
            )

    cleaned = remove_share_words(text)
    cleaned = cleaned.strip(
        " ，,；;：:="
    )

    # 例如：车主 7
    if re.fullmatch(r"#?\d+", cleaned):
        return normalize_order_no(
            cleaned.lstrip("#")
        )

    return ""


def normalize_order_no(
    value: Any,
) -> str:
    text = normalize_text(value)

    if not re.fullmatch(r"\d+", text):
        return ""

    number = int(text)

    if number <= 0:
        return ""

    return str(number)


def parse_include_share(
    text: str,
) -> bool | None:
    """
    返回：
        False：明确不参摊
        True：明确参摊
        None：没有说明
    """
    normalized = normalize_text(text)

    # 否定词必须先判断。
    if any(
        word in normalized
        for word in NON_SHARE_WORDS
    ):
        return False

    if any(
        word in normalized
        for word in SHARE_WORDS
    ):
        return True

    return None


def extract_bare_nickname(
    segment: str,
) -> str:
    """
    提取没有明确写“昵称=”的昵称。

    示例：
        Yann
        Yann，单号7
        Yann 单号7 不参摊
        Yann 7号
    """
    value = normalize_text(segment)

    if not value:
        return ""

    value = remove_share_words(value)

    # 显式字段后面的内容不能作为裸昵称。
    field_positions: list[int] = []

    for field_name in FIELD_NAMES:
        position = value.find(field_name)

        if position >= 0:
            field_positions.append(position)

    if field_positions:
        first_position = min(field_positions)

        if first_position == 0:
            return ""

        value = value[:first_position]

    # 删除“7号”这种单号表达。
    value = re.sub(
        r"(?:^|[\s，,；;])"
        r"#?\d+\s*号"
        r"(?=$|[\s，,；;])",
        " ",
        value,
    )

    # 只取第一个明显的成员名称片段。
    value = re.split(
        r"[，,；;\n]",
        value,
        maxsplit=1,
    )[0]

    value = value.strip(
        " ，,；;：:=-"
    )

    value = re.sub(
        r"^(?:昵称|名字)"
        r"\s*(?:是|为|叫做|叫|=|：|:)?"
        r"\s*",
        "",
        value,
    ).strip()

    # 纯数字优先作为单号，而不是昵称。
    if re.fullmatch(r"#?\d+", value):
        return ""

    if not is_valid_bare_nickname(value):
        return ""

    return value


def remove_share_words(
    text: str,
) -> str:
    result = text

    all_words = sorted(
        NON_SHARE_WORDS + SHARE_WORDS,
        key=len,
        reverse=True,
    )

    for word in all_words:
        result = result.replace(
            word,
            " ",
        )

    return result


def is_valid_bare_nickname(
    value: str,
) -> bool:
    normalized = normalize_text(value)

    if not normalized:
        return False

    if any(
        word in normalized
        for word in QUESTION_OR_COMMAND_WORDS
    ):
        return False

    if normalized in {
        "信息",
        "成员",
        "人员",
        "角色",
        "设置",
        "修改",
        "新增",
        "添加",
    }:
        return False

    return True


def has_special_member_update_content(
    update: dict[str, Any],
) -> bool:
    """
    判断解析结果是否确实包含设置内容。

    “车主”本身不算设置；
    “车主不参摊”可以用于更新已有车主的状态。
    """
    return bool(
        update.get("昵称")
        or update.get("群昵称")
        or update.get("单号")
        or update.get("参摊") is not None
    )