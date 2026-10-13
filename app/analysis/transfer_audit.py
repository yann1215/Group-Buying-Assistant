"""接收驱动的双向转单核查。纯函数，不修改来源订单或聊天。"""
from collections import defaultdict
from datetime import datetime
import re

from app.analysis.transfer_extraction import (
    EXCLUDED_RECEIPTS, is_initiation, match_transfer_products, parse_products,
    parse_quantity, person, split_message, text_key,
)

META = {"单号", "昵称", "总金额"}
NEGATIVE = re.compile(r"不接|没接|未接|不收|没收|未收|取消|撤回|不转|暂不|不要")


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def receipt_text(body):
    text = body
    for word in (*EXCLUDED_RECEIPTS, "收款", "收货", "收快递", "收到快递", "收集", "收入", "收据"):
        text = text.replace(word, "")
    return bool(re.search(r"接|收", text)) or text.strip() in {"1", "已", "姐", "截"}


class Members:
    """先按唯一订单昵称跨版本关联，再使用未冲突的单号。"""
    def __init__(self, old, new, identities):
        self.entries = {}
        self.identities = identities
        self.chat_keys = {}
        used = set()
        for index, row in enumerate(old):
            name = row.get("昵称", "")
            candidates = [i for i, other in enumerate(new) if name and other.get("昵称") == name]
            unique_old = sum(r.get("昵称") == name for r in old) == 1
            if len(candidates) != 1 or not unique_old:
                candidates = [i for i, other in enumerate(new) if str(other["单号"]) == str(row["单号"])
                              and (not name or not other.get("昵称") or name == other.get("昵称"))]
            match = candidates[0] if len(candidates) == 1 and candidates[0] not in used else None
            if match is not None:
                used.add(match)
            self.entries[f"old:{index}"] = {"old": row, "new": new[match] if match is not None else {}, "person": {}}
        for index, row in enumerate(new):
            if index not in used:
                self.entries[f"new:{index}"] = {"old": {}, "new": row, "person": {}}

    def resolve(self, row):
        notes = []
        identity = person(row, self.identities, notes)
        token = identity.get("wxid") or "|".join(identity.get(f, "") for f in ("单号", "群昵称", "微信昵称"))
        if token in self.chat_keys:
            return self.chat_keys[token]
        candidates = []
        if not any("冲突" in note for note in notes):
            names = {identity.get(f) for f in ("订单昵称", "群昵称", "微信昵称")} - {None, ""}
            candidates = [k for k, e in self.entries.items()
                          if names & {e[side].get("昵称") for side in ("old", "new")}]
            if not candidates and identity.get("单号"):
                candidates = [k for k, e in self.entries.items() if any(
                    str(e[side].get("单号", "")) == identity["单号"] for side in ("old", "new"))]
        key = candidates[0] if len(candidates) == 1 else f"chat:{token or len(self.entries)}"
        if key not in self.entries:
            verified_absence = not candidates and not notes and bool(identity.get("订单昵称")) and any(
                i.get("wxid") == identity.get("wxid") and i.get("name") == identity["订单昵称"]
                for i in self.identities)
            self.entries[key] = {"old": {}, "new": {}, "person": {}, "unresolved": not verified_absence}
        existing = self.entries[key]["person"].get("wxid")
        if existing and identity.get("wxid") and existing != identity["wxid"]:
            self.entries[key]["unresolved"] = True
        self.entries[key]["person"].update({k: v for k, v in identity.items() if v})
        self.chat_keys[token] = key
        return key

    def label(self, key):
        entry = self.entries[key]
        if entry["new"] and not entry.get("unresolved"):
            return "单号" + str(entry["new"]["单号"])
        p = entry["person"]
        if p.get("群昵称"):
            return "群昵称" + p["群昵称"]
        if p.get("微信昵称"):
            return "微信昵称" + p["微信昵称"]
        return "身份待核实（订单昵称" + str(entry["old"].get("昵称") or "未知") + "）"

    def targeted(self, body, key):
        p = self.entries[key]["person"]
        aliases = {p.get(f) for f in ("wxid", "单号", "群昵称", "微信昵称")} - {None, ""}
        return any(re.search(r"@\s*" + re.escape(a) + (r"(?!\d)" if a.isdecimal() else r"(?=$|[\s，,。；;、])"), body)
                   for a in aliases)


