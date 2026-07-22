# app/analysis/member_parser.py
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path
from typing import Any, Callable


# 让直接运行 python app/analysis/member_parser.py 时，也能正常导入项目根目录下的 integrations / app
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from integrations.wechatmsg_lite_client import get_wechat_group_members
from app.analysis.order_parser import parse_order_file
from app.analysis.special_member import (
    enrich_special_members,
    member_matches_special_member,
)


def parse_group_member_orders(
    group_name: str,
    order_input: str | Path | dict[str, Any],
    order_output_dir: str | Path | None = None,
    special_members: list[dict[str, Any]] | None = None,
    key_input_func: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """
    比对微信群成员昵称开头序号与订单表第一列单号。

    [检查项目]            [特殊成员是否参与]
    群昵称前没有数字              0
    订单有、群昵称没有            0
    群昵称重复序号               1
    群里有、订单没有             1


    返回：
    {
        "ok": True,
        "message": "...",
        "群聊名称": "...",
        "chatroom_wxid": "...",
        "member_count": 123,

        "member_serials": [
            {"群昵称": "12张三", "序号": "12"},
            {"群昵称": "李四", "序号": ""},
        ],

        "members_without_serial": [...],

        "parsed_order_file": "...",
        "order_serials": [...],

        "serials_in_group_not_in_orders": [...],
        "serials_in_orders_not_in_group": [...],
    }
    """

    # 1. 获取群成员
    member_result = get_wechat_group_members(
        group_name=group_name,
        allow_manual_key_input=True,
        key_input_func=key_input_func,
    )

    if not member_result.get("ok"):
        return {
            "ok": False,
            "message": f"获取群成员失败：{member_result.get('message')}",
            "群聊名称": member_result.get("群聊名称"),
            "chatroom_wxid": member_result.get("chatroom_wxid"),
            "member_count": 0,
            "member_serials": [],
            "members_without_serial": [],
            "duplicate_member_serials": [],
            "parsed_order_file": None,
            "order_serials": [],
            "serials_in_group_not_in_orders": [],
            "serials_in_orders_not_in_group": [],
        }

    members = member_result.get("members", [])

    # 2. 提取 {"群昵称", "序号"}
    member_serials = []
    members_without_serial = []

    # 记录每个序号对应哪些成员，用于检查重复编号
    member_serial_index: dict[str, list[dict[str, str]]] = {}

    for member in members:
        group_nickname = str(member.get("群昵称", "") or "").strip()
        serial_raw = extract_leading_number(group_nickname)
        serial_normalized = normalize_serial(serial_raw)

        item = {
            "群昵称": group_nickname,
            "序号": serial_raw,
        }

        member_serials.append(item)

        member_info = {
            "wxid": str(member.get("wxid", "") or ""),
            "备注": str(member.get("备注", "") or ""),
            "群昵称": group_nickname,
            "昵称": str(member.get("昵称", "") or ""),
        }

        if serial_raw == "":
            members_without_serial.append(member_info)
        else:
            member_serial_index.setdefault(serial_normalized, []).append(member_info)

    # 3. 群昵称中的数字序号集合
    group_serials = {
        normalize_serial(item["序号"])
        for item in member_serials
        if item["序号"] != ""
    }

    group_serials.discard("")

    duplicate_member_serials = [
        {
            "序号": serial,
            "members": member_list,
        }
        for serial, member_list in sorted(
            member_serial_index.items(),
            key=lambda x: int(x[0]),
        )
        if len(member_list) > 1
    ]

    # 4. 调用 order_parser.py 简化订单表
    parsed_order_file = parse_order_file(
        order_input=order_input,
        output_dir=order_output_dir,
    )

    # 5. 读取订单成员
    order_members = read_order_members(
        parsed_order_file
    )

    # 6. 补全特殊成员信息
    resolved_special_members = enrich_special_members(
        special_members=special_members or [],
        group_members=members,
        order_members=order_members,
    )

    # 特殊成员不参与群昵称备注格式检查。
    # 例如车主、工具人、供稿人没有在群昵称开头填写单号时，
    # 不放入“群昵称前没有数字的成员”。
    members_without_serial = [
        member
        for member in members_without_serial
        if not is_special_group_member(
            group_member=member,
            special_members=resolved_special_members,
        )
    ]

    # 重复序号提示中不排除特殊成员。

    # 7. 获取订单单号集合
    order_serials = sorted_serials(
        [
            item["单号"]
            for item in order_members
        ]
    )

    order_serial_set = set(order_serials)

    # 8. 比对群成员单号与订单单号
    special_member_serials = {
        normalize_serial(member.get("单号"))
        for member in resolved_special_members
        if normalize_serial(member.get("单号"))
    }

    # 群里有、订单没有：
    # 特殊成员仍然参与，因此两边都使用完整序号集合。
    serials_in_group_not_in_orders = sorted_serials(
        group_serials - order_serial_set
    )

    # 订单有、群昵称没有：
    # 只从订单侧排除特殊成员的单号。
    serials_in_orders_not_in_group = sorted_serials(
        order_serial_set
        - special_member_serials
        - group_serials
    )

    return {
        "ok": True,
        "message": "群成员序号与订单单号比对完成",

        "群聊名称": member_result.get("群聊名称"),
        "chatroom_wxid": member_result.get("chatroom_wxid"),
        "member_count": member_result.get("member_count", len(members)),

        "members": members,
        "member_serials": member_serials,
        "members_without_serial": members_without_serial,
        "duplicate_member_serials": duplicate_member_serials,

        "parsed_order_file": parsed_order_file,
        "order_serials": sorted_serials(order_serial_set),

        "special_members": resolved_special_members,

        "serials_in_group_not_in_orders": serials_in_group_not_in_orders,
        "serials_in_orders_not_in_group": serials_in_orders_not_in_group,
    }


def is_special_group_member(
    *,
    group_member: dict[str, Any],
    special_members: list[dict[str, Any]],
) -> bool:
    """
    判断微信群成员是否属于已设置的特殊成员。

    特殊成员包括：
        车主
        画师
        章稿画师
        供稿人
        工具人
    """
    return any(
        member_matches_special_member(
            group_member=group_member,
            special_member=special_member,
        )
        for special_member in special_members
    )


def extract_leading_number(text: str) -> str:
    """
    提取字符串开头连续数字。
    例：
    "12张三" -> "12"
    "12 张三" -> "12"
    "001张三" -> "001"
    "张三12" -> ""
    """

    text = str(text or "").strip()
    match = re.match(r"^(\d+)", text)
    if not match:
        return ""

    return match.group(1)


def normalize_serial(value: Any) -> str:
    """
    将序号规范化为字符串数字，用于比对。

    例：
    "001" -> "1"
    1 -> "1"
    " 12 " -> "12"
    "" -> ""
    """

    if value is None:
        return ""

    s = str(value).strip()
    if not re.fullmatch(r"\d+", s):
        return ""

    return str(int(s))


def read_order_members(
    csv_path: str | Path,
) -> list[dict[str, str]]:
    """
    从简化后的订单 CSV 中读取订单成员信息。

    返回：
        [
            {
                "单号": "1",
                "昵称": "Yann",
            },
            ...
        ]
    """
    csv_path = Path(csv_path)

    if not csv_path.exists():
        raise FileNotFoundError(
            f"订单 CSV 文件不存在：{csv_path}"
        )

    order_members: list[dict[str, str]] = []

    with csv_path.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        if not reader.fieldnames:
            return order_members

        if "单号" not in reader.fieldnames:
            raise ValueError(
                "简化订单 CSV 中没有找到“单号”列。"
            )

        for row_index, row in enumerate(
            reader,
            start=2,
        ):
            raw_order_no = row.get("单号")
            order_no = normalize_serial(raw_order_no)

            if not order_no:
                raise ValueError(
                    f"订单 CSV 第 {row_index} 行的单号"
                    f"不是有效正整数：{raw_order_no!r}"
                )

            order_members.append(
                {
                    "单号": order_no,
                    "昵称": str(
                        row.get("昵称") or ""
                    ).strip(),
                }
            )

    return order_members


def sorted_serials(serials: set[str] | list[str]) -> list[str]:
    """
    按数字大小排序，但返回字符串。
    """

    return sorted(
        {normalize_serial(x) for x in serials if normalize_serial(x) != ""},
        key=lambda x: int(x),
    )


if __name__ == "__main__":
    result = parse_group_member_orders(
        group_name="临时喵喵",
        order_input=r"D:\2_PycharmTestData\test\miao2.xlsx",
        order_output_dir=r"D:\2_PycharmTestData\test2",
    )

    print("ok:", result["ok"])
    print("message:", result["message"])
    print("群聊名称:", result["群聊名称"])
    print("chatroom_wxid:", result["chatroom_wxid"])
    print("群成员数量:", result["member_count"])

    print("\n群昵称前没有数字的成员：")
    for member in result["members_without_serial"]:
        print(member)

    print("\n群昵称中重复标注的序号：")
    for item in result["duplicate_member_serials"]:
        print(f"序号 {item['序号']}：")
        for member in item["members"]:
            print("  ", member)

    print("\n群昵称有、但是订单没有的序号：")
    print(result["serials_in_group_not_in_orders"])

    print("\n订单里有、但是群昵称没有的序号：")
    print(result["serials_in_orders_not_in_group"])

    print("\n简化后的订单文件：")
    print(result["parsed_order_file"])
