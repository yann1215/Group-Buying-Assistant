"""金额过滤后的非阻断提醒。"""
from decimal import Decimal
from app.analysis.share_calculator import normalize_order_no_for_compare
from app.utils.name_matching import normalize_name


def format_receivable_notice(result, special_members, amount_field, label):
    members = special_members or []
    serials = {normalize_order_no_for_compare(m.get("单号")) for m in members if m.get("单号")}
    names = {normalize_name(m.get(key)) for m in members if not m.get("单号")
             for key in ("昵称", "群昵称") if m.get(key)}
    excluded = [row for row in result.get("all_items", result.get("items", []))
                if Decimal(str(row[amount_field])) <= 0
                and normalize_order_no_for_compare(row.get("单号")) not in serials
                and normalize_name(row.get("昵称")) not in names]
    lines = ["仅输出应收金额大于 0 的订单。"]
    if not result.get("items"):
        lines.append("没有金额大于 0 的订单，结果文件仅含表头。")
    if excluded:
        lines.append(f"提醒：以下订单的{label}小于或等于 0，且不属于特殊成员，已从结果表中排除，请核对：")
        for row in excluded:
            detail = (f"均摊：{row['均摊金额']} 元｜大货：{row['大货金额']} 元｜"
                      if amount_field == "总金额" else "")
            lines.append(f"- 单号 {row['单号']}｜昵称：{row['昵称']}｜{detail}{label}：{row[amount_field]} 元")
        lines.append("其他符合条件的订单已正常输出。")
    return "\n".join(lines)
