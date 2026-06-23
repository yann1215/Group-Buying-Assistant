# app/analysis/order_validator.py

from __future__ import annotations

from typing import Any, Iterable


# 强制参摊关键词优先级高于不参摊关键词。
FORCE_INCLUDE_SHARE_KEYWORDS = (
    "特典",
)

EXCLUDE_SHARE_KEYWORDS = (
    "底胚",
)


class OrderValidationError(RuntimeError):
    """订单校验失败。"""


def default_include_share(product_name: str) -> bool:
    """
    默认参摊规则。

    优先级：
    1. 商品名称包含不参摊关键词，例如“底胚” → 不参摊
    2. 商品名称包含强制参摊关键词，例如“特典” → 参摊
    3. 其他商品 → 参摊

    示例：
    - “普通底胚” → False
    - “普通特典” → True
    - “底胚特典” → False
    """
    name = str(product_name or "").strip()

    # 不参摊关键词优先级最高
    if any(keyword in name for keyword in EXCLUDE_SHARE_KEYWORDS):
        return False

    if any(keyword in name for keyword in FORCE_INCLUDE_SHARE_KEYWORDS):
        return True

    return True


def find_orders_with_only_non_share_products(
    order_rows: Iterable[Any],
    product_configs: Iterable[Any],
) -> list[dict[str, Any]]:
    """
    查找只购买了不参摊商品的订单。

    order_rows 支持：
    - share_calculator.OrderRow
    - dict

    product_configs 支持：
    - share_calculator.ProductShareConfig
    - 配置表读取出的 dict

    返回示例：
    [
        {
            "单号": "12",
            "昵称": "用户A",
            "不参摊商品": [
                {
                    "商品名称": "款式A底胚",
                    "数量": 2,
                }
            ],
        }
    ]
    """
    include_share_map = _build_include_share_map(product_configs)

    abnormal_orders: list[dict[str, Any]] = []

    for row in order_rows:
        order_no = str(
            _get_value(
                row,
                attribute_name="order_no",
                dict_keys=("单号", "order_no"),
            )
            or ""
        ).strip()

        nickname = str(
            _get_value(
                row,
                attribute_name="nickname",
                dict_keys=("昵称", "nickname"),
            )
            or ""
        ).strip()

        quantities = _get_quantities(row)

        purchased_products: list[tuple[str, int]] = []

        for product_name, raw_quantity in quantities.items():
            quantity = _to_positive_int_or_zero(raw_quantity)

            if quantity <= 0:
                continue

            name = str(product_name or "").strip()
            if not name:
                continue

            purchased_products.append((name, quantity))

        # 没有商品的空订单不在这里处理。
        if not purchased_products:
            continue

        has_share_product = False
        non_share_products: list[dict[str, Any]] = []

        for product_name, quantity in purchased_products:
            include_share = include_share_map.get(
                product_name,
                default_include_share(product_name),
            )

            if include_share:
                has_share_product = True
                break

            non_share_products.append(
                {
                    "商品名称": product_name,
                    "数量": quantity,
                }
            )

        if has_share_product:
            continue

        abnormal_orders.append(
            {
                "单号": order_no,
                "昵称": nickname,
                "不参摊商品": non_share_products,
            }
        )

    return abnormal_orders


def format_only_non_share_orders_message(
    abnormal_orders: list[dict[str, Any]],
    operation_name: str,
) -> str:
    """
    格式化异常提示。

    operation_name 示例：
    - 个数摊计算
    - 收大货
    """
    if not abnormal_orders:
        return ""

    order_nos = [
        str(item.get("单号") or "").strip()
        for item in abnormal_orders
        if str(item.get("单号") or "").strip()
    ]

    lines: list[str] = []

    lines.append(
        f"订单异常：检测到仅包含不参摊商品的订单，已停止{operation_name}。"
    )
    lines.append(
        "当前检索规则：底胚不参摊，特典参摊；"
        "当商品名称同时包含底胚和特典时，按不参摊处理。"
    )
    lines.append(f"异常订单号：{'、'.join(order_nos)}")

    lines.append("")
    lines.append("异常订单明细：")

    for item in abnormal_orders:
        product_texts = []

        for product in item.get("不参摊商品") or []:
            product_name = product.get("商品名称") or ""
            quantity = product.get("数量") or 0
            product_texts.append(f"{product_name} × {quantity}")

        details = "；".join(product_texts)

        lines.append(
            f"- {item.get('单号')}｜"
            f"{item.get('昵称') or '未填写昵称'}｜"
            f"{details}"
        )

    lines.append("")
    lines.append("请先核对这些订单是否漏选了本体商品。")

    return "\n".join(lines)


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


def _get_quantities(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        for key in (
            "quantities",
            "商品明细",
            "商品数量明细",
        ):
            value = row.get(key)
            if isinstance(value, dict):
                return value

        return {}

    value = getattr(row, "quantities", None)

    if isinstance(value, dict):
        return value

    return {}


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