def transfer_scope(body, products):
    clause = re.split(r"给|@", body, maxsplit=1)[0].strip()
    exclusion = re.search(r"(?:整单|蒸蛋)(?:除|不含|不包括)(.+)", clause)
    if exclusion:
        excluded = re.sub(r"(?:以外|之外|外)$", "", exclusion[1]).strip()
        _, items, notes = parse_products("转" + excluded, products)
        if items and all(i.get("商品名称") for i in items):
            return "整单", [dict(i, 排除=True) for i in items], []
        return "未知", items, notes
    # 明确的“合单给@成员”视为整单；带商品限定的合单仍按商品解析。
    if re.fullmatch(r"(?:整单)?(?:合单|核弹)(?:转)?", clause):
        return "整单", [], []
    return parse_products(body, products)


def accepted_scope(body, scope, items, products):
    detail = re.sub(r"^(?:我)?(?:已|只|仅|再)?(?:接收|收到|接|收|姐|截)(?:了)?\s*", "", body.strip()).strip(" 。！!")
    if detail in {"", "1", "已", "了", "好", "好的"}:
        # “接1”是明确数量；裸“1”才是引用确认。
        if detail != "1" or body.strip() == "1":
            return scope, items, []
    count = re.fullmatch(r"([\d零一二两三四五六七八九十]+)(?:个|件|份|套|张|本|枚)?", detail)
    if count and len(items) == 1:
        return "部分商品", [dict(items[0], 数量=parse_quantity(count[1]), 数量来源="明确")], []
    if detail in {"整单", "全部", "全接", "全收"}:
        return scope, items, []
    if detail and detail != body.strip():
        new_scope, new_items, notes = transfer_scope(detail, products)
        if new_items and all(i.get("商品名称") for i in new_items):
            original = {i["商品名称"]: i for i in items}
            for item in new_items:
                if item["数量来源"] == "默认" and item["商品名称"] in original and not re.search(r"只|仅|部分", body):
                    item.update({k: original[item["商品名称"]][k] for k in ("数量", "数量来源")})
            return new_scope, new_items, []
        return new_scope, new_items, ["接收范围无法确定"]
    return scope, items, []


