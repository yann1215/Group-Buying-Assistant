"""将同一批订单的均摊应收和大货应收按单号合并。"""
import csv
from decimal import Decimal
from typing import Any

from app.analysis.share_calculator import normalize_order_no_for_compare
from app.core.path_manager import get_combined_output_path


COMBINED_FIELDS = ["单号", "昵称", "总金额", "均摊金额", "大货金额"]


def create_combined_receivable_orders(
    share_result: dict[str, Any], bulk_result: dict[str, Any], group_name: str,
) -> dict[str, Any]:
    share_amounts = {
        normalize_order_no_for_compare(row["单号"]): Decimal(str(row["应收金额"]))
        for row in share_result["items"]
    }
    rows = []
    for row in bulk_result["items"]:
        share = share_amounts.get(normalize_order_no_for_compare(row["单号"]), Decimal(0))
        bulk = Decimal(str(row["大货应收金额"]))
        total = share + bulk
        if total == 0:
            continue
        rows.append({
            "单号": row["单号"], "昵称": row["昵称"],
            "总金额": f"{total:.2f}", "均摊金额": f"{share:.2f}", "大货金额": f"{bulk:.2f}",
        })
    output = get_combined_output_path(group_name)
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COMBINED_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {"ok": True, "items": rows, "order_count": len(rows), "result_file": str(output.resolve())}
