"""逐单检查原始订单的收货信息，不推断邮费或发货状态。"""
from openpyxl import load_workbook
from app.analysis import order_parser as parser


def check_shipping_information(path):
    headers = ("收货人姓名", "收货人联系方式", "收货人地址")
    wb = load_workbook(path, data_only=True)
    try:
        ws = wb.worksheets[parser.SHEET_INDEX]
        merged = parser._build_merged_value_map(ws)
        columns = {}
        for header in headers:
            try:
                columns[header] = parser._find_required_header_col(ws, header, merged)
            except parser.OrderParseError:
                columns[header] = None
        issues, total = [], 0
        for row in range(parser.HEADER_ROW + 1, ws.max_row + 1):
            if parser._is_empty_data_row(ws, row, ws.max_column, merged):
                continue
            serial = parser._parse_positive_int_required(
                parser._get_cell_value(ws, row, parser.ORDER_NO_COL, merged), "单号", row)
            name = parser._get_cell_value(ws, row, parser.NICKNAME_COL, merged)
            total += 1
            missing = [header for header, col in columns.items() if col is None or
                       not str(parser._get_cell_value(ws, row, col, merged) or "").strip()]
            if missing:
                issues.append(f"单号{serial}（{name or '昵称为空'}）：缺少{'、'.join(missing)}")
        lines = [f"收货信息：共{total}单，完整{total - len(issues)}单，缺失{len(issues)}单。"]
        lines.extend(issues)
        if not total:
            lines.append("当前订单没有可检查的订单记录。")
        lines.append("本次仅检查收货信息是否填写，不计算邮费或判断是否已发货。")
        return "\n".join(lines)
    finally:
        wb.close()
