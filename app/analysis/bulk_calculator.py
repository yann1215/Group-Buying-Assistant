# app/analysis/bulk_calculator.py

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


NON_PRODUCT_FIELDS = {
    "单号",
    "昵称",
    "总金额",
    "大货应收金额",
}


class BulkGoodsError(RuntimeError):
    """大货订单处理失败。"""


def create_bulk_receivable_orders(
    parsed_order_file: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """
    根据大货订单的 parsed orders 生成大货应收文件。

    原来的“总金额”不进行重新计算，只将其解释为并重命名为：
    “大货应收金额”。
    """
    parsed_order_file = Path(parsed_order_file)

    if not parsed_order_file.exists():
        raise FileNotFoundError(
            f"简化订单文件不存在：{parsed_order_file}"
        )

    output_dir_path = (
        Path(output_dir)
        if output_dir
        else parsed_order_file.parent
    )
    output_dir_path.mkdir(parents=True, exist_ok=True)

    base_name = parsed_order_file.stem
    if base_name.endswith("_parsed_orders"):
        base_name = base_name.removesuffix("_parsed_orders")

    output_path = (
        output_dir_path
        / f"{base_name}_parsed_bulk_orders.csv"
    )

    rows: list[dict[str, Any]] = []

    with parsed_order_file.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as src:
        reader = csv.DictReader(src)

        if not reader.fieldnames:
            raise BulkGoodsError("简化订单文件没有表头。")

        if "总金额" not in reader.fieldnames:
            raise BulkGoodsError(
                "简化订单文件缺少“总金额”列，"
                "请先确认 order_parser.py 已导出订单表中的总金额。"
            )

        product_fields = [
            field
            for field in reader.fieldnames
            if field not in {
                "单号",
                "昵称",
                "总金额",
                "大货应收金额",
            }
        ]

        output_fields = [
            "单号",
            "昵称",
            "大货应收金额",
            *product_fields,
        ]

        for row in reader:
            output_row = {
                "单号": row.get("单号", ""),
                "昵称": row.get("昵称", ""),
                # 原值直接复制，不在这里重新计算或取整
                "大货应收金额": row.get("总金额", ""),
            }

            for product_name in product_fields:
                output_row[product_name] = row.get(
                    product_name,
                    "",
                )

            rows.append(output_row)

    with output_path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as dst:
        writer = csv.DictWriter(
            dst,
            fieldnames=output_fields,
        )
        writer.writeheader()
        writer.writerows(rows)

    return {
        "ok": True,
        "result_file": str(output_path.resolve()),
        "order_count": len(rows),
        "items": rows,
    }
