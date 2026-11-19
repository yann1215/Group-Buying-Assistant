"""将同一批订单的均摊应收和大货应收按单号合并。"""
from app.utils.receivable_xlsx import write_receivable_xlsx
from decimal import Decimal
from typing import Any

from app.analysis.share_calculator import normalize_order_no_for_compare
from app.core.path_manager import get_combined_output_path


COMBINED_FIELDS = ["单号", "昵称", "总金额", "均摊金额", "大货金额"]


def create_combined_receivable_orders(
    share_result: dict[str, Any], bulk_result: dict[str, Any], group_name: str,
) -> dict[str, Any]:
    share_amounts = {
        normalize_order_no_for_compare(row["单号"]): Decimal(str(row["均摊金额"]))
        for row in share_result.get("all_items", share_result["items"])
    }
    all_items = []
    rows = []
    for row in bulk_result.get("all_items", bulk_result["items"]):
        share = share_amounts.get(normalize_order_no_for_compare(row["单号"]), Decimal(0))
        bulk = Decimal(str(row["大货金额"]))
        total = share + bulk
        item = {
            "单号": row["单号"], "昵称": row["昵称"],
            "总金额": f"{total:.2f}", "均摊金额": f"{share:.2f}", "大货金额": f"{bulk:.2f}",
        }
        all_items.append(item)
        if total > 0:
            rows.append(item)
    output = get_combined_output_path(group_name)
    write_receivable_xlsx(output, COMBINED_FIELDS, rows, "总金额")
    return {"ok": True, "items": rows, "all_items": all_items, "order_count": len(rows), "result_file": str(output.resolve())}
