"""专用分析接口：分段提取事件、程序核对数量、带证据审阅差异。"""
from __future__ import annotations

import csv
import io
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from app.analysis.order_parser import parse_order_file
from app.analysis.order_compare import file_signature
from app.config import DEFAULT_MODEL, get_resource_path, TRANSFER_MAX_TOKENS
from app.llm.knowledge import load_knowledge, select_transfer_knowledge
from app.llm.schemas.transfer import TransferResult


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def prepare_analysis_payload(payload, metadata):
    """证据编号来自固定输入，不改变已有 CSV；完整记录只有签名有效才使用。"""
    payload = dict(payload)
    payload["knowledge"] = load_knowledge()
    payload["differences"] = [{**row, "id": f"D{i:05}"}
                              for i, row in enumerate(csv_rows(payload["comparison_csv"]), 1)]
    source = payload["chat_history_csv"]
    payload["chat_coverage"] = "关键词筛选记录，可能缺少确认和前后文"
    raw_path = metadata.get("raw_path")
    if raw_path and metadata.get("raw_signature"):
        path = Path(raw_path).resolve()
        if path.parent != Path(payload["chat_history_path"]).resolve().parent:
            raise ValueError("完整聊天记录不属于当前工作目录")
        if file_signature(path) != metadata["raw_signature"]:
            raise ValueError("完整聊天记录内容已变化，请重新获取聊天记录。")
        source = path.read_text(encoding="utf-8-sig")
        if file_signature(path) != metadata["raw_signature"]:
            raise ValueError("读取期间完整聊天记录发生变化，请重新获取。")
        payload["chat_coverage"] = "同次导出的完整文本及引用消息；其他类型未解析"
    payload["messages"] = [{**row, "id": f"M{i:06}"}
                           for i, row in enumerate(csv_rows(source), 1)
                           if row.get("类型") in {"文本", "引用消息"}]
    with tempfile.TemporaryDirectory() as directory:
        orders = {}
        for label, key in (("old", "old_order"), ("new", "new_order")):
            parsed = parse_order_file(payload[key], Path(directory) / f"{label}.csv")
            orders[label] = csv_rows(Path(parsed).read_text(encoding="utf-8-sig"))
    payload["orders"] = orders
    return payload


def bounded_batches(rows, limit=6000, overlap=0):
    batch = []
    size = 0
    for row in rows:
        length = len(json.dumps(row, ensure_ascii=False))
        if length > limit:
            raise ValueError("单条分析资料过长，不能完整放入模型上下文。")
        if batch and size + length > limit:
            yield batch
            batch = batch[-overlap:] if overlap else []
            while batch and sum(len(json.dumps(r, ensure_ascii=False)) for r in batch) + length > limit:
                batch.pop(0)
            size = sum(len(json.dumps(r, ensure_ascii=False)) for r in batch)
        batch.append(row)
        size += length
    if batch:
        yield batch


def check_refs(result, messages, differences, knowledge):
    message_ids = {r["id"] for r in messages}
    order_ids = {r["id"] for r in differences}
    knowledge_ids = {r["id"] for r in knowledge}
    for item in [*result.events, *result.findings]:
        if not set(item.message_refs) <= message_ids or not set(item.knowledge_refs) <= knowledge_ids:
            raise ValueError("模型引用了不存在的聊天或知识证据。")
    for finding in result.findings:
        if not set(finding.order_refs) <= order_ids:
            raise ValueError("模型引用了不存在的订单差异。")
        if finding.status != "unresolved" and not finding.message_refs:
            raise ValueError("模型结论缺少聊天证据。")


def quantity(value):
    value = str(value).strip()
    return int(value) if value.isdecimal() else None


