"""订单名称查询及与其他业务隔离的比较确认流程。"""
import re
from pathlib import Path
from zipfile import BadZipFile
from openpyxl.utils.exceptions import InvalidFileException
from app.analysis.order_compare import compare_orders, file_signature
from app.core.order_version_manager import validate_order_path
from app.core.path_manager import format_order_path

LABELS = {"new_order_file": "新订单", "old_order_file": "旧订单",
          "order_cache_1_file": "缓存1", "order_cache_2_file": "缓存2"}


def handle_order_comparison(ctx, intent, text):
    action = intent["intent"]
    if action == "show_orders":
        return "\n".join(f"{label}：{format_order_path(getattr(ctx, field))}" for field, label in LABELS.items())
    pending = ctx.pending_order_comparison
    if action == "cancel_orders" or (pending and text.strip() in {"取消", "不比较", "不要比较"}):
        ctx.pending_order_comparison = None
        return "已取消订单比较。"
    confirming = action == "confirm_orders" or (pending and text.strip() in {"确认", "确认无误", "没问题", "可以", "好的"})
    if action != "compare_orders" and not confirming:
        if action in {"calculate_share", "calculate_bulk_goods", "update_participation", "member_check"}:
            ctx.pending_order_comparison = None
        return None
    if confirming and not pending:
        return "当前没有等待确认的订单比较，请先输入“比较订单”。"
    if action == "compare_orders":
        ctx.pending_order_comparison = None
        selected = re.findall(r"新订单|旧订单|缓存[12]", text)
        if selected and (len(selected) != 2 or selected[0] == selected[1]):
            return "请指定两个不同的订单版本，例如“比较新订单和缓存1”。"
        inverse = {label: field for field, label in LABELS.items()}
        fields = [inverse[label] for label in selected] if selected else ["old_order_file", "new_order_file"]
        # 槽位顺序为从新到旧，比较方向始终从旧到新。
        fields.sort(key=lambda field: list(LABELS).index(field), reverse=True)
    else:
        fields = pending["fields"]
    values = [getattr(ctx, field) for field in fields]
    for value in values:
        valid, reason = validate_order_path(value or "")
        if not valid:
            ctx.pending_order_comparison = None
            return f"无法比较订单：{format_order_path(value)}（{reason}）。请补充两个有效订单。"
    if Path(values[0]).resolve() == Path(values[1]).resolve():
        ctx.pending_order_comparison = None
        return "新旧订单不能是同一文件。"
    if not ctx.group_name:
        return "请先设置群聊名称，以便保存订单比较报告。"
    try:
        signatures = [file_signature(value) for value in values]
        snapshot = {"fields": fields, "values": values, "signatures": signatures, "group_name": ctx.group_name}
        if confirming and snapshot == pending:
            ctx.pending_order_comparison = None
            return compare_orders(*values, ctx.group_name, expected_signatures=signatures)
        ctx.pending_order_comparison = snapshot
        ctx.share_request.pending_config_confirmation = False
        ctx.bulk_request.pending_confirmation = False
        ctx.pending_participation = None
        prefix = "订单或文件内容已变化，请重新确认。\n" if confirming else ""
        return (prefix + "请确认要比较的订单（旧 → 新）：\n" +
                "\n".join(f"{LABELS[field]}：{format_order_path(value)}" for field, value in zip(fields, values)) +
                "\n回复“确认比较”开始，也可重新指定订单或回复“取消比较”。")
    except (OSError, ValueError, RuntimeError, BadZipFile, InvalidFileException) as error:
        ctx.pending_order_comparison = None
        return f"订单比较失败：{error}"
