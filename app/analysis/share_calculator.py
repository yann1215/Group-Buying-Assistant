# app/analysis/share_calculator.py

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.config import CSV_OUTPUT_DIR, ensure_dirs
from app.analysis.order_validator import (
    default_include_share,
    find_orders_with_only_non_share_products,
    format_only_non_share_orders_message,
)

ORDER_METADATA_COLUMNS = {"单号", "昵称", "总金额"}


class ShareCalculateError(RuntimeError):
    """均摊计算失败。"""


@dataclass
class OrderRow:
    order_no: str
    nickname: str
    quantities: dict[str, int]


@dataclass
class ProductShareConfig:
    """
    商品均摊配置。

    字段对应商品均摊配置表：
        商品序号
        商品名称
        商品数量
        计入均摊
        均摊类型
        商品均摊
        单份均摊
        商品单价
        商品大货总价
    """
    product_no: int | None = None
    product_name: str = ""
    product_quantity: int | None = None
    include_share: bool = False
    share_type: str = ""
    product_share_amount: Decimal | None = None
    unit_share_price: Decimal | None = None
    product_unit_price: Decimal | None = None
    product_total_price: Decimal | None = None


@dataclass
class ParticipantResult:
    order_no: str
    nickname: str
    total_quantity: int = 0
    share_cents: int = 0
    details: dict[str, int] = field(default_factory=dict)


