"""确认转单分析资料，复用有效文件，并调用可替换的专用分析接口。"""
from __future__ import annotations

import json
import re
from uuid import uuid4
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from app.analysis.order_compare import compare_orders, file_signature
from app.core import chat_history_workflow as history
from app.core.order_version_manager import validate_order_path
from app.core.path_manager import format_order_path, get_workspace_dir, is_within

CONFIRM_REPLIES = {"确认分析", "确认", "是", "yes", "y", "1", "对", "无误", "没问题"}
HISTORY_HINT = "如需其他时长的聊天记录，或手动强制更新，请输入“获取聊天记录”指令，例如“获取近1个月的聊天记录”。"


def parse_focus_products(text):
    match = re.search(r"(?:特别关注(?:商品)?|关注商品)\s*[:：]([\s\S]*)", text)
    if not match:
        return None
    value = match[1].strip()
    if value in {"无", "没有", "无特别关注", "清空"}:
        return []
    products = []
    for item in re.split(r"[，,；;\n]+", value):
        item = item.strip()
        if not item:
            continue
        parts = re.split(r"[:：]|(?=缺少|缺了|少了|数量异常)", item, maxsplit=1)
        name = parts[0].strip()
        description = parts[1].strip() if len(parts) > 1 else "数量异常，缺少数量未知"
        if not name:
            raise ValueError("请填写特别关注的商品名称，例如“特别关注：商品A缺少3件”。")
        quantity = re.search(r"(?:缺少|缺了|少了)\s*(\d+)(?:件|个|份|套|张|本|枚)?", description)
        products.append({"product_name": name, "description": description,
                         "missing_quantity": int(quantity[1]) if quantity else None})
    if not products:
        raise ValueError("请填写特别关注商品，或输入“特别关注：无”。")
    return products


def fresh_history(ctx):
    metadata = ctx.chat_history_metadata
    if not isinstance(metadata, dict) or metadata.get("group_name") != ctx.group_name:
        return None
    try:
        age = (history.history_now() - datetime.fromisoformat(metadata["fetched_at"])).total_seconds()
        path = Path(metadata["filtered_path"])
        if (0 <= age < 600 and is_within(path, get_workspace_dir(ctx.session_id))
                and path.is_file() and metadata.get("start") and metadata.get("end")
                and file_signature(path) == metadata["filtered_signature"]):
            return dict(metadata)
    except (KeyError, TypeError, ValueError, OSError):
        pass
    return None


def cached_report(ctx, values, signatures):
    for report in reversed(ctx.order_comparison_reports):
        if (report.get("values") == values and report.get("signatures") == signatures
                and report.get("group_name") == ctx.group_name):
            try:
                path = Path(report["report_path"])
                if path.is_file() and file_signature(path) == report["report_signature"]:
                    return report
            except (KeyError, TypeError, ValueError, OSError):
                pass
    return None


def prepare_confirmation(ctx):
    if not ctx.group_name:
        raise ValueError("请先设置群聊名称，再分析转单记录。")
    values = []
    for value in (ctx.old_order_file, ctx.new_order_file):
        valid, reason = validate_order_path(value or "")
        if not valid:
            raise ValueError(f"无法分析转单记录：{format_order_path(value)}（{reason}）。请补充两个有效订单。")
        values.append(str(Path(value).resolve()))
    if values[0] == values[1]:
        raise ValueError("新旧订单不能是同一文件。")
    signatures = [file_signature(value) for value in values]
    metadata = fresh_history(ctx)
    pending = ctx.pending_transfer_analysis
    if metadata:
        chat = {"mode": "reuse", "metadata": metadata, "start": metadata["start"], "end": metadata["end"]}
    else:
        # 等待确认期间保持展示的时间范围；模式变化则需要重新确认。
        if pending and pending.get("group_name") == ctx.group_name and pending["chat"]["mode"] == "refresh":
            start, end = pending["chat"]["start"], pending["chat"]["end"]
        else:
            start, end = history.history_time_range(history.DEFAULT_HISTORY_PERIOD)
        chat = {"mode": "refresh", "start": start, "end": end}
    report = cached_report(ctx, values, signatures)
    return {"group_name": ctx.group_name, "values": values, "signatures": signatures,
            "report_path": report["report_path"] if report else None,
            "chat": chat, "focus_products": list(ctx.transfer_focus_products)}


def confirmation_message(snapshot, prefix=""):
    chat = snapshot["chat"]
    focus = "；".join(f"{p['product_name']}：{p['description']}" for p in snapshot["focus_products"]) or "无"
    report_mode = "复用已有比对报告" if snapshot["report_path"] else "确认后生成比对报告"
    chat_mode = ("复用十分钟内获取的记录" if chat["mode"] == "reuse"
                 else "预计范围，确认后提取近一周，最终截至确认执行时刻")
    return (f"{prefix}请确认转单分析信息：\n"
            f"旧订单：{format_order_path(snapshot['values'][0])}\n"
            f"新订单：{format_order_path(snapshot['values'][1])}\n"
            f"比对方向：旧订单 → 新订单（{report_mode}）\n"
            f"聊天记录：{chat['start']} 至 {chat['end']}（北京时间，{chat_mode}）\n"
            f"特别关注：{focus}\n\n"
            "回复“确认分析”执行，或回复“取消分析”取消。\n"
            "可先修改订单、获取其他时间的聊天记录，或输入“特别关注：商品A缺少3件；商品B数量异常”。"
            "\n" + HISTORY_HINT)


