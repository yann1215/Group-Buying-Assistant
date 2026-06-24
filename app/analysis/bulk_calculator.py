# app/analysis/bulk_calculator.py

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from app.analysis.share_config import default_include_share


NON_PRODUCT_FIELDS = {
    "单号",
    "昵称",
    "总金额",
    "大货应收金额",
}


class BulkGoodsError(RuntimeError):
    """大货订单处理失败。"""


def find_only_non_share_orders(
    parsed_order_file: str | Path,
) -> list[dict[str, Any]]:
    """
    查找只购买了不参摊商品的订单。

    返回：
    [
        {
            "单号": "12",
            "昵称": "xxx",
            "商品": ["底胚A", "底胚B"],
        }
    ]
    """
    parsed_order_file = Path(parsed_order_file)

    if not parsed_order_file.exists():
        raise FileNotFoundError(
            f"简化订单文件不存在：{parsed_order_file}"
        )

    abnormal_orders: list[dict[str, Any]] = []

    with parsed_order_file.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise BulkGoodsError("简化订单文件没有表头。")

        product_fields = [
            field
            for field in reader.fieldnames
            if field not in NON_PRODUCT_FIELDS
        ]

        for row_idx, row in enumerate(reader, start=2):
            ordered_products: list[str] = []

            for product_name in product_fields:
                quantity = parse_optional_quantity(
                    row.get(product_name),
                    row_idx=row_idx,
                    product_name=product_name,
                )

                if quantity > 0:
                    ordered_products.append(product_name)

            # 没有购买任何商品，由其他订单合法性检查负责处理
            if not ordered_products:
                continue

            has_share_product = any(
                default_include_share(product_name)
                for product_name in ordered_products
            )

            if not has_share_product:
                abnormal_orders.append(
                    {
                        "单号": str(row.get("单号") or "").strip(),
                        "昵称": str(row.get("昵称") or "").strip(),
                        "商品": ordered_products,
                    }
                )

    return abnormal_orders


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


def parse_optional_quantity(
    value: Any,
    row_idx: int,
    product_name: str,
) -> int:
    if value is None:
        return 0

    text = str(value).strip()

    if text == "":
        return 0

    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise BulkGoodsError(
            f"简化订单第 {row_idx} 行商品"
            f"“{product_name}”数量不是整数：{value!r}"
        ) from exc

    if number < 0:
        raise BulkGoodsError(
            f"简化订单第 {row_idx} 行商品"
            f"“{product_name}”数量不能小于 0。"
        )

    return number