def calculate_share(
    parsed_order_file: str | Path,
    total_amount: int | float | str | None,
    share_mode: str,
    calculation_scope: str = "flat",
    product_configs: list[dict[str, Any]] | None = None,
    output_dir: str | Path | None = None,
    max_product_slots: int = 20,
    excluded_order_nos: set[str] | None = None,
) -> dict[str, Any]:
    """
    均摊计算主函数。

    parsed_order_file:
        order_parser.py 输出的宽表 CSV：
            单号, 昵称, 总金额, 商品A, 商品B, 商品C...

    total_amount:
        拉通计算时使用的总均摊金额。
        独立计算时可以为空，但每个参摊商品需要提供 product_share_amount。

    share_mode:
        head / 人头摊
        quantity / 个数摊

    calculation_scope:
        flat / 拉通
        independent / 独立

    product_configs:
        商品配置数组，长度最多 20。
        可以先不传，不传时自动根据商品名称生成默认配置。

    计算金额规则：
        每个人每项应收金额向上取整到 0.01 元。
        允许总收款大于原始均摊金额。
        返回多收金额。
    """
    parsed_order_file = Path(parsed_order_file)

    if not parsed_order_file.exists():
        raise FileNotFoundError(f"简化订单文件不存在：{parsed_order_file}")

    share_mode = normalize_share_mode(share_mode)
    calculation_scope = normalize_calculation_scope(calculation_scope)

    ensure_dirs()
    output_dir_path = Path(output_dir) if output_dir else CSV_OUTPUT_DIR
    output_dir_path.mkdir(parents=True, exist_ok=True)

    order_rows, product_names = read_order_rows(parsed_order_file)

    # 排除不参摊成员的订单
    excluded_order_no_set = {
        normalize_order_no_for_compare(order_no)
        for order_no in (excluded_order_nos or set())
    }

    excluded_order_rows = [
        row
        for row in order_rows
        if normalize_order_no_for_compare(row.order_no)
           in excluded_order_no_set
    ]

    order_rows = [
        row
        for row in order_rows
        if normalize_order_no_for_compare(row.order_no)
           not in excluded_order_no_set
    ]

    if not order_rows:
        raise ShareCalculateError(
            "排除不参摊特殊成员后，没有可参与均摊的订单。"
        )

    if len(product_names) > max_product_slots:
        raise ShareCalculateError(
            f"当前商品数量为 {len(product_names)}，超过预留上限 {max_product_slots}。"
        )

    configs = build_product_configs(
        product_names=product_names,
        order_rows=order_rows,
        product_configs=product_configs,
        max_product_slots=max_product_slots,
        global_share_mode=share_mode,
        calculation_scope=calculation_scope,
    )

    # -------------------------------------------------
    # 个数摊计算前检查：
    # 是否存在只购买了不参摊商品的订单
    # -------------------------------------------------
    if share_mode == "quantity":
        abnormal_orders = find_orders_with_only_non_share_products(
            order_rows=order_rows,
            product_configs=configs,
        )

        if abnormal_orders:
            return {
                "ok": False,
                "need_user_input": False,
                "error_code": "orders_only_non_share_products",
                "message": format_only_non_share_orders_message(
                    abnormal_orders=abnormal_orders,
                    operation_name="个数摊计算",
                ),
                "abnormal_order_nos": [
                    order["单号"]
                    for order in abnormal_orders
                ],
                "abnormal_orders": abnormal_orders,
                "product_configs": product_configs_to_dicts(configs),
            }

    active_configs = [
        cfg for cfg in configs
        if cfg.product_name and cfg.include_share
    ]

    if not active_configs:
        raise ShareCalculateError("没有可参摊商品。")

    participant_results = {
        row.order_no: ParticipantResult(
            order_no=row.order_no,
            nickname=row.nickname,
            total_quantity=sum(row.quantities.values()),
        )
        for row in order_rows
    }

    # 个数摊时，用于保存“参摊商品数量（总数）”
    summary_total_share_quantity: int | None = None
    if share_mode == "quantity":
        summary_total_share_quantity = calculate_total_share_quantity_for_summary(
            order_rows=order_rows,
            configs=active_configs,
        )

    # 个数摊时，用于保存“单个商品均摊金额”
    # 其他均摊模式下保持为 None。
    unit_share_cents: int | None = None
    total_share_quantity: int | None = None
    if calculation_scope == "flat":
        if total_amount is None:
            return {
                "ok": False,
                "need_user_input": True,
                "message": "拉通均摊需要提供总均摊金额。",
                "missing_fields": ["total_amount"],
                "product_configs": product_configs_to_dicts(configs),
            }

        total_amount_decimal = amount_to_decimal(total_amount)
        total_original_cents = decimal_yuan_to_cents(
            total_amount_decimal
        )

        unit_share_cents, _ = calculate_flat_share(
            order_rows=order_rows,
            configs=active_configs,
            participant_results=participant_results,
            total_amount=total_amount_decimal,
            share_mode=share_mode,
        )

    elif calculation_scope == "independent":
        missing_products = [
            cfg.product_name
            for cfg in active_configs
            if cfg.product_share_amount is None
        ]

        if missing_products:
            return {
                "ok": False,
                "need_user_input": True,
                "message": "独立均摊需要补充每个参摊商品的商品均摊金额。",
                "missing_fields": [
                    {
                        "商品名称": name,
                        "缺少字段": "商品均摊",
                    }
                    for name in missing_products
                ],
                "product_configs": product_configs_to_dicts(configs),
            }

        total_original_cents = sum(
            decimal_yuan_to_cents(cfg.product_share_amount)
            for cfg in active_configs
            if cfg.product_share_amount is not None
        )

        calculate_independent_share(
            order_rows=order_rows,
            configs=active_configs,
            participant_results=participant_results,
        )

    else:
        raise ShareCalculateError(f"未知计算范围：{calculation_scope}")

    charged_results = [
        result for result in participant_results.values()
        if result.share_cents > 0
    ]

    total_collected_cents = sum(item.share_cents for item in charged_results)
    over_collected_cents = total_collected_cents - total_original_cents

    # timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # output_path = (
    #     output_dir_path
    #     / f"{parsed_order_file.stem}_share_{share_mode}_{calculation_scope}_{timestamp}.csv"
    # )
    output_path = (
            output_dir_path
            / f"{parsed_order_file.stem}_share_{share_mode}_{calculation_scope}.csv"
    )

    write_share_result_csv(
        output_path=output_path,
        results=charged_results,
        configs=active_configs,
        calculation_scope=calculation_scope,
    )

    return {
        "ok": True,
        "share_mode": share_mode,
        "share_mode_text": share_mode_to_text(share_mode),
        "calculation_scope": calculation_scope,
        "calculation_scope_text": calculation_scope_to_text(calculation_scope),
        "total_amount": cents_to_yuan_text(total_original_cents),
        "total_share_quantity": summary_total_share_quantity,
        "unit_share_amount": (
            cents_to_yuan_text(unit_share_cents)
            if unit_share_cents is not None
            else None
        ),
        "total_collected": cents_to_yuan_text(total_collected_cents),
        "over_collected": cents_to_yuan_text(over_collected_cents),
        "participant_count": len(charged_results),
        "result_file": str(output_path.resolve()),
        "product_configs": product_configs_to_dicts(configs),
        "items": [
            {
                "单号": item.order_no,
                "昵称": item.nickname,
                "商品总数": item.total_quantity,
                "应收金额": cents_to_yuan_text(item.share_cents),
                "明细": {
                    product_name: cents_to_yuan_text(cents)
                    for product_name, cents in item.details.items()
                },
            }
            for item in charged_results
        ],
    }


