from __future__ import annotations

import re
from collections import Counter
from typing import Any, Iterable

from app.analysis.special_constants import (
    MULTI_PERSON_ROLES,
    SINGLE_PERSON_ROLES,
    SPECIAL_MEMBER_ROLES,
)


class SpecialMemberError(RuntimeError):
    """特殊成员配置错误。"""


IDENTITY_FIELDS = (
    "昵称",
    "群昵称",
    "单号",
)


def normalize_text(value: Any) -> str:
    """将任意值规范化为去除首尾空白的字符串。"""
    if value is None:
        return ""

    return str(value).strip()


def normalize_serial(value: Any) -> str:
    """
    将单号规范化为不含前导零的正整数字符串。

    示例：
        "001" -> "1"
        1 -> "1"
        1.0 -> "1"
        "" -> ""
        "A01" -> ""
    """
    if value is None or isinstance(value, bool):
        return ""

    if isinstance(value, int):
        return str(value) if value > 0 else ""

    if isinstance(value, float):
        if value.is_integer() and value > 0:
            return str(int(value))

        return ""

    text = str(value).strip()

    if not text:
        return ""

    if not re.fullmatch(r"\d+", text):
        return ""

    number = int(text)

    if number <= 0:
        return ""

    return str(number)


def extract_leading_serial(group_nickname: Any) -> str:
    """
    提取群昵称开头的连续数字，并规范化。

    示例：
        "001 Yann" -> "1"
        "12张三" -> "12"
        "张三12" -> ""
    """
    text = normalize_text(group_nickname)
    match = re.match(r"^(\d+)", text)

    if not match:
        return ""

    return normalize_serial(match.group(1))


def normalize_include_share(
    value: Any,
    *,
    default: bool | None = None,
) -> bool | None:
    """
    规范化“是否参摊”。

    返回：
        True：参摊
        False：不参摊
        None：没有提供
    """
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int) and value in (0, 1):
        return bool(value)

    text = normalize_text(value).lower()

    if not text:
        return default

    # 必须先判断否定含义。
    false_values = {
        "false",
        "no",
        "n",
        "0",
        "否",
        "不",
        "不参摊",
        "不参与均摊",
        "不计入均摊",
        "不参加均摊",
    }

    true_values = {
        "true",
        "yes",
        "y",
        "1",
        "是",
        "参摊",
        "参与均摊",
        "计入均摊",
        "参加均摊",
    }

    if text in false_values:
        return False

    if text in true_values:
        return True

    raise SpecialMemberError(
        f"无法识别是否参摊：{value!r}。"
        "请使用“参摊”或“不参摊”。"
    )


def normalize_special_member(
    item: dict[str, Any],
    *,
    default_include_share: bool | None = True,
) -> dict[str, Any]:
    """
    将特殊成员信息规范化为统一结构。

    返回：
        {
            "角色": "车主",
            "昵称": "Yann",
            "群昵称": "001 Yann",
            "单号": "1",
            "参摊": False,
        }
    """
    if not isinstance(item, dict):
        raise SpecialMemberError(
            "特殊成员信息必须是字典，"
            f"实际收到：{type(item).__name__}"
        )

    role = normalize_text(item.get("角色"))

    if role not in SPECIAL_MEMBER_ROLES:
        raise SpecialMemberError(
            f"无法识别特殊成员角色：{role or '空'}。"
            f"允许的角色：{'、'.join(SPECIAL_MEMBER_ROLES)}。"
        )

    raw_order_no = item.get("单号")
    order_no = normalize_serial(raw_order_no)

    if normalize_text(raw_order_no) and not order_no:
        raise SpecialMemberError(
            f"{role}的单号不是有效正整数："
            f"{raw_order_no!r}"
        )

    return {
        "角色": role,
        "昵称": normalize_text(item.get("昵称")),
        "群昵称": normalize_text(item.get("群昵称")),
        "单号": order_no,
        "参摊": normalize_include_share(
            item.get("参摊"),
            default=default_include_share,
        ),
    }


