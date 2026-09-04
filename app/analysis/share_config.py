# app/analysis/share_config.py

from __future__ import annotations

import csv
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from app.config import CSV_OUTPUT_DIR, ensure_dirs
from app.analysis.order_validator import default_include_share
from app.analysis.order_parser import (
    read_product_unit_prices,
)

ORDER_METADATA_COLUMNS = {"单号", "昵称", "总金额"}

CONFIG_FIELDNAMES = [
    "商品序号",
    "商品名称",
    "商品数量",
    "计入均摊",
    "均摊类型",
    "商品均摊",
    "商品单价",
]


class ShareConfigError(RuntimeError):
    """商品均摊配置表处理失败。"""


def sync_product_config_file(
        parsed_order_file: str | Path,
        original_order_file: str | Path,
        output_dir: str | Path | None = None,
        share_mode: str | None = None,
        calculated_configs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    根据简化订单表生成商品均摊配置表。

    输入：
        order_parser.py 输出的宽表 CSV：
            单号, 昵称, 总金额, 商品A, 商品B, 商品C...

    输出：
        商品均摊配置表 CSV。

    默认规则：
        - 商品名称包含“底胚” → 计入均摊=False
        - 其他商品 → 计入均摊=True

    返回：
        配置表文件绝对路径。
    """

    parsed_order_file = Path(parsed_order_file)

    output_dir_path = (
        Path(output_dir)
        if output_dir
        else CSV_OUTPUT_DIR
    )
    output_dir_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    base_name = parsed_order_file.stem

    if base_name.endswith("_parsed_orders"):
        base_name = base_name.removesuffix(
            "_parsed_orders"
        )

    output_path = (
            output_dir_path
            / f"{base_name}_parsed_product_config.csv"
    )

    product_rows = read_product_summary_from_order_file(
        parsed_order_file
    )

    # -------------------------
    # 读取旧配置
    # -------------------------

    old_map: dict[str, dict[str, Any]] = {}

    if output_path.exists():
        try:
            old_configs = load_product_share_config_file(
                output_path
            )

            old_map = {
                str(item.get("商品名称") or "").strip(): item
                for item in old_configs
                if str(item.get("商品名称") or "").strip()
            }

        except Exception:
            # 配置文件格式已经是旧版时，
            # 不需要阻止重新生成新版配置。
            old_map = {}

    # -------------------------
    # 本次计算结果
    # -------------------------

    calculated_map = {
        str(item.get("商品名称") or "").strip(): item
        for item in (calculated_configs or [])
        if str(item.get("商品名称") or "").strip()
    }

    # -------------------------
    # 从 sheet1 获取价格
    # -------------------------

    product_names = [
        item["商品名称"]
        for item in product_rows
    ]

    price_result = read_product_unit_prices(
        order_input=original_order_file,
        product_names=product_names,
    )

    prices = price_result["prices"]
    warnings = price_result["warnings"]

    # -------------------------
    # 生成新配置
    # -------------------------

    rows = []

    for idx, product in enumerate(
            product_rows,
            start=1,
    ):
        product_name = product["商品名称"]

        old = old_map.get(product_name, {})
        calculated = calculated_map.get(
            product_name,
            {},
        )

        include_share = old.get("计入均摊")

        if include_share is None:
            include_share = default_include_share(
                product_name
            )

        # 商品均摊：
        # 计算结果有明确值 → 更新
        # 否则 → 保留原值
        product_share = calculated.get(
            "商品均摊"
        )

        if product_share in (None, ""):
            product_share = old.get(
                "商品均摊",
                "",
            )

        if (
                not include_share
                and product_share == ""
        ):
            product_share = "0.00"

        # 均摊类型
        if share_mode == "head":
            share_type = "人头摊"
        elif share_mode == "quantity":
            share_type = "个数摊"
        else:
            share_type = old.get(
                "均摊类型",
                "",
            )

        rows.append(
            {
                "商品序号": idx,
                "商品名称": product_name,
                "商品数量": product["商品数量"],
                "计入均摊": include_share,
                "均摊类型": share_type,
                "商品均摊": product_share,
                "商品单价": prices.get(
                    product_name,
                    "",
                ),
            }
        )

    with output_path.open(
            "w",
            encoding="utf-8-sig",
            newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CONFIG_FIELDNAMES,
        )
        writer.writeheader()
        writer.writerows(rows)

    return {
        "ok": True,
        "config_file": str(
            output_path.resolve()
        ),
        "warnings": warnings,
    }

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
            name
            for name in reader.fieldnames
            if name not in ORDER_METADATA_COLUMNS
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
                        f"第 {row_idx} 行商品“{product_name}”数量必须是非负整数：{value!r}"
                    )

                quantity = int(value)

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
                "商品单价": "",
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

            product_quantity = parse_required_non_negative_int(
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

            product_unit_price = parse_optional_money_ceil(
                row.get("商品单价"),
                field_name="商品单价",
                row_idx=row_idx,
            )

            # 如果不计入均摊，商品均摊允许为空，也允许为 0.00。
            if not include_share:
                product_share_amount = normalize_zero_or_blank_money(product_share_amount)

            config = {
                "商品序号": product_no,
                "商品名称": product_name,
                "商品数量": product_quantity,
                "计入均摊": include_share,
                "均摊类型": str(row.get("均摊类型", "") or "").strip(),
                "商品均摊": product_share_amount,
                "商品单价": product_unit_price,
            }

            configs.append(config)

    return configs


def update_product_share_config_file(
    config_file: str | Path,
    updates: list[dict[str, Any]],
    share_type: str | None = None,
) -> dict[str, Any]:
    """
    根据用户对话输入，更新商品均摊配置表。

    updates 支持两种格式：
        {"商品序号": 1, "商品均摊": "10"}
        {"商品名称": "雪梅蜂", "商品均摊": "10"}

    share_type:
        可选。如果传入，则同步写入“均摊类型”列。
        例如：
            head_independent
            quantity_independent
    """
    config_file = Path(config_file)

    if not config_file.exists():
        raise FileNotFoundError(f"商品均摊配置表不存在：{config_file}")

    with config_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

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

    updated_items: list[dict[str, Any]] = []
    unmatched_updates: list[dict[str, Any]] = []

    for update in updates:
        target_no = update.get("商品序号")
        target_name = str(update.get("商品名称") or "").strip()
        amount_raw = update.get("商品均摊")

        amount = parse_optional_money_ceil(
            amount_raw,
            field_name="商品均摊",
            row_idx=0,
        )

        matched = False

        for row in rows:
            row_name = str(row.get("商品名称", "") or "").strip()

            if not row_name:
                continue

            row_no_text = str(row.get("商品序号", "") or "").strip()

            by_no = (
                target_no is not None
                and row_no_text.isdigit()
                and int(row_no_text) == int(target_no)
            )

            by_name = (
                target_name
                and row_name == target_name
            )

            if by_no or by_name:
                row["商品均摊"] = amount

                if share_type:
                    row["均摊类型"] = share_type

                updated_items.append(
                    {
                        "商品序号": row.get("商品序号"),
                        "商品名称": row_name,
                        "商品数量": row.get("商品数量"),
                        "计入均摊": row.get("计入均摊"),
                        "均摊类型": row.get("均摊类型"),
                        "商品均摊": amount,
                    }
                )

                matched = True
                break

        if not matched:
            unmatched_updates.append(update)

    with config_file.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CONFIG_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)

    summary = summarize_product_share_config(config_file)

    return {
        "ok": True,
        "config_file": str(config_file.resolve()),
        "updated_items": updated_items,
        "unmatched_updates": unmatched_updates,
        "summary": summary,
    }


def summarize_product_share_config(
    config_file: str | Path,
    total_amount: str | int | float | None = None,
) -> dict[str, Any]:
    """
    汇总商品均摊配置表。

    如果 total_amount 不为空，则检查：
        各商品均摊合计 == 用户输入总均摊

    如果 total_amount 为空，则代码自行计算总均摊：
        总均摊 = 所有计入均摊商品的“商品均摊”之和
    """
    config_file = Path(config_file)

    configs = load_product_share_config_file(config_file)

    items: list[dict[str, Any]] = []
    total = Decimal("0.00")

    for cfg in configs:
        include_share = bool(cfg.get("计入均摊"))
        product_name = str(cfg.get("商品名称") or "").strip()

        if not product_name:
            continue

        amount_text = str(cfg.get("商品均摊") or "").strip()

        if include_share and amount_text:
            amount = Decimal(amount_text)
            total += amount
        else:
            amount = Decimal("0.00")

        items.append(
            {
                "商品序号": cfg.get("商品序号"),
                "商品名称": product_name,
                "商品数量": cfg.get("商品数量"),
                "计入均摊": include_share,
                "均摊类型": cfg.get("均摊类型"),
                "商品均摊": f"{amount:.2f}" if amount_text else "",
            }
        )

    total = total.quantize(Decimal("0.01"), rounding=ROUND_CEILING)

    expected_total = None
    diff = None
    matched = None

    if total_amount is not None and str(total_amount).strip() != "":
        expected_total = Decimal(str(total_amount)).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING,
        )
        diff = (total - expected_total).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING,
        )
        matched = diff == Decimal("0.00")

    return {
        "ok": True,
        "config_file": str(config_file.resolve()),
        "items": items,
        "config_total": f"{total:.2f}",
        "expected_total": "" if expected_total is None else f"{expected_total:.2f}",
        "diff": "" if diff is None else f"{diff:.2f}",
        "matched": matched,
    }


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

    # 检查数字非负
    if number <= 0:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”必须是正整数，实际值：{value!r}"
        )

    return number


def parse_required_non_negative_int(
    value: Any,
    field_name: str,
    row_idx: int,
) -> int:
    """
    解析必填非负整数。

    不允许：
        空值
        -1
        1.5
        abc
    """
    if value is None:
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”不能为空，必须是非负整数。"
        )

    text = str(value).strip()

    if text == "":
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”不能为空，必须是非负整数。"
        )

    if not text.isdigit():
        raise ShareConfigError(
            f"第 {row_idx} 行“{field_name}”必须是非负整数，实际值：{value!r}"
        )

    number = int(text)

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
    不计入均摊的商品，商品均摊可以为空，也可以是 0.00。

    如果用户填了非 0 金额，这里不强制报错，先保留。
    如果你希望严格禁止，可以改成非 0 时报错。
    """
    if value == "":
        return ""

    return value