def audit_transfers(old, new, chat, *, identities=(), focus_products=(), history_complete=False):
    names = sorted({f for row in old + new for f in row if f not in META})
    products = [{"商品名称": name} for name in names]
    selected = set(names)
    if focus_products:
        selected = set()
        for focus in focus_products:
            matches = match_transfer_products(focus["product_name"], products)
            if len(matches) != 1:
                raise ValueError(f"关注商品“{focus['product_name']}”无法唯一对应订单商品，请使用完整商品名称。")
            selected.add(matches[0]["商品名称"])
    members = Members(old, new, identities)
    messages, seen = [], set()
    for index, row in enumerate(chat):
        if row.get("类型") not in {"文本", "引用消息"}:
            continue
        mid = row.get("消息ID") or f"row:{index}"
        if mid in seen:
            continue
        seen.add(mid)
        body, target, quote, error = split_message(row)
        messages.append({"id": mid, "body": body.strip(), "quote": quote, "target": target,
                         "error": error, "time": row.get("时间", ""), "when": timestamp(row.get("时间")),
                         "member": members.resolve(row), "row": row})
    starts = [m for m in messages if not m["error"] and is_initiation(m["body"]) and not NEGATIVE.search(m["body"])]
    receipts = [m for m in messages if receipt_text(m["body"]) and (m["error"] or not is_initiation(m["body"]))
                and not NEGATIVE.search(m["body"])]
    cancellations = [m for m in messages if NEGATIVE.search(m["body"])]
    events, pending = [], []
    reserved = set()
    partially_reserved = set()

    def evidence(m, sender=None):
        result = []
        for role, msg in (("发起", sender), ("接收", m)):
            if msg:
                result.append(f"{role}消息{msg['id']}（{msg['time'] or '时间未知'}）：{msg['body']}")
        if m["quote"]:
            result.append("引用：" + m["quote"])
        return result

    def relevant(scope, items):
        if not focus_products:
            return True
        if scope == "整单":
            return True
        return any(i.get("商品名称") in selected or any(
            p["商品名称"] in selected for p in match_transfer_products(i.get("商品原文", ""), products)) for i in items)

    def uncertain(m, reason, candidates=(), scope="未知", items=(), taint=True):
        if not relevant(scope, items) and scope != "未知":
            return
        keys = {m["member"], *(s["member"] for s in candidates)}
        pending.append({"members": sorted(keys), "products": sorted({i["商品名称"] for i in items
                        if i.get("商品名称") in selected} or selected), "reason": reason,
                        "taint": taint,
                        "evidence": evidence(m) + [f"候选发起{s['id']}（{s['time']}）：{s['body']}" for s in candidates]})

    # 引用优先占用发起记录；无引用接收只能搜索早于接收且 @ 本人的发起。
    for m in sorted(receipts, key=lambda r: (not bool(r["quote"]), r["when"] or datetime.max, r["id"])):
        if m["error"]:
            uncertain(m, "引用格式无法解析，接收关系待核实")
            continue
        if m["quote"]:
            candidates = [s for s in starts if text_key(s["body"]) == text_key(m["quote"])
                          and s["when"] and m["when"] and s["when"] < m["when"]]
            named = [s for s in candidates if m["target"] in {
                s["row"].get("群昵称"), s["row"].get("昵称"), s["row"].get("微信昵称")}]
            if named:
                candidates = named
        else:
            candidates = [s for s in starts if (s["id"] not in reserved or s["id"] in partially_reserved) and s["when"] and m["when"]
                          and s["when"] < m["when"] and members.targeted(s["body"], m["member"])]
            # 有商品说明时用聊天中的商品范围消歧，不使用订单增量反推。
            _, detail, _ = accepted_scope(m["body"], "未知", [], products)
            specified = {i["商品名称"] for i in detail if i.get("商品名称")}
            if specified:
                candidates = [s for s in candidates if transfer_scope(s["body"], products)[0] == "整单"
                              or specified <= {i["商品名称"] for i in transfer_scope(s["body"], products)[1]}]
            compatible = []
            for candidate in candidates:
                offered_scope, offered_items, _ = transfer_scope(candidate["body"], products)
                _, accepted, _ = accepted_scope(m["body"], offered_scope, offered_items, products)
                limits = {i["商品名称"]: i["数量"] for i in offered_items
                          if i.get("数量来源") == "明确" and i.get("数量")}
                if offered_scope == "整单" or not any(i.get("数量来源") == "明确" and i.get("数量")
                       and i["商品名称"] in limits and i["数量"] > limits[i["商品名称"]] for i in accepted):
                    compatible.append(candidate)
            # 唯一但冲突的候选保留，后续报告数量冲突，不伪装为来源缺失。
            if compatible:
                candidates = compatible
        source = candidates[0] if len(candidates) == 1 else None
        source_text = source["body"] if source else m["quote"]
        scope, items, _ = transfer_scope(source_text, products) if source_text else ("未知", [], [])
        scope, items, issues = accepted_scope(m["body"], scope, items, products)
        if not relevant(scope, items) and scope != "未知":
            continue
        if focus_products and scope != "未知":
            items = [i for i in items if i.get("商品名称") in selected or (not i.get("商品名称") and any(
                p["商品名称"] in selected for p in match_transfer_products(i.get("商品原文", ""), products)))]
        if len(candidates) > 1:
            candidate_scopes = [transfer_scope(s["body"], products) for s in candidates]
            if focus_products and all(s != "未知" and not relevant(s, it) for s, it, _ in candidate_scopes):
                continue
            uncertain(m, "存在多条发起候选，无法唯一匹配", candidates, scope, items)
            continue
        if NEGATIVE.search(source_text):
            uncertain(m, "引用包含否定或取消，不能确定有效接收", candidates, scope, items)
            continue
        if source and any(c["quote"] and text_key(c["quote"]) == text_key(source["body"])
                          and c["member"] in {source["member"], m["member"]}
                          and c["when"] and m["when"] and c["when"] > m["when"] for c in cancellations):
            continue
        if source and any(c["quote"] and text_key(c["quote"]) == text_key(source["body"])
                          and c["member"] in {source["member"], m["member"]}
                          and c["when"] and source["when"] and m["when"]
                          and source["when"] < c["when"] <= m["when"] for c in cancellations):
            issues.append("发起后已有取消记录，后续接收是否重新生效待核实")
        if source and "@" in source["body"] and not members.targeted(source["body"], m["member"]):
            issues.append("接收人与发起消息 @ 目标不一致")
        if source and source["member"] == m["member"]:
            issues.append("发起人与接收人相同")
        if not source:
            # 引用原文可独立证明接收量，但发起身份仍需追查。
            uncertain(m, "未匹配到发起消息，转出方待核实", (), scope, items, taint=False)
        if not m["when"] or (not m["quote"] and m["body"] in {"1", "已", "姐", "截"}):
            issues.append("缺少可靠时间或明确接收表达")
        if issues or scope == "未知" or (scope != "整单" and any(
                not i.get("商品名称") or not i.get("数量") for i in items)):
            uncertain(m, "；".join(issues) or "商品或数量无法确定", candidates, scope, items)
            continue
        if source:
            reserved.add(source["id"])
            if re.search(r"只|仅|部分|再接|再收", m["body"]):
                partially_reserved.add(source["id"])
        offered_scope, offered, _ = transfer_scope(source_text, products) if source_text else ("未知", [], [])
        if offered_scope == "部分商品" and any(i["商品名称"] not in {o["商品名称"] for o in offered} for i in items):
            uncertain(m, "接收商品与发起范围冲突", candidates, scope, items)
            continue
        events.append({"sender": source["member"] if source else None, "receiver": m["member"],
                       "source_id": source["id"] if source else "quote:" + m["quote"] if m["quote"] else m["id"],
                       "receipt_id": m["id"], "time": m["time"], "when": m["when"],
                       "scope": scope, "items": items, "evidence": evidence(m, source),
                       "offered": offered,
                       "certainty": 0 if m["quote"] else 1,
                       "partial": bool(re.search(r"只|仅|部分|再接|再收", m["body"]))})

    # 无引用的撤销不能猜测其目标，但应阻止关联成员的数量被当作确定结论。
    for cancellation in cancellations:
        if cancellation["quote"] or not re.search(r"取消|撤回|不转了|不接了|不收了", cancellation["body"]):
            continue
        related = [event for event in events if cancellation["member"] in {event["sender"], event["receiver"]}
                   and cancellation["when"] and event["when"] and cancellation["when"] > event["when"]]
        if related:
            affected = {k for event in related for k in (event["sender"], event["receiver"]) if k}
            pending.append({"members": sorted(affected), "products": sorted(selected),
                            "reason": "存在无引用的取消或撤回，需确认撤销的转单", "evidence": evidence(cancellation)})

    ledger = defaultdict(lambda: {"incoming": 0, "outgoing": 0, "default": False,
                                  "certainty": 0, "evidence": [], "events": []})
    balances = {(k, p): int(e["old"].get(p) or 0) for k, e in members.entries.items() for p in selected}
    tainted = {(k, p) for issue in pending if issue.get("taint", True) for k in issue["members"] for p in issue["products"]}
    default_balances = set()
    applied = {}
    allocated = defaultdict(int)
    for event in sorted(events, key=lambda e: (e["when"] or datetime.max, e["receipt_id"])):
        sender, receiver = event["sender"], event["receiver"]
        key = (event["source_id"], receiver)
        signature = (event["scope"], tuple((i.get("商品名称"), i.get("数量"), i.get("数量来源")) for i in event["items"]))
        if key in applied and applied[key] == signature and not event["partial"]:
            continue  # 同一发起、同一接收人的重复确认不重复入账。
        if key in applied and not event["partial"]:
            issue = {"members": [k for k in (sender, receiver) if k], "products": sorted(selected),
                     "reason": "同一发起存在不一致的接收回复，需核实分批或更正", "evidence": event["evidence"]}
            pending.append(issue)
            tainted.update((k, p) for k in issue["members"] for p in selected)
            continue
        applied[key] = signature
        items = event["items"]
        overallocated = any(o.get("数量来源") == "明确" and o.get("数量") is not None
                            and allocated[event["source_id"], o["商品名称"]] + sum(
                                i["数量"] for i in items if i["商品名称"] == o["商品名称"]) > o["数量"]
                            for o in event["offered"])
        if overallocated:
            pending.append({"members": [k for k in (sender, receiver) if k], "products": sorted(selected),
                            "reason": "累计接收超过发起的明确数量，需核实重复或部分接收", "evidence": event["evidence"]})
            tainted.update((k, p) for k in (sender, receiver) if k for p in selected)
            continue
        if event["scope"] == "整单":
            simultaneous = any(other is not event and other["when"] == event["when"]
                               and sender in {other["sender"], other["receiver"]} for other in events)
            if not sender or simultaneous or any((sender, p) in tainted for p in selected) or members.entries[sender].get("unresolved"):
                pending.append({"members": [k for k in (sender, receiver) if k], "products": sorted(selected),
                                "reason": "整单数量无法根据旧订单及前序转单可靠展开", "evidence": event["evidence"]})
                tainted.update((k, p) for k in (sender, receiver) if k for p in selected)
                continue
            excluded = {i["商品名称"] for i in items if i.get("排除")}
            items = [{"商品名称": p, "数量": balances.get((sender, p), 0),
                      "数量来源": "默认" if (sender, p) in default_balances else "整单推导"}
                     for p in sorted(selected - excluded) if balances.get((sender, p), 0) > 0]
        for item in items:
            product, quantity = item["商品名称"], item["数量"]
            if product not in selected:
                continue
            allocated[event["source_id"], product] += quantity
            for member, direction, sign in ((sender, "outgoing", -1), (receiver, "incoming", 1)):
                if member is None:
                    continue
                entry = ledger[member, product]
                entry[direction] += quantity
                entry["default"] |= item["数量来源"] == "默认"
                entry["certainty"] = max(entry["certainty"], event["certainty"])
                entry["evidence"].extend(event["evidence"])
                entry["events"].append(event["source_id"])
                balances[member, product] = balances.get((member, product), 0) + sign * quantity
                if entry["default"]:
                    default_balances.add((member, product))
                if balances[member, product] < 0:
                    tainted.add((member, product))

    findings = []
    for key, member in members.entries.items():
        for product in sorted(selected):
            before, after = (int(member[side].get(product) or 0) for side in ("old", "new"))
            entry = ledger.get((key, product))
            if entry is None and before == after:
                continue
            entry = entry or {"incoming": 0, "outgoing": 0, "default": False, "certainty": 2,
                              "evidence": [], "events": []}
            expected = entry["incoming"] - entry["outgoing"]
            delta, difference = after - before, after - before - expected
            uncertain_quantity = (key, product) in tainted or member.get("unresolved")
            if uncertain_quantity:
                rank, reason = 3, "存在未决接收、身份或前序数量问题，差额待核实"
            elif not difference and not entry["default"]:
                continue
            elif not difference:
                rank, reason = 4, "数量吻合，但未注明数量的商品按1个暂算"
            elif entry["default"]:
                rank, reason = 2, "数量依赖默认值，订单变化与暂算数量不一致"
            elif not entry["events"]:
                rank = 1 if delta > 0 and history_complete else 3
                reason = "当前聊天范围内未找到接收依据，商品却增加" if delta > 0 else "商品减少但没有有效转单依据"
            elif difference > 0:
                rank, reason = 1, "转出后未减或少减，商品仍有多记" if expected < 0 else "订单增加超过有效转单数量"
            else:
                rank, reason = 2, "订单少加或多减"
            findings.append({"rank": rank, "certainty": entry["certainty"], "member": key,
                             "label": members.label(key), "product": product, "before": before, "after": after,
                             "delta": delta, "incoming": entry["incoming"], "outgoing": entry["outgoing"],
                             "expected": expected, "difference": difference, "default": entry["default"],
                             "reason": reason, "evidence": list(dict.fromkeys(entry["evidence"])),
                             "events": sorted(set(entry["events"]))})
    # 同一转单事件的双端问题合并；同一成员/商品已由 ledger 汇总，差额不相加。
    groups = []
    for finding in findings:
        connected = [g for g in groups if set(g["events"]) & set(finding["events"])]
        group = {"findings": [finding], "events": finding["events"][:], "pending": []}
        for other in connected:
            group["findings"].extend(other["findings"])
            group["events"] = sorted(set(group["events"]) | set(other["events"]))
            groups.remove(other)
        groups.append(group)
    for issue in pending:
        if not issue["products"]:
            continue
        related = [g for g in groups if any(f["member"] in issue["members"] and f["product"] in issue["products"]
                                          for f in g["findings"])]
        if related:
            related[0]["pending"].append(issue)
        else:
            groups.append({"findings": [], "events": [], "pending": [issue]})

    def sort_key(group):
        fs = group["findings"]
        rank = min((f["rank"] for f in fs), default=3)
        strongest = [f for f in fs if f["rank"] == rank]
        serials = [int(members.entries[f["member"]]["new"]["单号"]) for f in fs
                   if members.entries[f["member"]]["new"]]
        times = [event["time"] for event in events if event["source_id"] in group["events"]]
        return (rank, min((f["certainty"] for f in strongest), default=2),
                -max((abs(f["difference"]) for f in strongest), default=0),
                -len({(f["member"], f["product"]) for f in fs}), min(serials, default=10**12),
                min(times, default=""), str(group["events"]))
    groups.sort(key=sort_key)
    lines = [f"转单异常核查完成，仅分析：{'、'.join(sorted(selected))}。" if focus_products else "转单异常核查完成。"]
    if not history_complete:
        lines.append("聊天时间范围是否覆盖新旧订单之间的全部变化尚未证实；无记录的增减列为待核实。")
    if not groups:
        lines.append("未发现转单数量异常。仅发起但未被接收的记录不计入转单。")
    for number, group in enumerate(groups, 1):
        lines.append(f"\n{number}. " + ("；".join(dict.fromkeys(f["reason"] for f in group["findings"])) or "接收记录待核实"))
        for f in sorted(group["findings"], key=lambda f: (f["rank"], f["label"], f["product"])):
            lines.append(f"{f['label']}｜{f['product']}：旧数量{f['before']}，新数量{f['after']}，实际变化{f['delta']:+d}；"
                         f"有效接收{f['incoming']}，有效转出{f['outgoing']}，预计变化{f['expected']:+d}，"
                         f"{'暂算' if f['default'] or f['rank'] == 3 else ''}差额{f['difference']:+d}。")
        for issue in group["pending"]:
            lines.append("、".join(members.label(k) for k in issue["members"]) + "：" + issue["reason"] + "。")
        ev = [e for f in group["findings"] for e in f["evidence"]] + [e for i in group["pending"] for e in i["evidence"]]
        lines.extend("依据：" + e for e in dict.fromkeys(ev))
    return {"groups": groups, "findings": findings, "pending": pending, "text": "\n".join(lines),
            "focus_products": sorted(selected), "history_complete": history_complete}
