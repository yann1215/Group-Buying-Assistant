# app/analysis/order_validator.py

from __future__ import annotations

from typing import Any, Iterable
import csv
import re
from pathlib import Path


# 强制参摊关键词优先级高于不参摊关键词。
FORCE_INCLUDE_SHARE_KEYWORDS = (
    "本体",
)

EXCLUDE_SHARE_KEYWORDS = (
    "底胚",
    "专拍",
)


class OrderValidationError(RuntimeError):
    """订单校验失败。"""


def is_special_member_product(
    product_name: str,
) -> bool:
    """
    判断商品是否属于特殊成员身份商品。

    规则：
    1. 商品名称中存在“摊……套”
    2. 商品名称中包含“专拍”
    """
    name = str(product_name or "").strip()

    if not name:
        return False

    if re.search(r"摊.*套", name):
        return True

    if "专拍" in name:
        return True

    return False


def default_include_share(product_name: str) -> bool:
    """
    默认参摊规则。

    优先级：
    1. 商品名称包含不参摊关键词，例如“底胚” → 不参摊
    2. 商品名称包含强制参摊关键词，例如“本体” → 参摊
    3. 其他商品 → 参摊

    示例：
    - “普通底胚” → False
    - “普通特典” → True
    - “底胚特典” → False
    """

    name = str(product_name or "").strip()

    # 特殊成员商品固定不参摊
    if is_special_member_product(name):
        return False

    # 普通不参摊商品
    # 不参摊关键词优先级最高
    if any(keyword in name for keyword in EXCLUDE_SHARE_KEYWORDS):
        return False

    if any(keyword in name for keyword in FORCE_INCLUDE_SHARE_KEYWORDS):
        return True

    return True


def inspect_order_status(
    parsed_order_file: str | Path,
    product_configs: Iterable[Any],
) -> dict[str, list[dict[str, Any]]]:

    parsed_order_file = Path(parsed_order_file)

    include_share_map = _build_include_share_map(
        product_configs
    )

    special_product_orders = []
    only_non_share_orders = []

    with parsed_order_file.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as f:
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            raise OrderValidationError(
                "简化订单文件没有表头。"
            )

        product_fields = [
            field
            for field in reader.fieldnames
            if field not in {
                "单号",
                "昵称",
                "总金额",
            }
        ]

        for row in reader:
            order_no = str(
                row.get("单号") or ""
            ).strip()

            nickname = str(
                row.get("昵称") or ""
            ).strip()

            purchased_products = []

            for product_name in product_fields:
                quantity = _to_positive_int_or_zero(
                    row.get(product_name)
                )

                if quantity <= 0:
                    continue

                purchased_products.append(
                    {
                        "商品名称": product_name,
                        "数量": quantity,
                    }
                )

            # 空订单不在这里处理
            if not purchased_products:
                continue

            # =========================================
            # 第一优先级：特殊商品
            # =========================================

            special_products = [
                item
                for item in purchased_products
                if is_special_member_product(
                    item["商品名称"]
                )
            ]

            if special_products:
                special_product_orders.append(
                    {
                        "单号": order_no,
                        "昵称": nickname,
                        "特殊商品": special_products,
                    }
                )

                # 特殊商品订单不再进入下面的异常判断
                continue

            # =========================================
            # 第二优先级：普通商品是否存在参摊商品
            # =========================================

            has_share_product = False
            non_share_products = []

            for item in purchased_products:
                product_name = item["商品名称"]

                include_share = include_share_map.get(
                    product_name,
                    default_include_share(
                        product_name
                    ),
                )

                if include_share:
                    has_share_product = True
                    break

                non_share_products.append(item)

            # 至少有一个参摊商品 → 正常
            if has_share_product:
                continue

            # 没特殊商品，并且所有商品都不参摊
            only_non_share_orders.append(
                {
                    "单号": order_no,
                    "昵称": nickname,
                    "不参摊商品": non_share_products,
                }
            )

    return {
        "special_product_orders": (
            special_product_orders
        ),
        "only_non_share_orders": (
            only_non_share_orders
        ),
    }


def _build_include_share_map(
    product_configs: Iterable[Any],
) -> dict[str, bool]:
    result: dict[str, bool] = {}

    for config in product_configs:
        product_name = str(
            _get_value(
                config,
                attribute_name="product_name",
                dict_keys=("商品名称", "product_name"),
            )
            or ""
        ).strip()

        if not product_name:
            continue

        raw_include_share = _get_value(
            config,
            attribute_name="include_share",
            dict_keys=("计入均摊", "include_share"),
        )

        result[product_name] = _parse_include_share(
            value=raw_include_share,
            product_name=product_name,
        )

    return result


def _parse_include_share(
    value: Any,
    product_name: str,
) -> bool:
    if isinstance(value, bool):
        return value

    if value is None or str(value).strip() == "":
        return default_include_share(product_name)

    text = str(value).strip().lower()

    if text in {
        "true",
        "1",
        "是",
        "参摊",
        "参与",
        "计入",
    }:
        return True

    if text in {
        "false",
        "0",
        "否",
        "不参摊",
        "不参与",
        "不计入",
    }:
        return False

    raise OrderValidationError(
        f"商品“{product_name}”的“计入均摊”无法识别：{value!r}"
    )


def _get_value(
    item: Any,
    attribute_name: str,
    dict_keys: tuple[str, ...],
) -> Any:
    if isinstance(item, dict):
        for key in dict_keys:
            if key in item:
                return item.get(key)

        return None

    return getattr(item, attribute_name, None)


def _to_positive_int_or_zero(value: Any) -> int:
    if value is None:
        return 0

    text = str(value).strip()

    if text == "":
        return 0

    try:
        number = int(text)
    except (TypeError, ValueError):
        return 0

    return number if number > 0 else 0