def read_order_rows(parsed_order_file: Path) -> tuple[list[OrderRow], list[str]]:
    with parsed_order_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise ShareCalculateError("简化订单文件没有表头。")

        if "单号" not in reader.fieldnames or "昵称" not in reader.fieldnames:
            raise ShareCalculateError("简化订单文件必须包含“单号”和“昵称”列。")

        product_names = [
            col
            for col in reader.fieldnames
            if col not in ORDER_METADATA_COLUMNS
        ]

        rows: list[OrderRow] = []

        for row_idx, row in enumerate(reader, start=2):
            order_no = str(row.get("单号", "") or "").strip()
            nickname = str(row.get("昵称", "") or "").strip()

            if not order_no:
                raise ShareCalculateError(f"第 {row_idx} 行缺少单号。")

            quantities: dict[str, int] = {}

            for product_name in product_names:
                value = str(row.get(product_name, "") or "").strip()

                if value == "":
                    continue

                if not value.isdigit():
                    raise ShareCalculateError(
                        f"第 {row_idx} 行商品“{product_name}”数量不是正整数：{value!r}"
                    )

                quantity = int(value)

                if quantity <= 0:
                    raise ShareCalculateError(
                        f"第 {row_idx} 行商品“{product_name}”数量不是正整数：{value!r}"
                    )

                quantities[product_name] = quantity

            if quantities:
                rows.append(
                    OrderRow(
                        order_no=order_no,
                        nickname=nickname,
                        quantities=quantities,
                    )
                )

        return rows, product_names


def normalize_order_no_for_compare(
    value: Any,
) -> str:
    text = str(value or "").strip()

    if not text.isdigit():
        return text

    return str(int(text))


