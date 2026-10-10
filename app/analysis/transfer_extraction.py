"""聊天转单规则提取；不依赖模型，不修改订单。"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from app.utils.csv_utils import read_csv_dict_rows
from app.utils.name_matching import match_names

INITIATION_WORDS = ("转", "掰", "合单", "核弹", "合", "分")
RECEIPT_WORDS = ("接", "姐", "截", "收", "1", "已")
EXCLUDED_RECEIPTS = ("接下来", "接单", "接受", "接龙", "接触", "接着", "接手",
                     "接待", "接送", "接电话", "接近", "接口", "接班", "接头")
PERSON_FIELDS = ("wxid", "单号", "群昵称", "微信昵称", "订单昵称")
TRANSFER_FIELDS = [*("发起人" + f for f in PERSON_FIELDS), *("接收人" + f for f in PERSON_FIELDS),
                   "消息原文", "引用对象", "消息引用", "转移范围", "商品转移", "确认状态", "问题备注",
                   "发起消息ID", "接收消息ID", "发起时间", "接收时间"]
CANDIDATE_FIELDS = ["消息ID", "时间", *PERSON_FIELDS, "内容", "消息原文", "引用对象", "消息引用",
                    "目标原文", "转移范围", "商品转移", "确认状态", "问题备注", "候选原因"]
FILES = ("transfer_records.csv", "candidate_initiations.csv", "candidate_receipts.csv")


def text_key(value):
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def split_message(row):
    content = row.get("内容", "")
    body, marker, tail = content.partition("引用：")
    if row.get("类型") != "引用消息":
        return content, "", "", ""
    target, separator, quote = tail.partition(": ")
    if not marker or not separator or not target or not quote:
        return content, "", "", "引用格式解析失败"
    return body, target, quote, ""


def is_initiation(text):
    return any(w in text for w in INITIATION_WORDS) and ("给" in text or "@" in text)


def has_receipt(text):
    return any(w in text for w in RECEIPT_WORDS)


def suspicious(text):
    return bool(re.search(r"不接|没接|不收|没收|未收|取消|撤回|不转|暂不|不要|只接|只收|部分|其中", text))


def serial_from_nickname(value):
    match = re.match(r"\s*(\d+)", value or "")
    return str(int(match[1])) if match else ""


def parse_quantity(value):
    if value.isdecimal():
        return int(value)
    digits = dict(zip("零一二三四五六七八九", range(10)))
    digits["两"] = 2
    if "十" in value:
        if not re.fullmatch(r"[一二两三四五六七八九]?十[一二三四五六七八九]?", value):
            return None
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(value)


def parse_products(text, products):
    """只解析目标之前的商品，防止 @23 等单号成为数量。"""
    notes = []
    clause = re.split(r"给|@", text, maxsplit=1)[0].strip()
    if re.search(r"整单|蒸蛋", clause):
        if re.search(r"除|不含|不包括|里的|其中|只", clause):
            return "未知", [], ["整单存在限定条件，需核实实际转移范围"]
        return "整单", [], notes
    clause = re.sub(r"^(?:转单|合单|核弹|转|掰|合|分)\s*", "", clause)
    # 在相邻的已知商品之间补分隔符，同时保留数量表达。
    names = sorted({p.get("商品名称", "") for p in products if p.get("商品名称")}, key=len, reverse=True)
    chunks = []
    for segment in re.split(r"[、，,；;\n+]+", clause):
        if names:
            pattern = "|".join(re.escape(n) for n in names)
            starts = [m.start() for m in re.finditer(pattern, segment)][1:]
            for start in reversed(starts):
                segment = segment[:start] + "、" + segment[start:]
        chunks.extend(s.strip() for s in segment.split("、") if s.strip())
    items = []
    for chunk in chunks:
        number = r"[\d零一二两三四五六七八九十]+"
        match = re.fullmatch(rf"(.+?)\s*(?:[xX×*]\s*)?({number})\s*(?:个|件|份|套|张|本|枚)?", chunk)
        prefix = re.fullmatch(rf"({number})\s*(?:个|件|份|套|张|本|枚)\s*(.+)", chunk)
        # 完整商品名称可含数字，优先识别名称。
        if any(chunk == n for n in names):
            raw, quantity, source = chunk, 1, "默认"
        elif match:
            raw, quantity, source = match[1].strip(), parse_quantity(match[2]), "明确"
        elif prefix:
            raw, quantity, source = prefix[2].strip(), parse_quantity(prefix[1]), "明确"
        else:
            raw, quantity, source = chunk, 1, "默认"
        candidates = match_names(raw, products, ("商品名称",))
        name = candidates[0]["商品名称"] if len(candidates) == 1 else None
        if not name:
            notes.append(f"商品“{raw}”" + ("匹配多个候选：" + "、".join(p["商品名称"] for p in candidates)
                                         if candidates else "未匹配商品配置"))
        if source == "默认":
            notes.append(f"“{raw}”未注明数量，按规则暂记1")
        if quantity is None or quantity <= 0 or re.search(r"\d\s*[-~～至.]\s*\d|[负-]\s*\d", chunk):
            notes.append(f"“{raw}”数量无效")
            quantity = None
        items.append({"商品原文": raw, "商品名称": name, "数量": quantity, "数量来源": source})
    if not items:
        notes.append("未识别商品及数量")
    return ("部分商品" if items else "未知"), items, notes


def person(row, identities, notes):
    result = {"wxid": row.get("wxid", ""), "单号": row.get("单号", ""),
              "群昵称": row.get("群昵称", ""), "微信昵称": row.get("昵称", row.get("微信昵称", "")),
              "订单昵称": row.get("订单昵称", "")}
    result["单号"] = str(result["单号"] or serial_from_nickname(result["群昵称"]))
    if result["wxid"]:
        matches = [p for p in identities if p.get("wxid") == result["wxid"]]
    elif result["单号"]:
        matches = [p for p in identities if str(p.get("serial", "")) == result["单号"]]
    else:
        names = {result["群昵称"], result["微信昵称"], result["订单昵称"], row.get("引用对象", "")} - {""}
        matches = [p for p in identities if names & {p.get("wechat_name"), p.get("name"), p.get("group_name")}]
    if len({p.get("wxid") for p in matches if p.get("wxid")}) > 1:
        notes.append("身份映射冲突：匹配到多个wxid")
        return result
    for dest, src in (("wxid", "wxid"), ("单号", "serial"), ("微信昵称", "wechat_name"),
                      ("订单昵称", "name"), ("群昵称", "group_name")):
        values = {str(p[src]) for p in matches if p.get(src) not in (None, "")}
        if len(values) > 1 or (result[dest] and values and result[dest] not in values):
            notes.append(f"身份映射冲突：{dest}")
        elif not result[dest] and len(values) == 1:
            result[dest] = values.pop()
    return result


def finish(record, notes):
    record["问题备注"] = "；".join(dict.fromkeys(notes))
    record["确认状态"] = "存在冲突" if any("冲突" in n for n in notes) else ("待核实" if notes else "规则匹配完整")
    return record


def extract_records(rows, products=(), identities=()):
    messages, seen = [], set()
    index = defaultdict(list)
    for number, row in enumerate(rows):
        if row.get("类型") not in {"文本", "引用消息"}:
            continue
        key = row.get("消息ID") or f"row:{number}"
        if key in seen:
            continue
        seen.add(key)
        body, target, quote, error = split_message(row)
        message = dict(row, body=body, target=target, quote=quote, error=error, key=key)
        messages.append(message)
        if not error:
            index[text_key(body)].append(message)
    quoted = {text_key(m["quote"]) for m in messages if m["quote"]}
    transfers, initiations, receipts = [], [], []
    linked = set()
    receipt_groups = defaultdict(list)

    def make_transfer(receiver=None, sender=None, quote="", target="", extra=()):
        notes = list(extra)
        record = dict.fromkeys(TRANSFER_FIELDS, "")
        record.update({"消息原文": receiver["body"] if receiver else sender["body"],
                       "引用对象": target, "消息引用": quote})
        for role, message in (("发起", sender), ("接收", receiver)):
            if message:
                identity = person(message, identities, notes)
                for field, value in identity.items():
                    record[role + "人" + field] = value
                record[role + "消息ID"] = message.get("消息ID", "")
                record[role + "时间"] = message.get("时间", "")
            elif role == "发起" and target:
                for field, value in person({"引用对象": target}, identities, notes).items():
                    record[role + "人" + field] = value
        scope, items, issues = parse_products(quote or sender["body"], products)
        record.update({"转移范围": scope, "商品转移": json.dumps(items, ensure_ascii=False)})
        notes.extend(issues)
        for role in ("发起人", "接收人"):
            missing = [f for f in PERSON_FIELDS if not record[role + f]]
            if missing:
                notes.append(role + "信息缺失：" + "、".join(missing))
        if receiver and (suspicious(receiver["body"]) or suspicious(quote)):
            notes.append("含否定、取消或部分接收表达，需核实")
        if receiver and any(p.get("商品名称") and p["商品名称"] in receiver["body"] for p in products):
            notes.append("接收回复另含商品说明，需核实是否部分接收；商品转移暂保留发起内容")
        if receiver and not any(w in receiver["body"] for w in ("接", "收")):
            notes.append("仅命中弱接收关键词，需核实")
        if receiver and sender:
            target_match = re.search(r"(?:给\s*@?|@)\s*(\d+)(?!\d)", quote)
            if (target_match and record["接收人单号"].isdigit()
                    and int(target_match[1]) != int(record["接收人单号"])):
                notes.append("接收人与发起消息目标单号冲突")
            if record["发起人wxid"] and record["发起人wxid"] == record["接收人wxid"]:
                notes.append("发起人与接收人相同，需核实")
        return finish(record, notes)

    for m in messages:
        if not (m["quote"] and has_receipt(m["body"]) and is_initiation(m["quote"])):
            continue
        matches = [s for s in index[text_key(m["quote"])] if s["key"] != m["key"]
                   and (not s.get("时间") or not m.get("时间") or s["时间"] <= m["时间"])]
        if len(matches) > 1:
            named = [s for s in matches if m["target"] in (s.get("群昵称"), s.get("昵称"))]
            if named:
                matches = named
        sender = matches[0] if len(matches) == 1 else None
        notes = [] if sender else ["发起消息未找到" if not matches else "发起消息存在多个精准匹配，无法唯一确定"]
        if sender:
            linked.add(sender["key"])
        record = make_transfer(m, sender, m["quote"], m["target"], notes)
        transfers.append(record)
        receipt_groups[sender["key"] if sender else "quote:" + text_key(m["quote"])].append(record)
    for group in receipt_groups.values():
        if len(group) > 1:
            for record in group:
                finish(record, [record["问题备注"], "同一发起内容存在多条接收回复，需核实重复或部分接收"])
    for m in messages:
        if m["error"]:
            receipts.append(make_candidate(m, products, identities, "引用格式解析失败，需人工核实"))
        elif is_initiation(m["body"]) and m["key"] not in linked:
            if text_key(m["body"]) in quoted:
                transfers.append(make_transfer(sender=m, extra=["已被引用，缺少可唯一关联的有效接收回复"]))
            else:
                initiations.append(make_candidate(m, products, identities, "含发起及目标关键词，未被引用"))
        if m.get("类型") == "文本" and "引用：" not in m["body"]:
            remainder = m["body"]
            for word in EXCLUDED_RECEIPTS:
                remainder = remainder.replace(word, "")
            if "接" in remainder:
                receipts.append(make_candidate(m, products, identities, "无引用且包含独立接收关键词“接”"))
    return transfers, initiations, receipts


def make_candidate(message, products, identities, reason):
    notes = [message["error"]] if message["error"] else []
    record = {k: message.get(k, "") for k in ("消息ID", "时间", "内容")}
    record.update(person(message, identities, notes))
    record.update({"消息原文": message["body"], "引用对象": message["target"], "消息引用": message["quote"],
                   "目标原文": "", "转移范围": "未知", "商品转移": "[]", "候选原因": reason})
    if not message["error"] and is_initiation(message["body"]):
        scope, items, issues = parse_products(message["body"], products)
        target = re.search(r"(?:给|@)(.*)", message["body"], re.S)
        record.update({"转移范围": scope, "商品转移": json.dumps(items, ensure_ascii=False),
                       "目标原文": target[1].strip() if target else ""})
        notes.extend(issues)
    notes.append("候选记录，尚未确认转单关系")
    return finish(record, notes)


def write_extraction(chat_path, output_dir, *, products=(), identities=()):
    rows, fields = read_csv_dict_rows(chat_path)
    if not {"类型", "内容"}.issubset(fields):
        raise ValueError("聊天记录缺少类型或内容列")
    groups = extract_records(rows, products, identities)
    counts = {}
    for name, records, columns in zip(FILES, groups, (TRANSFER_FIELDS, CANDIDATE_FIELDS, CANDIDATE_FIELDS)):
        with (Path(output_dir) / name).open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
        counts[name] = len(records)
    counts["needs_review"] = sum(r["确认状态"] != "规则匹配完整" for r in groups[0])
    return counts
