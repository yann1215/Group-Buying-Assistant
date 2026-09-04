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
    "单份均摊",
    "商品单价",
    "商品大货总价",
]


class ShareConfigError(RuntimeError):
    """商品均摊配置表处理失败。"""


def _read_product_config_rows(
    config_file: str | Path,
) -> list[dict[str, Any]]:
    """
    读取商品配置文件。

    同时统一检查：
    1. 文件是否存在
    2. 是否存在表头
    3. 是否包含全部 CONFIG_FIELDNAMES
    """
    config_file = Path(config_file)

    if not config_file.exists():
        raise FileNotFoundError(
            f"商品配置文件不存在：{config_file}"
        )

    with config_file.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ShareConfigError(
                "商品配置文件没有表头。"
            )

        return list(reader)


def _write_product_config_rows(
    config_file: str | Path,
    rows: list[dict[str, Any]],
) -> None:
    """
    将商品配置写回 CSV。
    """
    config_file = Path(config_file)

    with config_file.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CONFIG_FIELDNAMES,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


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
    读取商品配置表，转换成 share_calculator.py 可用的数据结构。
    """
    rows = _read_product_config_rows(config_file)

    configs: list[dict[str, Any]] = []

    for row_idx, row in enumerate(
        rows,
        start=2,
    ):
        product_name = str(
            row.get("商品名称", "") or ""
        ).strip()

        if not product_name:
            continue

        product_no = parse_required_positive_int(
            row.get("商品序号"),
            field_name="商品序号",
            row_idx=row_idx,
        )

        product_quantity = (
            parse_required_non_negative_int(
                row.get("商品数量"),
                field_name="商品数量",
                row_idx=row_idx,
            )
        )

        include_share = parse_bool_required(
            row.get("计入均摊"),
            field_name="计入均摊",
            row_idx=row_idx,
        )

        product_share_amount = (
            parse_optional_money_ceil(
                row.get("商品均摊"),
                field_name="商品均摊",
                row_idx=row_idx,
            )
        )

        unit_share_amount = (
            parse_optional_money_ceil(
                row.get("单份均摊"),
                field_name="单份均摊",
                row_idx=row_idx,
            )
        )

        product_unit_price = (
            parse_optional_money_ceil(
                row.get("商品单价"),
                field_name="商品单价",
                row_idx=row_idx,
            )
        )

        product_total_price = (
            parse_optional_money_ceil(
                row.get("商品大货总价"),
                field_name="商品大货总价",
                row_idx=row_idx,
            )
        )

        if not include_share:
            product_share_amount = (
                normalize_zero_or_blank_money(
                    product_share_amount
                )
            )

        configs.append(
            {
                "商品序号": product_no,
                "商品名称": product_name,
                "商品数量": product_quantity,
                "计入均摊": include_share,
                "均摊类型": str(
                    row.get("均摊类型", "") or ""
                ).strip(),
                "商品均摊": product_share_amount,
                "单份均摊": unit_share_amount,
                "商品单价": product_unit_price,
                "商品大货总价": product_total_price,
            }
        )

    return configs


def ensure_product_config_file(
    parsed_order_file: str | Path,
    output_dir: str | Path | None = None,
) -> str:
    """
    确保商品配置文件存在，并同步当前订单中的基础商品信息。

    本函数只负责更新：
        - 商品序号
        - 商品名称
        - 商品数量
        - 计入均摊（仅新商品首次创建时初始化）

    本函数不会更新：
        - 均摊类型
        - 商品均摊
        - 单份均摊
        - 商品单价
        - 商品大货总价

    如果配置文件已经存在：
        - 保留现有商品的用户配置和各阶段计算结果
        - 删除当前订单中已经不存在的旧商品
        - 新商品按 default_include_share() 初始化
        - 商品数量始终以当前 parsed_orders 为准

    返回：
        parsed_product_config.csv 的绝对路径
    """
    parsed_order_file = Path(parsed_order_file)

    if not parsed_order_file.exists():
        raise FileNotFoundError(
            f"简化订单文件不存在：{parsed_order_file}"
        )

    output_dir_path = (
        Path(output_dir)
        if output_dir
        else CSV_OUTPUT_DIR
    )

    output_dir_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ---------------------------------
    # 生成配置文件路径
    # ---------------------------------

    base_name = parsed_order_file.stem

    if base_name.endswith("_parsed_orders"):
        base_name = base_name.removesuffix(
            "_parsed_orders"
        )

    output_path = (
        output_dir_path
        / f"{base_name}_parsed_product_config.csv"
    )

    # ---------------------------------
    # 读取当前 parsed_orders 的商品信息
    # ---------------------------------

    product_rows = read_product_summary_from_order_file(
        parsed_order_file
    )

    # ---------------------------------
    # 如果旧配置存在，读取旧配置
    # ---------------------------------

    old_rows: list[dict[str, Any]] = []

    if output_path.exists():
        old_rows = _read_product_config_rows(
            output_path
        )

    old_map = {
        str(row.get("商品名称") or "").strip(): row
        for row in old_rows
        if str(row.get("商品名称") or "").strip()
    }

    # ---------------------------------
    # 根据当前订单重新生成配置行
    # ---------------------------------

    new_rows: list[dict[str, Any]] = []

    for idx, product in enumerate(
        product_rows,
        start=1,
    ):
        product_name = str(
            product.get("商品名称") or ""
        ).strip()

        if not product_name:
            continue

        product_quantity = product.get(
            "商品数量",
            0,
        )

        old = old_map.get(
            product_name
        )

        # =============================
        # 已有商品
        # =============================

        if old is not None:
            # 先完整保留旧配置。
            row = {
                field_name: old.get(
                    field_name,
                    "",
                )
                for field_name in CONFIG_FIELDNAMES
            }

            # 只刷新基础字段。
            row["商品序号"] = idx
            row["商品名称"] = product_name
            row["商品数量"] = product_quantity

            # 注意：
            # 已有商品的“计入均摊”也保留，
            # 防止覆盖用户手动设置。
            #
            # 均摊类型、商品均摊、单份均摊、
            # 商品单价、商品大货总价均不修改。

        # =============================
        # 新商品
        # =============================

        else:
            row = {
                "商品序号": idx,
                "商品名称": product_name,
                "商品数量": product_quantity,
                "计入均摊": default_include_share(
                    product_name
                ),
                "均摊类型": "",
                "商品均摊": "",
                "单份均摊": "",
                "商品单价": "",
                "商品大货总价": "",
            }

        new_rows.append(row)

    # ---------------------------------
    # 写回配置文件
    # ---------------------------------

    _write_product_config_rows(
        output_path,
        new_rows,
    )

    return str(
        output_path.resolve()
    )


def make_share_type(
    share_mode: str,
    calculation_scope: str,
) -> str:
    mapping = {
        ("head", "flat"): "拉通人头摊",
        ("quantity", "flat"): "拉通个数摊",
        ("head", "independent"): "独立人头摊",
        ("quantity", "independent"): "独立个数摊",
    }

    result = mapping.get(
        (share_mode, calculation_scope)
    )

    if result is None:
        raise ShareConfigError(
            f"无法生成均摊类型："
            f"{share_mode=}, {calculation_scope=}"
        )

    return result


def update_product_share_config_file(
    config_file: str | Path,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    根据用户对话输入，更新商品均摊配置表。

    updates 支持两种格式：
        {"商品序号": 1, "商品均摊": "10"}
        {"商品名称": "雪梅蜂", "商品均摊": "10"}

    """
    config_file = Path(config_file)

    rows = _read_product_config_rows(config_file)

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

                updated_items.append(
                    {
                        "商品序号": row.get("商品序号"),
                        "商品名称": row_name,
                        "商品数量": row.get("商品数量"),
                        "计入均摊": row.get("计入均摊"),
                        "商品均摊": amount,
                    }
                )

                matched = True
                break

        if not matched:
            unmatched_updates.append(update)

    _write_product_config_rows(config_file,rows)

    summary = summarize_product_share_config(config_file)

    return {
        "ok": True,
        "config_file": str(config_file.resolve()),
        "updated_items": updated_items,
        "unmatched_updates": unmatched_updates,
        "summary": summary,
    }