def build_product_configs(
    product_names: list[str],
    order_rows: list[OrderRow],
    product_configs: list[dict[str, Any]] | None,
    max_product_slots: int,
    global_share_mode: str,
    calculation_scope: str,
) -> list[ProductShareConfig]:
    """
    根据订单商品列和商品均摊配置表生成 ProductShareConfig。

    配置表字段：
        商品序号
        商品名称
        商品数量
        计入均摊
        均摊类型
        商品均摊
        单份均摊
        商品单价
        商品大货总价
    """
    supplied_config_map: dict[str, dict[str, Any]] = {}

    for cfg in product_configs or []:
        name = str(cfg.get("商品名称") or "").strip()
        if name:
            supplied_config_map[name] = cfg

    result: list[ProductShareConfig] = []

    for idx, product_name in enumerate(product_names, start=1):
        total_quantity = sum(
            row.quantities.get(product_name, 0)
            for row in order_rows
        )

        supplied = supplied_config_map.get(product_name)

        if supplied:
            product_no = parse_optional_positive_int(
                supplied.get("商品序号"),
                default=idx,
            )

            product_quantity = parse_optional_positive_int(
                supplied.get("商品数量"),
                default=total_quantity,
            )

            include_share = parse_include_share_required(
                supplied.get("计入均摊"),
                product_name=product_name,
            )

            share_type = str(
                supplied.get("均摊类型") or make_share_type(global_share_mode, calculation_scope)
            ).strip()

            product_share_amount = optional_money_to_decimal_allow_zero(supplied.get("商品均摊"))
            unit_share_price = optional_money_to_decimal_allow_zero(supplied.get("单份均摊"))
            product_unit_price = optional_money_to_decimal_allow_zero(supplied.get("商品单价"))
            product_total_price = optional_money_to_decimal_allow_zero(supplied.get("商品大货总价"))

        else:
            product_no = idx
            product_quantity = total_quantity
            include_share = default_include_share(product_name)
            share_type = make_share_type(global_share_mode, calculation_scope)
            product_share_amount = None
            unit_share_price = None
            product_unit_price = None
            product_total_price = None

        result.append(
            ProductShareConfig(
                product_no=product_no,
                product_name=product_name,
                product_quantity=product_quantity,
                include_share=include_share,
                share_type=share_type,
                product_share_amount=product_share_amount,
                unit_share_price=unit_share_price,
                product_unit_price=product_unit_price,
                product_total_price=product_total_price,
            )
        )

    while len(result) < max_product_slots:
        result.append(
            ProductShareConfig(
                product_no=len(result) + 1,
                product_name="",
                product_quantity=None,
                include_share=False,
                share_type="",
                product_share_amount=None,
                unit_share_price=None,
                product_unit_price=None,
                product_total_price=None,
            )
        )

    return result


def calculate_flat_share(
    order_rows: list[OrderRow],
    configs: list[ProductShareConfig],
    participant_results: dict[str, ParticipantResult],
    total_amount: Decimal,
    share_mode: str,
) -> tuple[int | None, int | None]:
    """
    计算拉通均摊。

    人头摊：
        每个人的均摊金额单独计算，并向上取整到 0.01 元。

    个数摊：
        1. 先计算单个参摊商品需要均摊多少钱；
        2. 单个均摊向上取整到 0.01 元；
        3. 每个人应收金额 = 单个均摊 × 个人参摊个数；
        4. 个人总金额不再重复取整。

    返回值：
        个数摊时返回单个均摊金额，单位为分。
        人头摊时返回 None。
    """
    eligible_product_names = {
        cfg.product_name
        for cfg in configs
        if cfg.product_name and cfg.include_share
    }

    weights: dict[str, int] = {}

    for row in order_rows:
        if share_mode == "head":
            has_any_eligible_product = any(
                product_name in eligible_product_names
                and quantity > 0
                for product_name, quantity
                in row.quantities.items()
            )

            if has_any_eligible_product:
                weights[row.order_no] = 1

        elif share_mode == "quantity":
            quantity_weight = 0

            for product_name, quantity in row.quantities.items():
                if product_name not in eligible_product_names:
                    continue

                # “摊画师……”和“摊供稿人……”
                # 不计入个数摊数量权重。
                if is_special_non_quantity_product(product_name):
                    continue

                quantity_weight += quantity

            if quantity_weight > 0:
                weights[row.order_no] = quantity_weight

        else:
            raise ShareCalculateError(
                f"未知均摊方式：{share_mode}"
            )

    if not weights:
        raise ShareCalculateError(
            "没有可参与拉通均摊的订单。"
        )

    total_weight = sum(weights.values())

    # -------------------------------------------------
    # 拉通个数摊
    # -------------------------------------------------
    if share_mode == "quantity":
        # 先计算单个商品均摊金额。
        unit_amount = (
            total_amount
            / Decimal(total_weight)
        )

        # 单个金额先向上取整到 0.01 元。
        unit_cents = ceil_yuan_decimal_to_cents(
            unit_amount
        )

        unit_share_price = (
            Decimal(unit_cents)
            / Decimal("100")
        )

        # 将单份均摊写入商品配置返回结果。
        # 特殊的不计个数商品不写入。
        for cfg in configs:
            if not cfg.product_name:
                continue

            if not cfg.include_share:
                continue

            if is_special_non_quantity_product(
                cfg.product_name
            ):
                continue

            cfg.unit_share_price = unit_share_price

        for row in order_rows:
            quantity_weight = weights.get(
                row.order_no,
                0,
            )

            if quantity_weight <= 0:
                continue

            # 个人金额直接使用：
            # 单个均摊金额 × 个人参摊个数。
            #
            # unit_cents 已经是整数分，因此这里不需要、
            # 也不应该再进行任何金额取整。
            person_cents = (
                unit_cents
                * quantity_weight
            )

            participant = participant_results[
                row.order_no
            ]

            participant.share_cents += person_cents
            participant.details["拉通均摊"] = (
                person_cents
            )

        return unit_cents, total_weight

    # -------------------------------------------------
    # 拉通人头摊
    # -------------------------------------------------
    per_person_amount = (
            total_amount / Decimal(total_weight)
    )

    per_person_cents = ceil_yuan_decimal_to_cents(
        per_person_amount
    )

    for row in order_rows:
        weight = weights.get(row.order_no, 0)

        if weight <= 0:
            continue

        participant = participant_results[
            row.order_no
        ]

        participant.share_cents += per_person_cents

        participant.details["拉通均摊"] = (
            per_person_cents
        )

    return per_person_cents, None


