"""将每车原始订单映射到唯一微信身份，不执行成员检查业务。"""
from app.analysis.member_parser import extract_leading_number, normalize_serial
from app.analysis.order_parser import OrderParseError


def resolve_order_identities(orders: list[dict], members: list[dict], group: str) -> list[dict]:
    serial_index, name_index = {}, {}
    for member in members:
        serial = normalize_serial(extract_leading_number(str(member.get("群昵称") or "")))
        if serial:
            serial_index.setdefault(serial, []).append(member)
        # 精确比较原始昵称，不使用去空格、大小写折叠或模糊检索。
        for name in {str(member.get("昵称") or ""), str(member.get("群昵称") or "")} - {""}:
            name_index.setdefault(name, []).append(member)

    def unique_candidates(candidates):
        result = {}
        for index, member in enumerate(candidates):
            wxid = str(member.get("wxid") or "").strip()
            result.setdefault(wxid or ("missing", index), member)
        return list(result.values())

    def describe(candidates):
        return "；".join(
            f"{member.get('昵称') or '未提供昵称'}（群昵称={member.get('群昵称') or '空'}，"
            f"wxid={member.get('wxid') or '缺失'}）" for member in candidates)

    resolved, issues = [], []
    missing_members = False
    for order in orders:
        serial_matches = unique_candidates(serial_index.get(order["serial"], []))
        name_matches = unique_candidates(name_index.get(order["name"], []))
        label = f"{group}单号{order['serial']}（第{order['row']}行，订单昵称“{order['name']}”）"
        if len(serial_matches) > 1:
            issues.append(f"{label}单号匹配多人：{describe(serial_matches)}")
            continue
        if serial_matches:
            member = serial_matches[0]
            wxid = str(member.get("wxid") or "").strip()
            if name_matches and not any(str(candidate.get("wxid") or "").strip() == wxid for candidate in name_matches):
                issues.append(f"{label}单号与昵称指向不同成员：单号→{describe(serial_matches)}；昵称→{describe(name_matches)}")
                continue
            basis = "单号"
        elif len(name_matches) == 1:
            member = name_matches[0]
            basis = "精确昵称"
        else:
            issues.append(f"{label}" + (f"昵称匹配多人：{describe(name_matches)}" if name_matches else
                          "未在群聊中检测到对应成员（无法匹配微信成员）"))
            missing_members = missing_members or not name_matches
            continue
        wxid = str(member.get("wxid") or "").strip()
        if not wxid:
            issues.append(f"{label}匹配成员的 wxid 缺失：{describe([member])}")
            continue
        wechat_name = str(member.get("昵称") or "")
        if not wechat_name.strip():
            wechat_name = ""
        resolved.append(dict(order, wxid=wxid, wechat_name=wechat_name, identity_basis=basis))
    if issues:
        if missing_members:
            issues.append(f"车群{group}请检查订单版本：确认已登记的最新订单属于本车群，"
                          "订单单号与当前群昵称编号一致，并核对上述成员是否仍在群中；确认后重新执行")
        raise OrderParseError("\n".join(issues))
    return resolved
