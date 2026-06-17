# app/analysis/share_config.py

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from app.config import CSV_OUTPUT_DIR, ensure_dirs


MAX_PRODUCT_SLOTS = 20

CONFIG_FIELDNAMES = [
    "商品序号",
    "商品名称",
    "商品数量",
    "计入均摊",
    "均摊类型",
    "商品均摊",
    "单份均摊",
    "商品单价",
    "商品大货总价",
]


class ShareConfigError(RuntimeError):
    """商品均摊配置表处理失败。"""


def create_product_share_config_file(
    parsed_order_file: str | Path,
    output_dir: str | Path | None = None,
    max_product_slots: int = MAX_PRODUCT_SLOTS,
    overwrite: bool = False,
) -> str:
    """
    根据简化订单表生成商品均摊配置表。

    输入：
        order_parser.py 输出的宽表 CSV：
            单号, 昵称, 商品A, 商品B, 商品C...

    输出：
        商品均摊配置表 CSV。

    默认规则：
        - 商品名称包含“底胚” → 计入均摊=False
        - 其他商品 → 计入均摊=True

    返回：
        配置表文件绝对路径。
    """
    parsed_order_file = Path(parsed_order_file)

    if not parsed_order_file.exists():
        raise FileNotFoundError(f"简化订单文件不存在：{parsed_order_file}")

    ensure_dirs()

    output_dir_path = Path(output_dir) if output_dir else CSV_OUTPUT_DIR
    output_dir_path.mkdir(parents=True, exist_ok=True)

    product_rows = read_product_summary_from_order_file(parsed_order_file)

    if len(product_rows) > max_product_slots:
        raise ShareConfigError(
            f"商品数量为 {len(product_rows)}，超过预留上限 {max_product_slots}。"
        )

    rows: list[dict[str, Any]] = []

    for idx, product in enumerate(product_rows, start=1):
        product_name = product["商品名称"]
        product_quantity = product["商品数量"]
        include_share = default_include_share(product_name)

        rows.append(
            {
                "商品序号": idx,
                "商品名称": product_name,
                "商品数量": product_quantity,
                "计入均摊": include_share,
                "均摊类型": "",
                "商品均摊": "" if include_share else "0.00",
                "单份均摊": "" if include_share else "0.00",
                "商品单价": "",
                "商品大货总价": "",
            }
        )

    # 预留空槽位到 max_product_slots
    for idx in range(len(rows) + 1, max_product_slots + 1):
        rows.append(
            {
                "商品序号": idx,
                "商品名称": "",
                "商品数量": "",
                "计入均摊": False,
                "均摊类型": "",
                "商品均摊": "",
                "单份均摊": "",
                "商品单价": "",
                "商品大货总价": "",
            }
        )

    if overwrite:
        output_path = output_dir_path / f"{parsed_order_file.stem}_share_config.csv"
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir_path / f"{parsed_order_file.stem}_share_config_{timestamp}.csv"

    with output_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CONFIG_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    return str(output_path.resolve())


def read_product_summary_from_order_file(
    parsed_order_file: str | Path,
) -> list[dict[str, Any]]:
    """
    从简化订单宽表中读取商品名称，并统计每个商品总数量。
    """
    parsed_order_file = Path(parsed_order_file)

    with parsed_order_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ShareConfigError("简化订单文件没有表头。")

        if "单号" not in reader.fieldnames or "昵称" not in reader.fieldnames:
            raise ShareConfigError("简化订单文件必须包含“单号”和“昵称”列。")

        product_names = [
            name for name in reader.fieldnames
            if name not in {"单号", "昵称"}
        ]

        product_quantity_map = {
            product_name: 0
            for product_name in product_names
        }

        for row_idx, row in enumerate(reader, start=2):
            for product_name in product_names:
                value = str(row.get(product_name, "") or "").strip()

                if value == "":
                    continue

                if not value.isdigit():
                    raise ShareConfigError(
                        f"第 {row_idx} 行商品“{product_name}”数量不是正整数：{value!r}"
                    )

                quantity = int(value)

                if quantity <= 0:
                    raise ShareConfigError(
                        f"第 {row_idx} 行商品“{product_name}”数量不是正整数：{value!r}"
                    )

                product_quantity_map[product_name] += quantity

    return [
        {
            "商品名称": product_name,
            "商品数量": quantity,
        }
        for product_name, quantity in product_quantity_map.items()
    ]