def calculate_independent_share(
    order_rows: list[OrderRow],
    configs: list[ProductShareConfig],
    participant_results: dict[str, ParticipantResult],
) -> None:
    for cfg in configs:
        if not cfg.product_name:
            continue

        if not cfg.include_share:
            continue

        if cfg.product_share_amount is None:
            raise ShareCalculateError(f"商品“{cfg.product_name}”缺少商品均摊金额。")

        share_type = normalize_share_type(cfg.share_type)

        if share_type == "head_independent":
            calculate_one_product_head_independent(
                order_rows=order_rows,
                cfg=cfg,
                participant_results=participant_results,
            )

        elif share_type == "quantity_independent":
            # “摊画师……”和“摊供稿人……”不参与个数摊计算
            if is_special_non_quantity_product(cfg.product_name):
                continue

            calculate_one_product_quantity_independent(
                order_rows=order_rows,
                cfg=cfg,
                participant_results=participant_results,
            )

        else:
            raise ShareCalculateError(
                f"独立均摊时，商品“{cfg.product_name}”的均摊类型不支持：{cfg.share_type}"
            )


def calculate_one_product_head_independent(
    order_rows: list[OrderRow],
    cfg: ProductShareConfig,
    participant_results: dict[str, ParticipantResult],
) -> None:
    buyers = [
        row for row in order_rows
        if row.quantities.get(cfg.product_name, 0) > 0
    ]

    if not buyers:
        return

    per_person_amount = cfg.product_share_amount / Decimal(len(buyers))
    per_person_cents = ceil_yuan_decimal_to_cents(per_person_amount)

    cfg.unit_share_price = Decimal(per_person_cents) / Decimal("100")

    for row in buyers:
        participant_results[row.order_no].share_cents += per_person_cents
        participant_results[row.order_no].details[cfg.product_name] = per_person_cents


def calculate_one_product_quantity_independent(
    order_rows: list[OrderRow],
    cfg: ProductShareConfig,
    participant_results: dict[str, ParticipantResult],
) -> None:
    total_quantity = sum(
        row.quantities.get(cfg.product_name, 0)
        for row in order_rows
    )

    if total_quantity <= 0:
        return

    unit_amount = cfg.product_share_amount / Decimal(total_quantity)
    unit_cents = ceil_yuan_decimal_to_cents(unit_amount)

    cfg.unit_share_price = Decimal(unit_cents) / Decimal("100")

    for row in order_rows:
        quantity = row.quantities.get(cfg.product_name, 0)

        if quantity <= 0:
            continue

        cents = unit_cents * quantity

        participant_results[row.order_no].share_cents += cents
        participant_results[row.order_no].details[cfg.product_name] = cents