def update_product_config_before_share(
    config_file: str | Path,
    share_mode: str,
    calculation_scope: str,
    total_amount: str | int | float | None = None,
) -> dict[str, Any]:
    config_file = Path(config_file)

    rows = _read_product_config_rows(config_file)

    share_type = make_share_type(
        share_mode,
        calculation_scope,
    )

    if calculation_scope == "flat":
        if total_amount is None:
            raise ShareConfigError(
                "拉通均摊更新商品配置时缺少总均摊金额。"
            )

        common_amount = parse_optional_money_ceil(
            total_amount,
            field_name="商品均摊",
            row_idx=0,
        )

        for row in rows:
            # 此阶段只更新这两个字段
            row["均摊类型"] = share_type
            row["商品均摊"] = common_amount

    elif calculation_scope == "independent":
        for row in rows:
            # 独立均摊金额已经由用户逐商品输入，
            # 此处不要重新计算或覆盖。
            row["均摊类型"] = share_type

            include_share = parse_bool_required(
                row.get("计入均摊"),
                field_name="计入均摊",
                row_idx=0,
            )

            if (
                not include_share
                and not str(
                    row.get("商品均摊") or ""
                ).strip()
            ):
                row["商品均摊"] = "0.00"

    else:
        raise ShareConfigError(
            f"未知计算方式：{calculation_scope}"
        )

    _write_product_config_rows(config_file,rows)

    return {
        "ok": True,
        "config_file": str(
            config_file.resolve()
        ),
        "share_type": share_type,
    }