def execute_analysis(tools, ctx, snapshot, progress_callback):
    if progress_callback:
        progress_callback("正在准备订单比对文件……")
    report = cached_report(ctx, snapshot["values"], snapshot["signatures"])
    if report is None:
        report = compare_orders(*snapshot["values"], ctx.group_name,
                                expected_signatures=snapshot["signatures"], structured=True)
        ctx.order_comparison_reports.append(report)
    chat = snapshot["chat"]
    if chat["mode"] == "refresh":
        original_period = ctx.chat_history_period
        try:
            metadata = history.handle_chat_history(
                tools, ctx, {"chat_history_period": dict(history.DEFAULT_HISTORY_PERIOD)}, progress_callback,
                structured=True)
        finally:
            ctx.chat_history_period = original_period
        if not isinstance(metadata, dict):
            return metadata
    else:
        metadata = fresh_history(ctx)
        if metadata != chat["metadata"]:
            raise ValueError("聊天记录已变化或过期，请重新输入分析转单记录并确认。")
    if [file_signature(value) for value in snapshot["values"]] != snapshot["signatures"]:
        raise ValueError("准备资料期间订单内容发生变化，请重新输入分析转单记录并确认。")
    report_path = Path(report["report_path"])
    filtered_path = Path(metadata["filtered_path"])
    payload = {
        "task": "核查聊天中的转单与订单变化是否一致，特别关注指定商品的数量异常和缺少数量。"
                "返回摘要、匹配记录、疑似异常、订单与聊天证据及无法确定的事项。"
                "特别关注信息是用户提供的线索，不能直接视为已证实结论。",
        "group_name": ctx.group_name, "old_order": snapshot["values"][0], "new_order": snapshot["values"][1],
        "direction": "旧订单 → 新订单", "comparison_path": str(report_path),
        "comparison_csv": report_path.read_text(encoding="utf-8-sig"),
        "chat_history_path": str(filtered_path), "chat_history_csv": filtered_path.read_text(encoding="utf-8-sig"),
        "chat_start": metadata["start"], "chat_end": metadata["end"],
        "focus_products": snapshot["focus_products"],
    }
    if tools.transfer_analysis_client is not None:
        from app.llm.transfer_analyzer import prepare_analysis_payload
        payload = prepare_analysis_payload(payload, metadata)
        if [file_signature(value) for value in snapshot["values"]] != snapshot["signatures"]:
            raise ValueError("准备模型资料期间订单内容发生变化，请重新确认。")
    path = get_workspace_dir(ctx.session_id) / "transfer_analysis_input.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    files = f"\n订单比对文件：{report_path}\n筛选聊天记录：{filtered_path}\n分析输入：{path}"
    if tools.transfer_analysis_client is None:
        return "分析资料已准备，LLM 分析接口尚未配置。" + files + "\n" + HISTORY_HINT
    if progress_callback:
        progress_callback("正在分析转单记录……")
    try:
        from app.llm.transfer_analyzer import TransferAnalyzer
        if isinstance(tools.transfer_analysis_client, TransferAnalyzer):
            diagnostics_dir = path.parent / "transfer_diagnostics" / uuid4().hex
            files += f"\n分析诊断目录：{diagnostics_dir}"
            result = tools.transfer_analysis_client.analyze(payload, progress_callback=progress_callback,
                                                             diagnostics_dir=diagnostics_dir)
        else:
            result = tools.transfer_analysis_client.analyze(payload)
        if not isinstance(result, str) or not result.strip():
            raise ValueError("接口未返回有效的分析结果文本")
    except Exception as error:
        return f"分析资料已准备，LLM 分析失败：{error}。" + files
    structured_result = getattr(tools.transfer_analysis_client, "last_result", None)
    if isinstance(structured_result, dict):
        result_path = path.with_name("transfer_analysis_result.json")
        result_temporary = result_path.with_suffix(".tmp")
        result_temporary.write_text(json.dumps(structured_result, ensure_ascii=False, indent=2), encoding="utf-8")
        result_temporary.replace(result_path)
        files += f"\n结构化分析结果：{result_path}"
    return result + files


def handle_transfer_analysis(tools, ctx, intent, text, progress_callback=None):
    action = intent["intent"]
    pending = ctx.pending_transfer_analysis
    confirming = action == "confirm_transfer_analysis" or (pending is not None and text.strip().lower() in CONFIRM_REPLIES)
    if action == "cancel_transfer_analysis":
        ctx.pending_transfer_analysis = None
        return "已取消转单分析。"
    if action not in {"analyze_transfers", "update_transfer_focus"} and not confirming:
        # 其他业务指令结束此确认状态，避免其“确认”回复执行转单分析。
        if action not in {"chat", "set_context", "extract_chat_history"}:
            ctx.pending_transfer_analysis = None
        return None
    if confirming and pending is None:
        return "当前没有等待确认的转单分析，请先输入“分析转单记录”。"
    try:
        focus = parse_focus_products(text)
        if focus is not None:
            ctx.transfer_focus_products = focus
        if action == "update_transfer_focus" and pending is None:
            return "特别关注商品已保存。输入“分析转单记录”后确认执行。"
        if action == "analyze_transfers":
            ctx.pending_transfer_analysis = None
        snapshot = prepare_confirmation(ctx)
        if confirming and snapshot == pending:
            ctx.pending_transfer_analysis = None
            return execute_analysis(tools, ctx, snapshot, progress_callback)
        ctx.pending_transfer_analysis = snapshot
        ctx.pending_order_comparison = None
        ctx.pending_participation = None
        ctx.share_request.pending_config_confirmation = False
        ctx.bulk_request.pending_confirmation = False
        prefix = "订单、聊天记录或特别关注信息已变化，请重新确认。\n" if confirming else ""
        return confirmation_message(snapshot, prefix)
    except (OSError, ValueError, RuntimeError, BadZipFile, InvalidFileException) as error:
        ctx.pending_transfer_analysis = None
        return f"转单分析未完成：{error}"