class TransferAnalyzer:
    def __init__(self, client):
        self.client = client
        self.last_result = None

    def analyze(self, payload):
        self.last_result = None
        prompt = get_resource_path("app/llm/prompts/transfer.md").read_text(encoding="utf-8")
        knowledge = select_transfer_knowledge(payload["knowledge"]["entries"], json.dumps(
            {"messages": payload["messages"], "focus_products": payload["focus_products"], "orders": payload["orders"]},
            ensure_ascii=False))
        orders = payload["orders"]
        products = sorted({k for rows in orders.values() for row in rows for k in row
                           if k not in {"单号", "昵称", "总金额"}})
        identities = {label: [{"单号": r["单号"], "昵称": r.get("昵称", "")}
                              for r in rows] for label, rows in orders.items()}
        base = {"knowledge": knowledge, "identities": identities, "products": products,
                "focus_products": payload["focus_products"], "chat_coverage": payload["chat_coverage"]}
        if len(json.dumps(base, ensure_ascii=False)) > 12000:
            raise ValueError("订单身份或知识资料过长，请缩小分析范围。未静默截断资料。")
        messages = payload["messages"]
        events, seen, limitations, model_notes = [], set(), [], []
        for batch in bounded_batches(messages, overlap=3):
            result = self.client.structured(prompt, {**base, "stage": "extract", "messages": batch},
                                            TransferResult, max_tokens=TRANSFER_MAX_TOKENS)
            if result.stage != "extract":
                raise ValueError("模型返回错误的分析阶段。")
            check_refs(result, batch, [], knowledge)
            for event in result.events:
                item = event.model_dump()
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    events.append(item)
            model_notes.extend(result.limitations)
        old = {r["单号"]: r for r in orders["old"]}
        new = {r["单号"]: r for r in orders["new"]}
        known = set(old) | set(new)
        message_map = {r["id"]: r for r in messages}
        expected = defaultdict(int)
        stock = {(serial, product): quantity(row.get(product, "0")) or 0
                 for serial, row in old.items() for product in products}
        counted = set()
        for event in sorted(events, key=lambda e: min(e["message_refs"])):
            sender, receiver, product = event["sender_order"], event["receiver_order"], event["product"]
            if sender not in known and sender is not None or receiver not in known and receiver is not None:
                raise ValueError("模型编造了订单单号。")
            if product is not None and product not in products:
                raise ValueError("模型编造了商品名称。")
            if event["state"] != "confirmed" or not all((sender, receiver, product, event["quantity"])):
                continue
            if any(e["state"] in {"cancelled", "uncertain"} and e["product"] == product
                   and e["sender_order"] == sender and e["receiver_order"] == receiver for e in events):
                limitations.append("同一组转单存在取消或不确定描述，未自动计入数量。")
                continue
            if sender == receiver:
                limitations.append("同一单号的转出和接收无法直接计入转单数量。")
                continue
            # 跨段重复事件只计一次；共享证据的矛盾解析也不能重复累加。
            refs = set(event["message_refs"])
            if any(product == p and refs & existing for p, existing in counted):
                limitations.append("共享消息证据的事件需要人工核实，数量未重复计入。")
                continue
            evidence = "\n".join(" ".join(message_map[ref].get(field, "")
                                         for field in ("内容", "群昵称", "昵称")) for ref in refs)
            supported = True
            for serial in (sender, receiver):
                nick = (old.get(serial) or new.get(serial)).get("昵称", "")
                unique_nick = nick and len({r["单号"] for rows in orders.values() for r in rows if r.get("昵称") == nick}) == 1
                serial_text = re.escape(serial)
                explicit_serial = any(
                    re.match(rf"\s*0*{serial_text}(?!\d)", message_map[ref].get("群昵称", ""))
                    or re.search(rf"(?:单号|订单|@)\s*0*{serial_text}(?!\d)", message_map[ref].get("内容", ""))
                    for ref in refs)
                if not explicit_serial and not (unique_nick and nick in evidence):
                    supported = False
            if not supported:
                limitations.append("事件中的人员缺少可唯一定位的单号或昵称证据，未计入数量核对。")
                continue
            if stock.get((sender, product), 0) < event["quantity"]:
                limitations.append(f"单号{sender}的{product}在已识别时间线中数量不足；需核实漏掉的接单或订单修改。")
                continue
            counted.add((product, frozenset(refs)))
            expected[sender, product] -= event["quantity"]
            expected[receiver, product] += event["quantity"]
            stock[sender, product] = stock.get((sender, product), 0) - event["quantity"]
            stock[receiver, product] = stock.get((receiver, product), 0) + event["quantity"]
        differences = payload["differences"]
        numeric = {}
        for row in differences:
            serial, field = row.get("单号"), row.get("变动商品") or row.get("变化字段")
            if field not in products or not serial:
                continue
            before = quantity(old.get(serial, {}).get(field, "0"))
            after = quantity(new.get(serial, {}).get(field, "0"))
            if before is not None and after is not None:
                numeric[row["id"]] = {"actual_delta": after - before, "event_delta": expected[serial, field]}
        findings = []
        for batch in bounded_batches(differences, limit=4000):
            serials = {r.get("单号") for r in batch}
            selected = [e for e in events if e["sender_order"] in serials or e["receiver_order"] in serials
                        or e["sender_order"] is None or e["receiver_order"] is None]
            refs = {ref for e in selected for ref in e["message_refs"]}
            context = {**base, "stage": "review", "differences": batch, "events": selected,
                       "messages": [message_map[ref] for ref in sorted(refs)],
                       "quantity_checks": {r["id"]: numeric[r["id"]] for r in batch if r["id"] in numeric}}
            if len(json.dumps(context, ensure_ascii=False)) > 22000:
                raise ValueError("关联转单事件过多，请缩小聊天时间范围后分析。")
            result = self.client.structured(prompt, context, TransferResult, max_tokens=TRANSFER_MAX_TOKENS)
            if result.stage != "review":
                raise ValueError("模型返回错误的审阅阶段。")
            check_refs(result, context["messages"], batch, knowledge)
            if {ref for f in result.findings for ref in f.order_refs} != {r["id"] for r in batch}:
                raise ValueError("模型未完整核查本批订单差异。")
            for finding in result.findings:
                item = finding.model_dump()
                if item["status"] == "matched":
                    related_serials = {r.get("单号") for r in batch if r["id"] in item["order_refs"]}
                    supporting = {ref for event in selected if event["state"] == "confirmed"
                                  and (event["sender_order"] in related_serials or event["receiver_order"] in related_serials)
                                  for ref in event["message_refs"]}
                    item["message_refs"] = sorted(set(item["message_refs"]) | supporting)
                if item["status"] == "matched" and any(
                    numeric[ref]["actual_delta"] != numeric[ref]["event_delta"]
                    for ref in item["order_refs"] if ref in numeric
                ):
                    item["status"] = "unresolved"
                    item["summary"] = "程序数量核对未通过，需要核实。" + item["summary"]
                findings.append(item)
            model_notes.extend(result.limitations)
        # 已确认事件应涵盖未变化的单号，否则仍存在缺少订单变更的可能。
        missing = []
        for (serial, product), delta in expected.items():
            actual = (quantity(new.get(serial, {}).get(product, "0")) or 0) - (quantity(old.get(serial, {}).get(product, "0")) or 0)
            if delta != actual:
                missing.append(f"单号{serial}，{product}：订单变化{actual:+d}，已识别转单净变化{delta:+d}，待核实")
        limitations.extend(missing)
        limitations.extend([payload["chat_coverage"], "聊天分段保留三条重叠上下文，跨段远距离确认、取消或改口可能无法关联。",
                            "未检出异常不代表全部转单正确；未匹配人员、商品或数量的事件仍需人工核查。"])
        self.last_result = {"model": getattr(self.client, "model", DEFAULT_MODEL),
                            "knowledge_sha256": payload["knowledge"]["sha256"],
                            "knowledge_sections": [e["id"] for e in knowledge],
                            "events": events, "findings": findings, "quantity_checks": numeric,
                            "unverified_model_notes": list(dict.fromkeys(model_notes)),
                            "limitations": list(dict.fromkeys(limitations)),
                            "coverage": {"start": payload["chat_start"], "end": payload["chat_end"], "messages": len(messages)}}
        lines = [f"转单分析完成：分析{len(messages)}条文本/引用消息，识别{len(events)}条事件。"]
        for index, f in enumerate(findings, 1):
            label = {"matched": "已匹配", "suspected": "疑似异常", "unresolved": "待核实"}[f["status"]]
            lines.append(f"{index}. [{label}] {f['summary']}\n证据：订单 {', '.join(f['order_refs'])}；聊天 {', '.join(f['message_refs']) or '未找到'}；知识 {', '.join(f['knowledge_refs'])}")
        if not findings:
            lines.append("没有可核查的订单差异，转单事件仍需人工核实。")
        lines.append("分析范围与限制：\n" + "\n".join(self.last_result["limitations"]))
        return "\n\n".join(lines)
