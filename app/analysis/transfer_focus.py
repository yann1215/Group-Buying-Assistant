"""转单核查的商品范围；用户描述只作为线索。"""
import re


def parse_focus_products(text):
    text = re.sub(r"(?:比对转单记录|分析转单记录|分析转单异常|查找转单异常|查转单记录|查转单异常)", "", text).strip(" ，,；;。")
    if re.search(r"(?:不要|不用|取消|暂不|别)\s*(?:特别)?关注", text):
        return None
    explicit = re.search(r"(?:特别关注(?:商品)?|关注(?:商品)?)\s*[:：]?\s*(.*)", text)
    if explicit:
        value = explicit[1].strip(" 。")
    elif re.search(r"数量(?:有误|异常)|(?:有人)?多吃了|缺少|缺了|少了", text):
        value = text
    else:
        return None
    if value in {"无", "没有", "无特别关注", "清空"}:
        return []
    if not value:
        raise ValueError("请填写特别关注的商品名称，例如“关注雪梅蜂”。")
    products = []
    for item in re.split(r"[，,；;\n]+", value):
        if not item.strip():
            continue
        parts = re.split(r"[:：]|(?=(?:有人)?多吃了|缺少|缺了|少了|数量有误|数量异常)", item.strip(), maxsplit=1)
        name = parts[0].strip(" 。“”\"'")
        if not name:
            raise ValueError("请填写特别关注的商品名称，例如“关注雪梅蜂”。")
        description = parts[1].strip() if len(parts) > 1 else "数量异常，方向未知"
        quantity = re.search(r"(?:缺少|缺了|少了)\s*(\d+)", description)
        products.append({"product_name": name, "description": description,
                         "missing_quantity": int(quantity[1]) if quantity else None})
    return products
