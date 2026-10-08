"""先分别确认配置，再统一核对成员、计算及导出总金额。"""
import hashlib
import json

from app.analysis.bulk_calculator import create_bulk_receivable_orders
from app.analysis.combined_calculator import COMBINED_FIELDS, create_combined_receivable_orders
from app.analysis.product_config import load_product_share_config_file


def combined_signature(ctx):
    from app.core.tool_orchestrator import share_signature
    signature = share_signature(ctx)
    if not signature:
        return None
    configs = load_product_share_config_file(ctx.share_config_file)
    data = [signature, [{key: row.get(key) for key in (
        "商品序号", "商品名称", "商品数量", "商品单价", "商品大货总价",
    )} for row in configs]]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def start_combined(tools, ctx, intent, progress_callback=None):
    from app.core.tool_orchestrator import invalidate_share_confirmation
    invalidate_share_confirmation(ctx)
    ctx.bulk_request.pending_confirmation = False
    ctx.bulk_request.confirmed = False
    tools.update_share_request_from_intent(ctx, intent)
    ctx.share_request.force = False
    if intent.get("product_share_amounts") or any(
        intent.get(key) is not None for key in ("share_mode", "calculation_scope", "amount")
    ):
        reply = tools.handle_update_share_config(ctx, intent)
        if not intent.get("config_saved"):
            return reply
    reply = tools.handle_calculate_share(ctx, intent, progress_callback)
    if ctx.share_request.pending_config_confirmation:
        ctx.combined_stage = "share"
        reply += "\n均摊确认后将继续确认大货配置，再查成员并生成均摊与大货总金额表。"
    return reply


def confirm_combined_bulk(tools, ctx, progress_callback=None):
    from app.core.tool_orchestrator import (
        emit_progress, format_member_check_result, get_blocking_member_issues,
        invalidate_share_confirmation, share_signature,
    )

    def restart():
        return "订单或配置已变化，请重新确认均摊和大货配置。\n\n" + start_combined(tools, ctx, {}, progress_callback)

    if (not ctx.share_request.config_confirmed
            or ctx.share_request.confirmation_signature != share_signature(ctx)
            or not ctx.combined_confirmation_signature
            or ctx.combined_confirmation_signature != combined_signature(ctx)):
        return restart()
    check_result = tools.ensure_member_checked(ctx, progress_callback=progress_callback)
    if (ctx.share_request.confirmation_signature != share_signature(ctx)
            or ctx.combined_confirmation_signature != combined_signature(ctx)):
        return restart()
    if not check_result.get("ok") or check_result.get("blocking_issues") or get_blocking_member_issues(check_result):
        ctx.bulk_request.pending_confirmation = False
        invalidate_share_confirmation(ctx)
        return "成员核对未通过，暂不计算均摊和大货。\n\n" + format_member_check_result(check_result)
    share_reply = tools.execute_confirmed_share(ctx, progress_callback, check_result=check_result)
    ctx.bulk_request.pending_confirmation = False
    if ctx.share_results_invalidated or not ctx.last_share_result or not ctx.last_share_signature:
        return share_reply
    emit_progress(progress_callback, "正在计算大货……")
    bulk_result = create_bulk_receivable_orders(ctx.parsed_order_file, ctx.group_name)
    if not bulk_result.get("ok"):
        return str(bulk_result.get("message") or "大货计算失败。")
    emit_progress(progress_callback, "正在生成均摊与大货总金额表……")
    result = create_combined_receivable_orders(ctx.last_share_result, bulk_result, ctx.group_name)
    ctx.bulk_request.confirmed = True
    lines = ["均摊和大货总金额表已生成。", f"成员数量：{result['order_count']}",
             f"结果文件：{result['result_file']}", "", "| " + " | ".join(COMBINED_FIELDS) + " |",
             "| " + " | ".join(["---"] * len(COMBINED_FIELDS)) + " |"]
    for row in result["items"]:
        values = [str(row[key]).replace("|", "\\|").replace("\n", " ").replace("\r", " ") for key in COMBINED_FIELDS]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)
