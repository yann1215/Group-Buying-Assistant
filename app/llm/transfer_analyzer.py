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
from app.config import DEFAULT_MODEL, get_resource_path
from app.llm.knowledge import load_knowledge, select_transfer_knowledge
from app.llm.transfer_batches import BatchRunner


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


def bounded_batches(rows, limit=6000, overlap=0, max_rows=None):
    batch = []
    size = 0
    for row in rows:
        length = len(json.dumps(row, ensure_ascii=False))
        if batch and (size + length > limit or max_rows is not None and len(batch) >= max_rows):
            yield batch
            batch = batch[-overlap:] if overlap else []
            while batch and (sum(len(json.dumps(r, ensure_ascii=False)) for r in batch) + length > limit
                             or max_rows is not None and len(batch) >= max_rows):
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

    def analyze(self, payload, progress_callback=None, diagnostics_dir=None):
        self.last_result = None
        prompts = {stage: get_resource_path(f"app/llm/prompts/transfer_{stage}.md").read_text(encoding="utf-8")
                   for stage in ("extract", "review")}
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
        known_orders = {r["单号"] for rows in orders.values() for r in rows}

        def validate(result, batch_messages, differences, rules):
            check_refs(result, batch_messages, differences, rules)
            reviewed = [ref for finding in result.findings for ref in finding.order_refs]
            if len(reviewed) != len(set(reviewed)):
                raise ValueError("模型重复核查同一订单差异，不能合并矛盾结论")
            for event in result.events:
                if any(serial is not None and serial not in known_orders
                       for serial in (event.sender_order, event.receiver_order)):
                    raise ValueError("模型编造了订单单号")
                if event.product is not None and event.product not in products:
                    raise ValueError("模型编造了商品名称")

        runner = BatchRunner(self.client, prompts, validate, diagnostics_dir, progress_callback)
        events, seen, limitations, model_notes = [], set(), [], []
        message_batches = list(bounded_batches(messages, limit=3000, overlap=3, max_rows=12))
        for index, batch in enumerate(message_batches, 1):
            if progress_callback:
                progress_callback(f"正在提取转单事件：第 {index}/{len(message_batches)} 批聊天……")
            for result in runner.run({**base, "stage": "extract", "messages": batch}):
                for event in result.events:
                    item = event.model_dump()
                    # 相同事件的不同说明和引用顺序不能变成新事件。
                    key = json.dumps({k: sorted(v) if k == "message_refs" else v
                                      for k, v in item.items() if k not in {"explanation", "knowledge_refs"}},
                                     ensure_ascii=False, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        events.append(item)
                model_notes.extend(result.limitations)
        represented = {ref for event in events for ref in event["message_refs"]}
        candidate_ids = [r["id"] for r in messages if r["id"] not in represented
                         and re.search(r"转.{0,30}(?:给|@)|合单给|取消转", r.get("内容", ""))]
        if candidate_ids:
            runner.failures.append({"stage": "extract", "ids": candidate_ids,
                                    "error": "疑似转单消息没有对应提取事件，需人工核实；候选词匹配不代表已确认转单。"})
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
            if any(e["product"] == product and set(e["message_refs"]) & set(event["message_refs"])
                   and (e["sender_order"], e["receiver_order"], e["quantity"]) != (sender, receiver, event["quantity"])
                   for e in events):
                limitations.append("共享证据存在人员或数量冲突，相关事件均未计入数量。")
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
        remaining = []
        for row in differences:
            check = numeric.get(row["id"])
            product = row.get("变动商品") or row.get("变化字段")
            supporting = [e for e in events if e["state"] == "confirmed" and e["product"] == product
                          and row.get("单号") in (e["sender_order"], e["receiver_order"])
                          and (product, frozenset(e["message_refs"])) in counted]
            if (check and check["actual_delta"] != 0 and check["actual_delta"] == check["event_delta"]
                    and supporting):
                # 只匹配通过身份、库存、取消及重复证据检查的净数量；无需模型重做算术。
                findings.append({"status": "unresolved", "quantity_status": "consistent",
                                 "relationship_status": "unverified",
                                 "summary": f"单号{row['单号']}，{product}：按已识别事件计算净数量一致（{check['actual_delta']:+d}）；转单人员及逐笔关系待核实。",
                                 "order_refs": [row["id"]],
                                 "message_refs": sorted({ref for e in supporting for ref in e["message_refs"]}),
                                 "knowledge_refs": sorted({ref for e in supporting for ref in e["knowledge_refs"]})})
            else:
                remaining.append(row)
        difference_batches = list(bounded_batches(remaining, limit=4000, max_rows=3))
        for index, batch in enumerate(difference_batches, 1):
            if progress_callback:
                progress_callback(f"正在核查订单差异：第 {index}/{len(difference_batches)} 批……")
            serials = {r.get("单号") for r in batch}
            selected = [e for e in events if e["sender_order"] in serials or e["receiver_order"] in serials
                        or e["sender_order"] is None or e["receiver_order"] is None]
            refs = {ref for e in selected for ref in e["message_refs"]}
            context = {**base, "stage": "review", "differences": batch, "events": selected,
                       "messages": [message_map[ref] for ref in sorted(refs)],
                       "quantity_checks": {r["id"]: numeric[r["id"]] for r in batch if r["id"] in numeric}}
            results = runner.run(context)
            batch_findings = [f for result in results for f in result.findings]
            model_notes.extend(note for result in results for note in result.limitations)
            covered = {ref for f in batch_findings for ref in f.order_refs}
            missing_rows = [r for r in batch if r["id"] not in covered]
            # 超限恢复已用完预算的差异不再发起新一轮补查。
            failed_ids = {ref for failure in runner.failures if failure["stage"] == "review" for ref in failure["ids"]}
            retry_rows = [r for r in missing_rows if r["id"] not in failed_ids]
            if retry_rows:
                if progress_callback:
                    progress_callback(f"正在补查模型漏答的 {len(missing_rows)} 条订单差异……")
                retry_context = {**context, "differences": retry_rows,
                                 "quantity_checks": {r["id"]: numeric[r["id"]] for r in retry_rows
                                                     if r["id"] in numeric}}
                # 只补查一次，保留已核查结果，避免反复重跑全部聊天。
                for retry in runner.run(retry_context):
                    batch_findings.extend(retry.findings)
                    model_notes.extend(retry.limitations)
                    covered.update(ref for f in retry.findings for ref in f.order_refs)
            for row in missing_rows:
                if row["id"] not in covered:
                    findings.append({"status": "unresolved", "analysis_status": "incomplete",
                                     "summary": f"模型仍未返回有效判断（漏答或批次失败），需人工核实：{row['id']}",
                                     "order_refs": [row["id"]], "message_refs": [], "knowledge_refs": []})
                    limitations.append("部分订单差异因漏答或批次失败未完成核查，已明确标为待核实。")
            for finding in batch_findings:
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
        for finding in findings:
            finding["relationship_status"] = "unverified"
            if any(f["stage"] == "extract" for f in runner.failures):
                finding["analysis_status"] = "incomplete"
                finding["summary"] = "聊天提取有未完成范围，以下仅基于已识别部分。" + finding["summary"]
                if finding.get("quantity_status") == "consistent":
                    finding["quantity_status"] = "partial_consistent"
            if finding["status"] == "matched":
                finding["status"] = "unresolved"
                finding["summary"] = "模型认为存在对应证据，人员及逐笔转单关系仍待核实。" + finding["summary"]
            if "quantity_status" not in finding:
                checks = [numeric[ref] for ref in finding["order_refs"] if ref in numeric]
                finding["quantity_status"] = ("different" if any(c["actual_delta"] != c["event_delta"] for c in checks)
                                               else "not_established")
        for failure in runner.failures:
            limitations.append(f"未完成批次 {failure['stage']}：{', '.join(failure['ids'])}；{failure['error']}")
        # 已确认事件应涵盖未变化的单号，否则仍存在缺少订单变更的可能。
        missing = []
        for (serial, product), delta in expected.items():
            actual = (quantity(new.get(serial, {}).get(product, "0")) or 0) - (quantity(old.get(serial, {}).get(product, "0")) or 0)
            if delta != actual:
                missing.append(f"单号{serial}，{product}：订单变化{actual:+d}，已识别转单净变化{delta:+d}，待核实")
        limitations.extend(missing)
        limitations.extend([payload["chat_coverage"], "聊天分段保留三条重叠上下文，跨段远距离确认、取消或改口可能无法关联。",
                            "净数量一致不代表人员及逐笔关系已核实，多笔净额相抵也不能证明转单正确。",
                            "未检出异常不代表全部转单正确；未匹配人员、商品或数量的事件仍需人工核查。"])
        for event in events:
            event["relationship_status"] = "unverified"
        self.last_result = {"model": getattr(self.client, "model", DEFAULT_MODEL),
                            "knowledge_sha256": payload["knowledge"]["sha256"],
                            "knowledge_sections": [e["id"] for e in knowledge],
                            "events": events, "findings": findings, "quantity_checks": numeric,
                            "unverified_model_notes": list(dict.fromkeys(model_notes)),
                            "limitations": list(dict.fromkeys(limitations)),
                            "failures": runner.failures,
                            "diagnostics_dir": str(diagnostics_dir) if diagnostics_dir else None,
                            "coverage": {"start": payload["chat_start"], "end": payload["chat_end"], "messages": len(messages),
                                         "processed_messages": len(runner.successful_message_ids),
                                         "unprocessed_message_ids": [r["id"] for r in messages if r["id"] not in runner.successful_message_ids],
                                         "unresolved_candidate_message_ids": candidate_ids,
                                         "complete": not runner.failures and not any(f.get("analysis_status") == "incomplete" for f in findings)}}
        complete = self.last_result["coverage"]["complete"]
        heading = "转单分析处理完成（关系待核实）" if complete else "转单分析部分完成，存在未核查范围"
        lines = [f"{heading}：处理{len(runner.successful_message_ids)}/{len(messages)}条文本/引用消息，识别{len(events)}条事件。"]
        for index, f in enumerate(findings, 1):
            label = {"matched": "已匹配", "suspected": "疑似异常", "unresolved": "待核实"}[f["status"]]
            if f.get("quantity_status") == "consistent":
                label = "净数量一致，关系待核实"
            elif f.get("quantity_status") == "partial_consistent":
                label = "部分证据数量一致，分析未完成"
            lines.append(f"{index}. [{label}] {f['summary']}\n证据：订单 {', '.join(f['order_refs'])}；聊天 {', '.join(f['message_refs']) or '未找到'}；知识 {', '.join(f['knowledge_refs'])}")
        if not findings:
            lines.append("没有可核查的订单差异，转单事件仍需人工核实。")
        lines.append("分析范围与限制：\n" + "\n".join(self.last_result["limitations"]))
        return "\n\n".join(lines)