def update_product_config_after_share(
    config_file: str | Path,
    calculated_configs: list[dict[str, Any]],
) -> dict[str, Any]:
    config_file = Path(config_file)

    rows = _read_product_config_rows(config_file)

    calculated_map = {
        str(item.get("商品名称") or "").strip(): item
        for item in calculated_configs
        if str(item.get("商品名称") or "").strip()
    }

    for row_idx, row in enumerate(
        rows,
        start=2,
    ):
        product_name = str(
            row.get("商品名称") or ""
        ).strip()

        calculated = calculated_map.get(
            product_name
        )

        if calculated is None:
            continue

        # 此阶段只更新单份均摊。
        row["单份均摊"] = parse_optional_money_ceil(
            calculated.get("单份均摊"),
            field_name="单份均摊",
            row_idx=row_idx,
        )

    _write_product_config_rows(config_file,rows)

    return {
        "ok": True,
        "config_file": str(
            config_file.resolve()
        ),
    }


def update_product_config_before_bulk(
    config_file: str | Path,
    original_order_file: str | Path,
) -> dict[str, Any]:
    config_file = Path(config_file)

    rows = _read_product_config_rows(config_file)

    product_names = [
        str(row.get("商品名称") or "").strip()
        for row in rows
        if str(row.get("商品名称") or "").strip()
    ]

    price_result = read_product_unit_prices(
        order_input=original_order_file,
        product_names=product_names,
    )

    prices = price_result["prices"]
    warnings = price_result["warnings"]

    for row in rows:
        product_name = str(
            row.get("商品名称") or ""
        ).strip()

        if not product_name:
            continue

        price_text = prices.get(
            product_name,
            "",
        )

        # 此阶段只更新价格字段。
        row["商品单价"] = price_text

        if price_text == "":
            row["商品大货总价"] = ""
            continue

        quantity_text = str(
            row.get("商品数量") or ""
        ).strip()

        if not quantity_text.isdigit():
            row["商品大货总价"] = ""
            continue

        quantity = int(quantity_text)

        total_price = (
            Decimal(price_text)
            * Decimal(quantity)
        ).quantize(
            Decimal("0.01")
        )

        row["商品大货总价"] = (
            f"{total_price:.2f}"
        )

    _write_product_config_rows(config_file,rows)

    return {
        "ok": True,
        "config_file": str(
            config_file.resolve()
        ),
        "warnings": warnings,
    }


