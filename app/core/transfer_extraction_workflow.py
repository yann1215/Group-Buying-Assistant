"""为规则提取准备当前会话资料；结果由聊天工作流统一发布。"""
from pathlib import Path

from app.analysis.transfer_extraction import FILES, write_extraction, serial_from_nickname
from app.core.order_identity_cache import load_mapping, mapping_path
from app.utils.csv_utils import read_csv_dict_rows


def prepare_transfer_extraction(ctx, chat_path, staging):
    warnings = []
    products = []
    config = getattr(ctx, "share_config_file", None)
    if config and Path(config).is_file():
        from app.analysis.product_config import owns_product_config
        if owns_product_config(config, getattr(ctx, "config_owner_id", "")):
            products, fields = read_csv_dict_rows(config)
            if "商品名称" not in fields:
                raise ValueError("商品配置缺少商品名称列")
        else:
            warnings.append("商品配置不属于当前会话，商品名称待核实")
    else:
        warnings.append("未找到商品配置，商品名称待核实")
    identities = [entry for entry in load_mapping(mapping_path(ctx.session_id))
                  if entry.get("group") == ctx.group_name
                  and str(entry.get("source_session_id")) == str(ctx.session_id)]
    # 当前会话已确认的成员资料也可提供 wxid 与订单昵称。
    for member in getattr(ctx, "special_members", ()):
        if member.get("wxid"):
            identities.append({"wxid": member["wxid"], "serial": member.get("单号", ""),
                               "wechat_name": member.get("昵称", ""), "name": member.get("订单昵称", "")})
    # 聊天发送者提供 wxid-昵称证据；当前已解析订单按单号补充订单昵称。
    orders = []
    order_path = getattr(ctx, "parsed_order_file", None)
    if order_path and Path(order_path).is_file():
        orders, _ = read_csv_dict_rows(order_path)
    for row in read_csv_dict_rows(chat_path)[0]:
        if not row.get("wxid"):
            continue
        serial = serial_from_nickname(row.get("群昵称", ""))
        names = {o.get("昵称", "") for o in orders if serial and str(o.get("单号")) == serial} - {""}
        for name in names or {""}:
            entry = {"wxid": row["wxid"], "serial": serial, "wechat_name": row.get("昵称", ""),
                     "group_name": row.get("群昵称", ""), "name": name}
            if entry not in identities:
                identities.append(entry)
    if not identities:
        warnings.append("未找到当前群的身份映射，未识别的人员信息留空")
    counts = write_extraction(chat_path, staging, products=products, identities=identities)
    return {"files": list(FILES), "counts": counts, "warnings": warnings}