def has_member_identity(
    item: dict[str, Any],
) -> bool:
    """
    判断是否至少提供了昵称、群昵称、单号中的一项。
    """
    return any(
        normalize_text(item.get(field))
        for field in IDENTITY_FIELDS
    )


def update_special_member_cache(
    current_members: Iterable[dict[str, Any]] | None,
    updates: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    更新特殊成员缓存，并返回新的列表。

    规则：
    1. 车主、画师、章稿画师、供稿人每种角色最多1人。
    2. 工具人可以有多人。
    3. 单人角色再次输入时更新原记录，不新增第二条。
    4. 工具人优先按单号、群昵称、昵称匹配原记录。
    5. 新增成员未说明是否参摊时，默认不参摊。
    """
    result = [
        normalize_special_member(
            item,
            default_include_share=True,
        )
        for item in (current_members or [])
    ]

    for raw_update in updates or []:
        if raw_update.get("_修改错误"):
            raise SpecialMemberError(
                str(raw_update["_修改错误"])
            )

        update = normalize_special_member(
            raw_update,
            default_include_share=None,
        )

        # normalize_special_member() 只保留标准成员字段，
        # 所以需要把修改命令的定位信息重新放回去。
        update["_匹配字段"] = normalize_text(
            raw_update.get("_匹配字段")
        )
        update["_匹配值"] = normalize_text(
            raw_update.get("_匹配值")
        )
        update["_修改字段"] = normalize_text(
            raw_update.get("_修改字段")
        )

        role = update["角色"]

        if role in SINGLE_PERSON_ROLES:
            _update_single_person_role(
                members=result,
                update=update,
            )

        elif role in MULTI_PERSON_ROLES:
            _update_multi_person_role(
                members=result,
                update=update,
            )

        else:
            # 防止 special_constants.py 中的配置不完整。
            raise SpecialMemberError(
                f"角色“{role}”没有配置人数规则。"
            )

    errors = validate_special_member_cache(
        result,
        require_owner=False,
        require_non_share_order_no=False,
    )

    if errors:
        raise SpecialMemberError(
            "\n".join(errors)
        )

    return result


def _update_single_person_role(
    members: list[dict[str, Any]],
    update: dict[str, Any],
) -> None:
    """
    更新车主、画师、章稿画师、供稿人。
    """
    role = update["角色"]

    indexes = [
        index
        for index, member in enumerate(members)
        if member["角色"] == role
    ]

    if len(indexes) > 1:
        raise SpecialMemberError(
            f"缓存中已有多名{role}，"
            "请先清理重复配置。"
        )

    if not indexes:
        if not has_member_identity(update):
            raise SpecialMemberError(
                f"新增{role}时，至少需要提供昵称、"
                "群昵称或单号中的一项。"
            )

        if update["参摊"] is None:
            update["参摊"] = False

        members.append(update)
        return

    target = members[indexes[0]]

    _merge_special_member(
        target=target,
        update=update,
    )


def _update_multi_person_role(
    members: list[dict[str, Any]],
    update: dict[str, Any],
) -> None:
    """
    更新工具人。

    工具人可以有多人，所以需要根据昵称、群昵称或单号判断
    用户是在更新已有工具人，还是新增工具人。
    """
    role = update["角色"]

    role_indexes = [
        index
        for index, member in enumerate(members)
        if member["角色"] == role
    ]

    if update.get("_修改字段"):
        _apply_explicit_member_edit(
            members=members,
            update=update,
            role_indexes=role_indexes,
        )
        return

    # 例如只有“工具人：不参摊”，没有指定具体是谁。
    if not has_member_identity(update):
        if not role_indexes:
            raise SpecialMemberError(
                f"新增{role}时，至少需要提供昵称、"
                "群昵称或单号中的一项。"
            )

        if len(role_indexes) > 1:
            raise SpecialMemberError(
                f"当前有多名{role}，"
                "只修改参摊状态时必须指定昵称、"
                "群昵称或单号。"
            )

        _merge_special_member(
            target=members[role_indexes[0]],
            update=update,
        )
        return

    matched_indexes = _find_matching_indexes(
        members=members,
        update=update,
        role_indexes=role_indexes,
    )

    if len(matched_indexes) > 1:
        raise SpecialMemberError(
            f"{role}更新信息分别匹配到了多个人，"
            "请检查昵称、群昵称和单号是否属于同一人。"
        )

    if len(matched_indexes) == 1:
        matched_index = next(iter(matched_indexes))

        _merge_special_member(
            target=members[matched_index],
            update=update,
        )
        return

    # 没匹配到原工具人，作为新的工具人加入。
    if update["参摊"] is None:
        update["参摊"] = False

    members.append(update)


def _apply_explicit_member_edit(
    *,
    members: list[dict[str, Any]],
    update: dict[str, Any],
    role_indexes: list[int],
) -> None:
    """
    根据旧身份信息定位成员，然后修改指定字段。

    明确修改命令绝不自动新增成员。
    """
    role = update["角色"]
    match_field = normalize_text(
        update.get("_匹配字段")
    )
    match_value = normalize_text(
        update.get("_匹配值")
    )
    target_field = normalize_text(
        update.get("_修改字段")
    )

    if not match_value:
        raise SpecialMemberError(
            f"修改{role}时没有提供用于定位成员的信息。"
        )

    if target_field not in IDENTITY_FIELDS:
        raise SpecialMemberError(
            f"不支持修改字段：{target_field or '空'}。"
        )

    matched_indexes: list[int] = []

    for index in role_indexes:
        member = members[index]

        if match_field:
            fields_to_check = (match_field,)
        else:
            # 未指定检索字段时，昵称、群昵称、单号都检查。
            fields_to_check = IDENTITY_FIELDS

        matched = False

        for field in fields_to_check:
            current_value = normalize_text(
                member.get(field)
            )
            expected_value = match_value

            if field == "单号":
                current_value = normalize_serial(
                    current_value
                )
                expected_value = normalize_serial(
                    expected_value
                )

            if (
                current_value
                and expected_value
                and current_value == expected_value
            ):
                matched = True
                break

        if matched:
            matched_indexes.append(index)

    if not matched_indexes:
        field_text = (
            f"{match_field}为“{match_value}”"
            if match_field
            else f"身份信息为“{match_value}”"
        )

        raise SpecialMemberError(
            f"没有找到{field_text}的{role}，"
            "因此没有进行修改。"
        )

    if len(matched_indexes) > 1:
        field_text = (
            f"{match_field}“{match_value}”"
            if match_field
            else f"“{match_value}”"
        )

        raise SpecialMemberError(
            f"{field_text}匹配到了多名{role}，"
            "请明确写出昵称、群昵称或单号进行检索。"
        )

    target = members[matched_indexes[0]]
    new_value = normalize_text(
        update.get(target_field)
    )

    if target_field == "单号":
        new_value = normalize_serial(new_value)

        if not new_value:
            raise SpecialMemberError(
                "新单号必须是正整数。"
            )

    if not new_value:
        raise SpecialMemberError(
            f"新的{target_field}不能为空。"
        )

    target[target_field] = new_value


def _find_matching_indexes(
    *,
    members: list[dict[str, Any]],
    update: dict[str, Any],
    role_indexes: list[int],
) -> set[int]:
    """
    查找与更新数据匹配的已有成员。

    如果昵称匹配甲、单号却匹配乙，会返回两个下标，
    上层据此判断输入信息冲突。
    """
    matched: set[int] = set()

    for field in IDENTITY_FIELDS:
        value = normalize_text(
            update.get(field)
        )

        if not value:
            continue

        for index in role_indexes:
            current_value = normalize_text(
                members[index].get(field)
            )

            if current_value == value:
                matched.add(index)

    return matched


def _merge_special_member(
    *,
    target: dict[str, Any],
    update: dict[str, Any],
) -> None:
    """
    将本次提供的非空信息合并到原记录。

    未提供的字段保持原值。
    """
    for field in IDENTITY_FIELDS:
        value = normalize_text(
            update.get(field)
        )

        if value:
            target[field] = value

    if update.get("参摊") is not None:
        target["参摊"] = update["参摊"]


def validate_special_member_cache(
    members: Iterable[dict[str, Any]] | None,
    *,
    require_owner: bool = False,
    require_non_share_order_no: bool = False,
    require_order_no: bool = False,
    require_share_state: bool = False,
) -> list[str]:
    """
    校验特殊成员缓存，返回错误文本列表。

    编辑角色缓存后可以使用：
        require_owner=False

    计算均摊前建议使用：
        require_owner=True
        require_non_share_order_no=True

    参数说明：
        require_owner：
            是否要求必须有且只能有1名车主。

        require_non_share_order_no：
            是否要求所有不参摊成员必须具有有效单号。
            没有单号就无法可靠排除其订单。

        require_order_no：
            是否要求所有特殊成员都有单号。

        require_share_state：
            是否要求所有特殊成员明确设置参摊状态。
    """
    errors: list[str] = []
    normalized_members: list[dict[str, Any]] = []

    for index, raw_member in enumerate(
        members or [],
        start=1,
    ):
        try:
            member = normalize_special_member(
                raw_member,
                default_include_share=None,
            )
        except SpecialMemberError as exc:
            errors.append(
                f"第{index}条特殊成员配置错误：{exc}"
            )
            continue

        normalized_members.append(member)

        if not has_member_identity(member):
            errors.append(
                f"{member['角色']}没有昵称、群昵称或单号，"
                "无法识别具体成员。"
            )

        if (
            require_share_state
            and member["参摊"] is None
        ):
            errors.append(
                f"{_member_label(member)}"
                "没有设置是否参摊。"
            )

        if (
            require_order_no
            and not member["单号"]
        ):
            errors.append(
                f"{_member_label(member)}"
                "没有设置有效单号。"
            )

        if (
            require_non_share_order_no
            and member["参摊"] is False
            and not member["单号"]
        ):
            errors.append(
                f"{_member_label(member)}被设置为不参摊，"
                "但没有有效单号，无法从均摊订单中排除。"
            )

    role_counts = Counter(
        member["角色"]
        for member in normalized_members
    )

    for role in SINGLE_PERSON_ROLES:
        if role_counts[role] > 1:
            errors.append(
                f"{role}最多只能设置1人。"
            )

    owner_count = role_counts["车主"]

    if require_owner and owner_count != 1:
        errors.append(
            "计算均摊前必须设置且只能设置1名车主。"
        )

    errors.extend(
        _validate_multi_person_duplicates(
            normalized_members
        )
    )

    return _deduplicate_texts(errors)


def _validate_multi_person_duplicates(
    members: list[dict[str, Any]],
) -> list[str]:
    """
    检查工具人内部是否存在重复身份信息。
    """
    errors: list[str] = []

    for role in MULTI_PERSON_ROLES:
        role_members = [
            member
            for member in members
            if member["角色"] == role
        ]

        for field in IDENTITY_FIELDS:
            values = [
                normalize_text(member.get(field))
                for member in role_members
                if normalize_text(member.get(field))
            ]

            counts = Counter(values)

            for value, count in counts.items():
                if count > 1:
                    errors.append(
                        f"{role}中存在重复的{field}：{value}"
                    )

    return errors


def assert_valid_special_member_cache(
    members: Iterable[dict[str, Any]] | None,
    *,
    require_owner: bool = False,
    require_non_share_order_no: bool = False,
    require_order_no: bool = False,
    require_share_state: bool = False,
) -> None:
    """
    校验特殊成员缓存。

    与 validate_special_member_cache() 的区别是：
    本函数发现问题时直接抛出 SpecialMemberError。
    """
    errors = validate_special_member_cache(
        members,
        require_owner=require_owner,
        require_non_share_order_no=(
            require_non_share_order_no
        ),
        require_order_no=require_order_no,
        require_share_state=require_share_state,
    )

    if errors:
        raise SpecialMemberError(
            "\n".join(errors)
        )


def get_non_share_special_members(
    members: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    返回所有明确设置为不参摊的特殊成员。
    """
    result: list[dict[str, Any]] = []

    for item in members or []:
        member = normalize_special_member(
            item,
            default_include_share=True,
        )

        if member["参摊"] is False:
            result.append(member)

    return result


def get_non_share_order_nos(
    members: Iterable[dict[str, Any]] | None,
) -> set[str]:
    """
    返回不参摊特殊成员的有效单号集合。

    该集合可以直接传给均摊计算器，用于排除订单。
    """
    return {
        member["单号"]
        for member in get_non_share_special_members(
            members
        )
        if member["单号"]
    }


def enrich_special_members(
    special_members: Iterable[dict[str, Any]] | None,
    group_members: Iterable[dict[str, Any]] | None,
    order_members: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    使用微信群成员和订单数据补全特殊成员信息。

    补全顺序：
    1. 使用输入的昵称、群昵称定位微信群成员；
    2. 使用微信群成员的昵称、群昵称覆盖当前值；
    3. 优先从微信群昵称开头提取单号；
    4. 没有数字时，使用微信昵称匹配订单昵称；
    5. 仍未匹配时，使用最初输入的短昵称匹配订单昵称。

    订单昵称只用于定位单号，不会写回特殊成员。
    """
    result = [
        normalize_special_member(
            item,
            default_include_share=True,
        )
        for item in (special_members or [])
    ]

    normalized_group_members = [
        _normalize_group_member(item)
        for item in (group_members or [])
    ]

    normalized_order_members = [
        _normalize_order_member(item)
        for item in (order_members or [])
    ]

    for member in result:
        input_nickname = member["昵称"]
        input_group_nickname = member["群昵称"]

        group_match = _find_special_group_member(
            special_member=member,
            group_members=normalized_group_members,
        )

        _fill_from_group_member(
            special_member=member,
            group_member=group_match,
        )

        # 微信群昵称开头的数字是单号的首选来源。
        group_serial = extract_leading_serial(
            member["群昵称"] if group_match else ""
        )

        if group_serial:
            member["单号"] = group_serial
            continue

        # 已经手动提供单号时保留，不再用昵称重新推断。
        if member["单号"]:
            continue

        order_match = _find_special_order_member(
            special_member=member,
            order_members=normalized_order_members,
        )

        _fill_from_order_member(
            special_member=member,
            order_member=order_match,
        )

    return result


def _normalize_group_member(
    item: dict[str, Any],
) -> dict[str, str]:
    return {
        "wxid": normalize_text(
            item.get("wxid")
        ),
        "备注": normalize_text(
            item.get("备注")
        ),
        "群昵称": normalize_text(
            item.get("群昵称")
        ),
        "昵称": normalize_text(
            item.get("昵称")
        ),
    }


def _normalize_order_member(
    item: dict[str, Any],
) -> dict[str, str]:
    return {
        "单号": normalize_serial(
            item.get("单号")
        ),
        "昵称": normalize_text(
            item.get("昵称")
        ),
    }


def _find_special_group_member(
    *,
    special_member: dict[str, Any],
    group_members: list[dict[str, str]],
) -> dict[str, str] | None:
    group_nickname = special_member["群昵称"]
    nickname = special_member["昵称"]
    order_no = special_member["单号"]

    # --------------------------------------------------
    # 1. 群昵称精确匹配
    # --------------------------------------------------
    if group_nickname:
        matched = [
            member
            for member in group_members
            if member["群昵称"] == group_nickname
        ]

        unique = _unique_item(matched)

        if unique:
            return unique

    # --------------------------------------------------
    # 2. 单号精确匹配群昵称开头数字
    # --------------------------------------------------
    if order_no:
        matched = [
            member
            for member in group_members
            if extract_leading_serial(
                member["群昵称"]
            ) == order_no
        ]

        unique = _unique_item(matched)

        if unique:
            return unique

    # --------------------------------------------------
    # 3. 昵称、备注、wxid 精确匹配
    # --------------------------------------------------
    if nickname:
        matched = [
            member
            for member in group_members
            if nickname
            in {
                member["昵称"],
                member["备注"],
                member["wxid"],
            }
        ]

        unique = _unique_item(matched)

        if unique:
            return unique

        # 兼容把群昵称误填到“昵称”字段。
        matched = [
            member
            for member in group_members
            if member["群昵称"] == nickname
        ]

        unique = _unique_item(matched)

        if unique:
            return unique

    # --------------------------------------------------
    # 4. 群昵称模糊匹配
    # --------------------------------------------------
    if group_nickname:
        unique = _find_unique_fuzzy_member(
            query=group_nickname,
            members=group_members,
            fields=(
                "群昵称",
                "昵称",
            ),
        )

        if unique:
            return unique

    # --------------------------------------------------
    # 5. 昵称模糊匹配
    # --------------------------------------------------
    if nickname:
        unique = _find_unique_fuzzy_member(
            query=nickname,
            members=group_members,
            fields=(
                "昵称",
                "群昵称",
                "备注",
            ),
        )

        if unique:
            return unique

    return None


def _find_special_order_member(
    *,
    special_member: dict[str, Any],
    order_members: list[dict[str, str]],
) -> dict[str, str] | None:
    order_no = special_member["单号"]
    nickname = special_member["昵称"]

    # 1. 单号精确匹配
    if order_no:
        matched = [
            member
            for member in order_members
            if member["单号"] == order_no
        ]

        unique = _unique_item(matched)

        if unique:
            return unique

    # 2. 昵称精确匹配
    if nickname:
        matched = [
            member
            for member in order_members
            if member["昵称"] == nickname
        ]

        unique = _unique_item(matched)

        if unique:
            return unique

    # 3. 昵称模糊匹配
    # “微信群成员里暂时查不到，但订单中能找到”的情况
    # 订单昵称无法覆盖已经从微信获取到的完整昵称
    if nickname:
        return _find_unique_fuzzy_member(
            query=nickname,
            members=order_members,
            fields=("昵称",),
        )

    return None


def _fill_from_group_member(
    *,
    special_member: dict[str, Any],
    group_member: dict[str, str] | None,
) -> None:
    """
    使用已确认匹配的微信群成员更新特殊成员信息。

    由于传入的 group_member 已经经过唯一匹配，
    因此昵称和群昵称可以更新为微信群中的完整值。
    """
    if not group_member:
        return

    full_group_nickname = normalize_text(
        group_member.get("群昵称")
    )

    full_nickname = (
        normalize_text(group_member.get("昵称"))
        or normalize_text(group_member.get("备注"))
        or normalize_text(group_member.get("wxid"))
    )

    if full_group_nickname:
        special_member["群昵称"] = (
            full_group_nickname
        )

    if full_nickname:
        special_member["昵称"] = full_nickname

    serial = extract_leading_serial(
        full_group_nickname
    )

    if serial:
        special_member["单号"] = serial


def _fill_from_order_member(
    *,
    special_member: dict[str, Any],
    order_member: dict[str, str] | None,
) -> None:
    """订单信息只用于补全单号，不写回订单昵称。"""
    if not order_member:
        return

    if not special_member["单号"]:
        special_member["单号"] = (
            order_member["单号"]
        )


def member_matches_special_member(
    group_member: dict[str, Any],
    special_member: dict[str, Any],
) -> bool:
    """
    判断一条微信群成员记录是否对应某个特殊成员。
    """
    group = _normalize_group_member(
        group_member
    )

    special = normalize_special_member(
        special_member,
        default_include_share=True,
    )

    if (
        special["群昵称"]
        and group["群昵称"] == special["群昵称"]
    ):
        return True

    if (
        special["单号"]
        and extract_leading_serial(
            group["群昵称"]
        )
        == special["单号"]
    ):
        return True

    if (
        special["昵称"]
        and special["昵称"]
        in {
            group["昵称"],
            group["备注"],
            group["wxid"],
        }
    ):
        return True

    return False


def order_matches_special_member(
    order_member: dict[str, Any],
    special_member: dict[str, Any],
) -> bool:
    """
    判断一条订单记录是否对应某个特殊成员。
    """
    order = _normalize_order_member(
        order_member
    )

    special = normalize_special_member(
        special_member,
        default_include_share=True,
    )

    if (
        special["单号"]
        and order["单号"] == special["单号"]
    ):
        return True

    if (
        special["昵称"]
        and order["昵称"] == special["昵称"]
    ):
        return True

    return False


def find_special_members_by_order_no(
    members: Iterable[dict[str, Any]] | None,
    order_no: Any,
) -> list[dict[str, Any]]:
    """
    根据订单号查找全部对应角色。

    同一个人可能兼任多个角色，所以返回列表。
    """
    normalized_order_no = normalize_serial(
        order_no
    )

    if not normalized_order_no:
        return []

    result: list[dict[str, Any]] = []

    for item in members or []:
        member = normalize_special_member(
            item,
            default_include_share=True,
        )

        if member["单号"] == normalized_order_no:
            result.append(member)

    return result


def _member_label(
    member: dict[str, Any],
) -> str:
    name = (
        normalize_text(member.get("群昵称"))
        or normalize_text(member.get("昵称"))
        or normalize_text(member.get("单号"))
        or "未命名"
    )

    return (
        f"{member.get('角色', '特殊成员')}"
        f"“{name}”"
    )


def normalize_name_for_match(value: Any) -> str:
    """
    将昵称规范化为适合检索的形式。

    目前只忽略空白和英文大小写；
    emoji、汉字和其他符号保留。
    """
    text = normalize_text(value)

    return re.sub(
        r"\s+",
        "",
        text,
    ).casefold()


def is_fuzzy_name_match(
    query: Any,
    candidate: Any,
) -> bool:
    """
    判断昵称是否满足包含关系。

    示例：
        番茄      -> 番茄🍅
        番茄      -> 001 番茄🍅
        yann      -> Yann
    """
    query_text = normalize_name_for_match(query)
    candidate_text = normalize_name_for_match(candidate)

    if not query_text or not candidate_text:
        return False

    # 太短的检索词容易误匹配。
    # 中文昵称建议至少输入两个字符。
    if len(query_text) < 2:
        return False

    return (
        query_text in candidate_text
        or candidate_text in query_text
    )


def _find_unique_fuzzy_member(
    *,
    query: str,
    members: list[dict[str, str]],
    fields: tuple[str, ...],
) -> dict[str, str] | None:
    """
    在指定字段中模糊查找成员。

    只有唯一成员匹配时才返回；
    同一个成员多个字段匹配不会被重复计算。
    """
    matched: list[dict[str, str]] = []

    for member in members:
        if any(
            is_fuzzy_name_match(
                query,
                member.get(field),
            )
            for field in fields
        ):
            matched.append(member)

    return _unique_item(matched)


def _unique_item(
    items: list[dict[str, str]],
) -> dict[str, str] | None:
    """
    只有唯一匹配时才返回。

    如果同名成员有多人，不进行自动补全，避免错误匹配。
    """
    if len(items) == 1:
        return items[0]

    return None


def _deduplicate_texts(
    items: Iterable[str],
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    for item in items:
        if item in seen:
            continue

        result.append(item)
        seen.add(item)

    return result