def calculate_total_share_quantity_for_summary(
    order_rows: list[OrderRow],
    configs: list[ProductShareConfig],
) -> int:
    eligible_product_names = {
        cfg.product_name
        for cfg in configs
        if cfg.product_name and cfg.include_share
    }

    total_quantity = 0

    for row in order_rows:
        for product_name, quantity in row.quantities.items():
            if product_name not in eligible_product_names:
                continue

            if is_special_non_quantity_product(product_name):
                continue

            total_quantity += quantity

    return total_quantity


def write_share_result_csv(
    output_path: Path,
    results: list[ParticipantResult],
    configs: list[ProductShareConfig],
    calculation_scope: str,
) -> None:
    """
    输出均摊结果 CSV。

    拉通均摊：
        只输出单号、昵称、商品总数、应收金额。
        因为拉通均摊只计算每个订单的总应收金额，
        不会计算每个商品分别承担了多少钱。

    独立均摊：
        在基础列后增加每个参摊商品名称。
        商品列中写入该订单在对应商品上的均摊金额。
    """
    base_fieldnames = [
        "单号",
        "昵称",
        "商品总数",
        "应收金额",
    ]

    if calculation_scope == "independent":
        product_detail_columns = [
            cfg.product_name
            for cfg in configs
            if cfg.product_name and cfg.include_share
        ]
    else:
        product_detail_columns = []

    fieldnames = base_fieldnames + product_detail_columns

    with output_path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
        )
        writer.writeheader()

        for item in results:
            row_data: dict[str, Any] = {
                "单号": item.order_no,
                "昵称": item.nickname,
                "商品总数": item.total_quantity,
                "应收金额": cents_to_yuan_text(
                    item.share_cents,
                ),
            }

            if calculation_scope == "independent":
                for product_name in product_detail_columns:
                    cents = item.details.get(product_name)

                    row_data[product_name] = (
                        ""
                        if cents is None
                        else cents_to_yuan_text(cents)
                    )

            writer.writerow(row_data)


def product_configs_to_dicts(configs: list[ProductShareConfig]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for cfg in configs:
        result.append(
            {
                "商品序号": cfg.product_no,
                "商品名称": cfg.product_name,
                "商品数量": cfg.product_quantity,
                "计入均摊": cfg.include_share if cfg.product_name else "",
                "均摊类型": cfg.share_type,
                "商品均摊": decimal_to_text_or_blank(cfg.product_share_amount),
                "单份均摊": decimal_to_text_or_blank(cfg.unit_share_price),
                "商品单价": decimal_to_text_or_blank(cfg.product_unit_price),
                "商品大货总价": decimal_to_text_or_blank(cfg.product_total_price),
            }
        )

    return result


def is_special_non_quantity_product(product_name: str) -> bool:
    name = str(product_name or "").strip()
    special_1 = name.startswith(("摊画师", "画师摊", "画师专", "画师各", "画师一", "画师二", "画师1", "画师2"))
    special_2 = name.startswith(("摊供稿", "供稿", "摊章稿", "摊授权", "授权老师", "授权专"))
    special_3 = name.endswith(("专拍"))
    special_flag = special_1 or special_2 or special_3
    return special_flag


def make_share_type(global_share_mode: str, calculation_scope: str) -> str:
    return f"{global_share_mode}_{calculation_scope}"


def normalize_share_mode(value: str) -> str:
    value = str(value or "").strip().lower()

    if value in {"head", "person", "people", "人头摊", "按人头", "按人", "每人", "人均"}:
        return "head"

    if value in {"quantity", "count", "item", "个数摊", "按个数", "按数量", "按件数", "按件"}:
        return "quantity"

    raise ShareCalculateError(f"无法识别均摊方式：{value}")


def normalize_calculation_scope(value: str) -> str:
    value = str(value or "").strip().lower()

    if value in {"flat", "拉通", "拉通摊", "整体", "整体摊", "统一"}:
        return "flat"

    if value in {"independent", "独立", "独立摊", "单独", "分别", "分别摊"}:
        return "independent"

    raise ShareCalculateError(f"无法识别计算方式：{value}")


def normalize_share_type(value: str) -> str:
    value = str(value or "").strip().lower()

    mapping = {
        "head_flat": "head_flat",
        "人头摊-拉通": "head_flat",
        "人头拉通": "head_flat",

        "quantity_flat": "quantity_flat",
        "个数摊-拉通": "quantity_flat",
        "个数拉通": "quantity_flat",

        "head_independent": "head_independent",
        "人头摊-独立": "head_independent",
        "人头独立": "head_independent",

        "quantity_independent": "quantity_independent",
        "个数摊-独立": "quantity_independent",
        "个数独立": "quantity_independent",
    }

    if value in mapping:
        return mapping[value]

    raise ShareCalculateError(f"无法识别商品均摊类型：{value}")


def amount_to_decimal(value: int | float | str) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    except Exception as e:
        raise ShareCalculateError(f"金额格式错误：{value!r}") from e

    if amount <= 0:
        raise ShareCalculateError(f"金额必须大于 0：{value!r}")

    return amount


def optional_money_to_decimal_allow_zero(value: Any) -> Decimal | None:
    if value is None:
        return None

    text = str(value).strip()
    if text == "":
        return None

    try:
        amount = Decimal(text).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING,
        )
    except Exception as e:
        raise ShareCalculateError(f"金额格式错误：{value!r}") from e

    if amount < 0:
        raise ShareCalculateError(f"金额不能为负数：{value!r}")

    return amount


