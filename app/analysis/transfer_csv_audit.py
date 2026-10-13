"""以转单 CSV 为唯一入账依据，差额产生后才读取候选线索。"""
from collections import defaultdict
from copy import deepcopy
import json

from app.analysis.transfer_audit import Members, META, timestamp
from app.analysis.transfer_extraction import PERSON_FIELDS, match_transfer_products


def audit_transfer_records(old, new, records, *, identities=(), focus_products=(),
                           history_complete=False, candidate_loader=None):
    products = [{"商品名称": p} for p in sorted({p for row in old + new for p in row if p not in META})]
    selected = {p["商品名称"] for p in products}
    if focus_products:
        selected = set()
        for focus in focus_products:
            matches = match_transfer_products(focus["product_name"], products)
            if len(matches) != 1:
                raise ValueError(f"关注商品“{focus['product_name']}”无法唯一对应订单商品，请使用完整商品名称。")
            selected.add(matches[0]["商品名称"])
    members = Members(old, new, identities)
    events = []
    for number, row in enumerate(records, 2):
        def invalid(reason):
            raise ValueError(f"转单记录 CSV 第{number}行无法核算：{reason}。请补全 CSV 后重试。")

        parties = []
        for role in ("发起人", "接收人"):
            identity = {field: row.get(role + field, "") for field in PERSON_FIELDS}
            if not any(identity.values()):
                invalid(role + "身份缺失")
            identity["昵称"] = identity.get("微信昵称") or identity.get("订单昵称", "")
            key = members.resolve(identity)
            if members.entries[key].get("unresolved"):
                invalid(role + "无法唯一对应订单成员")
            parties.append(key)
        try:
            items = json.loads(row.get("商品转移") or "[]")
        except (ValueError, TypeError):
            invalid("商品转移不是有效 JSON")
        if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
            invalid("商品转移必须是商品列表")
        scope = row.get("转移范围")
        if scope not in {"整单", "部分商品"} or (scope == "部分商品" and not items):
            invalid("转移范围或商品数量缺失")
        normalized = []
        for item in items:
            matches = match_transfer_products(item.get("商品名称") or item.get("商品原文") or "", products)
            if len(matches) != 1:
                invalid("商品无法唯一对应订单商品")
            quantity = item.get("数量")
            if not item.get("排除") and (isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0):
                invalid("商品数量必须为正整数")
            normalized.append(dict(item, 商品名称=matches[0]["商品名称"]))
        events.append({"sender": parties[0], "receiver": parties[1], "items": normalized,
                       "scope": scope, "time": timestamp(row.get("接收时间")), "number": number,
                       "id": f"csv:{number}", "evidence": f"转单记录 CSV 第{number}行："
                       f"{row.get('消息引用', '')} → {row.get('消息原文', '')}"})

    balances = {(k, p): int(e["old"].get(p) or 0) for k, e in members.entries.items() for p in selected}
    ledger = defaultdict(lambda: {"incoming": 0, "outgoing": 0, "evidence": [], "events": [], "default": False})
    # CSV 行均已由上游认定为转单；不重新匹配、去重或依据聊天否决。
    for event in sorted(events, key=lambda e: (e["time"].isoformat() if e["time"] else "", e["number"])):
        items = event["items"]
        if event["scope"] == "整单":
            excluded = {i["商品名称"] for i in items if i.get("排除")}
            items = [{"商品名称": p, "数量": balances[event["sender"], p]}
                     for p in sorted(selected - excluded) if balances[event["sender"], p] > 0]
        for item in items:
            p, quantity = item["商品名称"], item["数量"]
            if p not in selected or item.get("排除"):
                continue
            for key, field, sign in ((event["sender"], "outgoing", -1), (event["receiver"], "incoming", 1)):
                entry = ledger[key, p]
                entry[field] += quantity
                entry["default"] |= item.get("数量来源") == "默认"
                entry["evidence"].append(event["evidence"])
                entry["events"].append(event["id"])
                balances[key, p] += sign * quantity

    findings = []
    for key, member in members.entries.items():
        for p in sorted(selected):
            before, after = (int(member[side].get(p) or 0) for side in ("old", "new"))
            entry = ledger.get((key, p), {"incoming": 0, "outgoing": 0, "events": [], "evidence": [], "default": False})
            expected = entry["incoming"] - entry["outgoing"]
            difference = after - before - expected
            if not difference:
                continue
            reason = "订单数量高于有效转单核算结果" if difference > 0 else "订单数量低于有效转单核算结果"
            rank = (1 if difference > 0 else 2) if entry["events"] or history_complete else 3
            findings.append(dict(entry, member=key, label=members.label(key), product=p, before=before,
                                 after=after, delta=after-before, expected=expected, difference=difference,
                                 reason=reason, rank=rank, certainty=0))
    findings.sort(key=lambda f: (f["rank"], -abs(f["difference"]), f["label"], f["product"]))
    groups = []
    for finding in findings:
        related = [g for g in groups if set(g["events"]) & set(finding["events"])]
        group = {"findings": [finding], "events": list(finding["events"]), "pending": []}
        for other in related:
            group["findings"].extend(other["findings"])
            group["events"] = sorted(set(group["events"]) | set(other["events"]))
            groups.remove(other)
        groups.append(group)
    pending = []
    if findings and candidate_loader:
        # 候选身份解析隔离，不能改变有效转单的身份和数量结论。
        candidate_members = deepcopy(members)
        for kind, row in candidate_loader():
            identity = dict(row, 昵称=row.get("微信昵称") or row.get("订单昵称", ""))
            key = candidate_members.resolve(identity)
            if candidate_members.entries[key].get("unresolved"):
                continue
            related_products = {f["product"] for f in findings if f["member"] == key}
            if not related_products:
                continue
            content = " ".join(row.get(f, "") for f in ("消息原文", "消息引用", "内容", "商品转移"))
            mentioned = {p["商品名称"] for p in products if p["商品名称"] in content}
            if mentioned and not mentioned & related_products:
                continue
            affected = related_products & mentioned if mentioned else related_products
            issue = {"members": [key], "products": sorted(affected), "taint": False,
                     "reason": f"{kind}线索，仅辅助解释已有差额，不计入有效转单数量",
                     "evidence": [f"{members.label(key)}（{row.get('时间', '')}）：{row.get('消息原文') or row.get('内容', '')}"]}
            pending.append(issue)
            for group in groups:
                if any(f["member"] == key and f["product"] in affected for f in group["findings"]):
                    group["pending"].append(issue)
    lines = ["转单异常核查完成，以转单记录 CSV 为数量依据。"]
    if focus_products:
        lines.append("仅分析：" + "、".join(sorted(selected)) + "。")
    if not history_complete:
        lines.append("记录时间范围是否覆盖新旧订单之间的全部变化尚未证实。")
    if not findings:
        lines.append("未发现转单数量异常。")
    for number, group in enumerate(groups, 1):
        descriptions = dict.fromkeys(
            "订单内缺少商品" if f["difference"] < 0 else "订单内有多余商品"
            for f in group["findings"]
        )
        lines.append(f"\n{number}. " + "；".join(descriptions))
        for f in group["findings"]:
            lines.append(f"{f['label']}｜{f['product']}：旧数量{f['before']}，新数量{f['after']}，"
                         f"有效接收{f['incoming']}，有效转出{f['outgoing']}，"
                         f"应有数量{f['before'] + f['expected']}，差额{f['difference']:+d}。")
        evidence = [e for f in group["findings"] for e in f["evidence"]]
        lines.extend("- " + e for e in dict.fromkeys(evidence))
        for issue in group["pending"]:
            lines.append(issue["reason"] + "：")
            lines.extend("- " + e for e in issue["evidence"])
    return {"groups": groups, "findings": findings, "pending": pending, "text": "\n".join(lines),
            "focus_products": sorted(selected), "history_complete": history_complete}