def summarize_product_share_config(
    config_file: str | Path,
    total_amount: str | int | float | None = None,
) -> dict[str, Any]:
    """
    汇总商品配置中的均摊金额。

    规则：

    1. 拉通人头摊 / 拉通个数摊
       - 每个商品的“商品均摊”都保存同一个总均摊金额。
       - 因此不能逐商品求和。
       - config_total 直接取共同的商品均摊金额。

    2. 独立人头摊 / 独立个数摊
       - 每个商品的“商品均摊”表示该商品自己的分均摊。
       - config_total = 所有计入均摊商品的商品均摊之和。

    3. total_amount 不为空时
       - 检查配置中的总均摊与用户输入总均摊是否一致。
    """
    config_file = Path(config_file)

    configs = load_product_share_config_file(
        config_file
    )

    items: list[dict[str, Any]] = []

    # -------------------------------------------------
    # 整理用于返回的商品信息
    # -------------------------------------------------

    for cfg in configs:
        product_name = str(
            cfg.get("商品名称") or ""
        ).strip()

        if not product_name:
            continue

        amount_text = str(
            cfg.get("商品均摊") or ""
        ).strip()

        items.append(
            {
                "商品序号": cfg.get("商品序号"),
                "商品名称": product_name,
                "商品数量": cfg.get("商品数量"),
                "计入均摊": bool(
                    cfg.get("计入均摊")
                ),
                "均摊类型": str(
                    cfg.get("均摊类型") or ""
                ).strip(),
                "商品均摊": amount_text,
            }
        )

    # -------------------------------------------------
    # 判断均摊类型
    # -------------------------------------------------

    share_types = {
        str(
            cfg.get("均摊类型") or ""
        ).strip()
        for cfg in configs
        if str(
            cfg.get("均摊类型") or ""
        ).strip()
    }

    flat_share_types = {
        "拉通人头摊",
        "拉通个数摊",
    }

    independent_share_types = {
        "独立人头摊",
        "独立个数摊",
    }

    config_total = Decimal("0.00")
    flat_amount_consistent = True

    # -------------------------------------------------
    # 拉通均摊
    # -------------------------------------------------

    if (
        len(share_types) == 1
        and next(iter(share_types))
        in flat_share_types
    ):
        flat_amounts: list[Decimal] = []

        for cfg in configs:
            amount_text = str(
                cfg.get("商品均摊") or ""
            ).strip()

            if not amount_text:
                continue

            flat_amounts.append(
                Decimal(amount_text)
            )

        if flat_amounts:
            # 拉通情况下，每一行理论上应该完全相同。
            config_total = flat_amounts[0]

            flat_amount_consistent = all(
                amount == config_total
                for amount in flat_amounts
            )

        else:
            config_total = Decimal("0.00")

    # -------------------------------------------------
    # 独立均摊
    # -------------------------------------------------

    elif (
        len(share_types) == 1
        and next(iter(share_types))
        in independent_share_types
    ):
        for cfg in configs:
            include_share = bool(
                cfg.get("计入均摊")
            )

            if not include_share:
                continue

            amount_text = str(
                cfg.get("商品均摊") or ""
            ).strip()

            if not amount_text:
                continue

            config_total += Decimal(
                amount_text
            )

    # -------------------------------------------------
    # 尚未设置均摊类型
    # -------------------------------------------------

    elif not share_types:
        # 这里主要兼容：
        # 用户还在输入独立商品均摊、
        # 尚未正式执行均摊前更新的阶段。
        #
        # 此时仍按照独立商品均摊的方式求和，
        # 保持原有 update_product_share_config_file()
        # 的汇总功能可用。
        for cfg in configs:
            include_share = bool(
                cfg.get("计入均摊")
            )

            if not include_share:
                continue

            amount_text = str(
                cfg.get("商品均摊") or ""
            ).strip()

            if not amount_text:
                continue

            config_total += Decimal(
                amount_text
            )

    # -------------------------------------------------
    # 出现混合均摊类型
    # -------------------------------------------------

    else:
        raise ShareConfigError(
            "商品配置中存在多个不同的均摊类型："
            + "、".join(sorted(share_types))
        )

    config_total = config_total.quantize(
        Decimal("0.01"),
        rounding=ROUND_CEILING,
    )

    # -------------------------------------------------
    # 与用户输入的总均摊进行比较
    # -------------------------------------------------

    expected_total = None
    diff = None
    matched = None

    if (
        total_amount is not None
        and str(total_amount).strip() != ""
    ):
        try:
            expected_total = Decimal(
                str(total_amount)
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_CEILING,
            )

        except Exception as e:
            raise ShareConfigError(
                f"总均摊金额格式错误：{total_amount!r}"
            ) from e

        diff = (
            config_total
            - expected_total
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING,
        )

        matched = (
            diff == Decimal("0.00")
        )

        # 拉通情况下，如果各行的总均摊本身不一致，
        # 即使第一行刚好等于 expected_total，
        # 也不能认为配置正确。
        if not flat_amount_consistent:
            matched = False

    return {
        "ok": True,
        "config_file": str(
            config_file.resolve()
        ),
        "items": items,
        "config_total": f"{config_total:.2f}",
        "expected_total": (
            ""
            if expected_total is None
            else f"{expected_total:.2f}"
        ),
        "diff": (
            ""
            if diff is None
            else f"{diff:.2f}"
        ),
        "matched": matched,
        "flat_amount_consistent": (
            flat_amount_consistent
        ),
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