def optional_amount_to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None

    if str(value).strip() == "":
        return None

    return amount_to_decimal(value)


def decimal_yuan_to_cents(value: Decimal) -> int:
    return int((value * Decimal("100")).to_integral_value())


def ceil_yuan_decimal_to_cents(value: Decimal) -> int:
    rounded = value.quantize(
        Decimal("0.01"),
        rounding=ROUND_CEILING,
    )
    return decimal_yuan_to_cents(rounded)


def cents_to_yuan_text(cents: int) -> str:
    yuan = cents // 100
    fen = cents % 100
    return f"{yuan}.{fen:02d}"


def decimal_to_text_or_blank(value: Decimal | None) -> str:
    if value is None:
        return ""

    return f"{value:.2f}"


def share_mode_to_text(share_mode: str) -> str:
    if share_mode == "head":
        return "人头摊"

    if share_mode == "quantity":
        return "个数摊"

    return share_mode


def calculation_scope_to_text(calculation_scope: str) -> str:
    if calculation_scope == "flat":
        return "拉通"

    if calculation_scope == "independent":
        return "独立"

    return calculation_scope


def parse_include_share_required(value: Any, product_name: str) -> bool:
    """
    解析“计入均摊”。

    配置表中这个字段必须是 bool 语义：
        True / False
        true / false
        TRUE / FALSE
        是 / 否
        1 / 0
    """
    if value is None or str(value).strip() == "":
        raise ShareCalculateError(
            f"商品“{product_name}”缺少“计入均摊”字段。"
        )

    text = str(value).strip()

    if text in {"True", "true", "TRUE", "1", "是"}:
        return True

    if text in {"False", "false", "FALSE", "0", "否"}:
        return False

    raise ShareCalculateError(
        f"商品“{product_name}”的“计入均摊”必须是 True 或 False，实际值：{value!r}"
    )


def parse_optional_positive_int(value: Any, default: int | None = None) -> int | None:
    """
    解析可选正整数。

    用于：
        商品序号
        商品数量
    """
    if value is None or str(value).strip() == "":
        return default

    text = str(value).strip()

    if not text.isdigit():
        raise ShareCalculateError(f"应为正整数，实际值：{value!r}")

    number = int(text)

    if number <= 0:
        raise ShareCalculateError(f"应为正整数，实际值：{value!r}")

    return number