def load_product_share_config_file(
    config_file: str | Path,
) -> list[dict[str, Any]]:
    """
    读取商品均摊配置表，转换成 share_calculator.py 可用的数据结构。

    金额字段规则：
        - 空值保持为空字符串
        - 非空时转成两位小数字符串
        - 多余小数位向上取整

    返回示例：
        [
            {
                "商品序号": 1,
                "商品名称": "商品A",
                "商品数量": 3,
                "计入均摊": True,
                "均摊类型": "",
                "商品均摊": "12.35",
                "单份均摊": "",
                "商品单价": "",
                "商品大货总价": "",
            }
        ]
    """
    config_file = Path(config_file)

    if not config_file.exists():
        raise FileNotFoundError(f"商品均摊配置表不存在：{config_file}")

    configs: list[dict[str, Any]] = []

    with config_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ShareConfigError("商品均摊配置表没有表头。")

        missing_headers = [
            name for name in CONFIG_FIELDNAMES
            if name not in reader.fieldnames
        ]

        if missing_headers:
            raise ShareConfigError(
                f"商品均摊配置表缺少表头：{missing_headers}"
            )

        for row_idx, row in enumerate(reader, start=2):
            product_name = str(row.get("商品名称", "") or "").strip()

            # 预留空行跳过
            if not product_name:
                continue

            product_no = parse_required_positive_int(
                row.get("商品序号"),
                field_name="商品序号",
                row_idx=row_idx,
            )

            product_quantity = parse_required_positive_int(
                row.get("商品数量"),
                field_name="商品数量",
                row_idx=row_idx,
            )

            include_share = parse_bool_required(
                row.get("计入均摊"),
                field_name="计入均摊",
                row_idx=row_idx,
            )

            product_share_amount = parse_optional_money_ceil(
                row.get("商品均摊"),
                field_name="商品均摊",
                row_idx=row_idx,
            )

            unit_share_amount = parse_optional_money_ceil(
                row.get("单份均摊"),
                field_name="单份均摊",
                row_idx=row_idx,
            )

            product_unit_price = parse_optional_money_ceil(
                row.get("商品单价"),
                field_name="商品单价",
                row_idx=row_idx,
            )

            product_total_price = parse_optional_money_ceil(
                row.get("商品大货总价"),
                field_name="商品大货总价",
                row_idx=row_idx,
            )

            # 如果不计入均摊，商品均摊和单份均摊允许为空，也允许为 0.00。
            if not include_share:
                product_share_amount = normalize_zero_or_blank_money(product_share_amount)
                unit_share_amount = normalize_zero_or_blank_money(unit_share_amount)

            config = {
                "商品序号": product_no,
                "商品名称": product_name,
                "商品数量": product_quantity,
                "计入均摊": include_share,
                "均摊类型": str(row.get("均摊类型", "") or "").strip(),
                "商品均摊": product_share_amount,
                "单份均摊": unit_share_amount,
                "商品单价": product_unit_price,
                "商品大货总价": product_total_price,
            }

            configs.append(config)

    return configs


def default_include_share(product_name: str) -> bool:
    """
    默认是否计入均摊。

    当前规则：
        商品名称包含“底胚” → False
        其他 → True
    """
    return "底胚" not in str(product_name or "")


def parse_required_positive_int(
    value: Any,
    field_name: str,
    row_idx: int,
) -> int:
    if value is None:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”不能为空，必须是正整数。"
        )

    text = str(value).strip()

    if not text.isdigit():
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”必须是正整数，实际值：{value!r}"
        )

    number = int(text)

    if number <= 0:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”必须是正整数，实际值：{value!r}"
        )

    return number


def parse_bool_required(
    value: Any,
    field_name: str,
    row_idx: int,
) -> bool:
    """
    CSV 没有真正的 bool 类型，所以这里规定：
        True / False
        true / false
        TRUE / FALSE

    为了方便手动编辑，也兼容：
        是 / 否
        1 / 0
    """
    if value is None:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”不能为空，必须是 bool。"
        )

    text = str(value).strip()

    if text in {"True", "true", "TRUE", "1", "是"}:
        return True

    if text in {"False", "false", "FALSE", "0", "否"}:
        return False

    raise ShareConfigError(
        f"第 {row_idx} 行“{field_name}”必须是 bool，建议填写 True 或 False。"
        f"实际值：{value!r}"
    )


def parse_optional_money_ceil(
    value: Any,
    field_name: str,
    row_idx: int,
) -> str:
    """
    解析可选金额字段。

    规则：
        - 空值返回 ""
        - 非空必须是 >= 0 的数字
        - 保留两位小数
        - 多余小数位向上取整

    示例：
        12       -> 12.00
        12.3     -> 12.30
        12.341   -> 12.35
        0        -> 0.00
    """
    if value is None:
        return ""

    text = str(value).strip()

    if text == "":
        return ""

    try:
        amount = Decimal(text)
    except Exception as e:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”必须是数字或空，实际值：{value!r}"
        ) from e

    if amount < 0:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”不能为负数，实际值：{value!r}"
        )

    amount = amount.quantize(
        Decimal("0.01"),
        rounding=ROUND_CEILING,
    )

    return f"{amount:.2f}"


def normalize_zero_or_blank_money(value: str) -> str:
    """
    不计入均摊的商品，商品均摊和单份均摊可以为空，也可以是 0.00。

    如果用户填了非 0 金额，这里不强制报错，先保留。
    如果你希望严格禁止，可以改成非 0 时报错。
    """
    if value == "":
        return ""

    return value