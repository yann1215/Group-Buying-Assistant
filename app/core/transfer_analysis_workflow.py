"""确认并复用转单资料，执行双向数量核查及排序报告。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from app.analysis.order_compare import compare_orders, file_signature
from app.analysis.order_parser import parse_order_file
from app.analysis.transfer_audit import audit_transfers
from app.analysis.transfer_focus import parse_focus_products
from app.core import chat_history_workflow as history
from app.core.order_identity_cache import load_source_mapping
from app.core.order_version_manager import validate_order_path
from app.core.path_manager import format_order_path, get_workspace_dir, is_within
from app.utils.csv_utils import read_csv_dict_rows

CONFIRM_REPLIES = {"确认分析", "确认", "是", "yes", "y", "1", "对", "无误", "没问题"}
HISTORY_HINT = "如需其他时长的聊天记录，或手动强制更新，请输入“获取聊天记录”指令，例如“获取近1个月的聊天记录”。"


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
    path = get_workspace_dir(ctx.session_id) / "transfer_analysis_input.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    if progress_callback:
        progress_callback("正在匹配接收记录、核查双方数量并按风险排序……")
    warnings = []
    identities = load_source_mapping(ctx.session_id, ctx.group_name, warnings=warnings)
    for member in getattr(ctx, "special_members", ()):
        if member.get("wxid"):
            identities.append({"wxid": member["wxid"], "serial": member.get("单号", ""),
                               "wechat_name": member.get("昵称", ""), "name": member.get("订单昵称", "")})
    with TemporaryDirectory(dir=get_workspace_dir(ctx.session_id)) as directory:
        tables = [read_csv_dict_rows(parse_order_file(value, Path(directory) / f"{index}.csv"))[0]
                  for index, value in enumerate(snapshot["values"])]
    # 原始记录可保留没有命中筛选关键词的撤销消息；必须属于当前会话且签名一致。
    audit_chat_path = filtered_path
    raw_path = Path(metadata["raw_path"]) if metadata.get("raw_path") else None
    if raw_path and is_within(raw_path, get_workspace_dir(ctx.session_id)) and raw_path.is_file():
        if file_signature(raw_path) != metadata.get("raw_signature"):
            raise ValueError("原始聊天记录发生变化，请重新获取聊天记录后分析。")
        audit_chat_path = raw_path
    audit_chat_signature = file_signature(audit_chat_path)
    rows, fields = read_csv_dict_rows(audit_chat_path)
    if not {"类型", "内容"}.issubset(fields):
        raise ValueError("聊天记录缺少类型或内容列")
    result = audit_transfers(*tables, rows, identities=identities, focus_products=snapshot["focus_products"],
                             history_complete=metadata.get("covers_order_interval") is True)
    if ([file_signature(value) for value in snapshot["values"]] != snapshot["signatures"]
            or file_signature(filtered_path) != metadata["filtered_signature"]):
        raise ValueError("核查期间订单或聊天记录发生变化，请重新分析。")
    if file_signature(audit_chat_path) != audit_chat_signature:
        raise ValueError("核查期间原始聊天记录发生变化，请重新分析。")
    result["chat_history_path"] = str(audit_chat_path)
    result["text"] += f"\n聊天范围：{metadata['start']} 至 {metadata['end']}（北京时间）。"
    if warnings:
        result["text"] += "\n" + "\n".join(warnings)
    result_path = path.with_name("transfer_analysis_result.json")
    report_output = path.with_name("transfer_analysis_report.txt")
    for destination, content in ((result_path, json.dumps(result, ensure_ascii=False, indent=2)),
                                 (report_output, result["text"])):
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(destination)
    files = f"\n异常报告：{report_output}\n结构化结果：{result_path}\n订单比对文件：{report_path}"
    return result["text"] + "\n" + files + "\n" + HISTORY_HINT


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
