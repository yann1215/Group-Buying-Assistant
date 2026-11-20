"""三种应收结果共用的 Excel 样式与布局。"""
from app.utils.xlsx_export import save_xlsx
from copy import copy
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Color, PatternFill
from openpyxl.utils import get_column_letter

VISIBLE_FIELDS = {"单号", "昵称", "均摊金额", "大货金额", "总金额"}
MONEY_FIELDS = {"均摊金额", "大货金额", "总金额"}


def write_receivable_xlsx(path, fields, rows, highlighted_amount):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "收款明细"
    # 使用不透明的固定 RGB，避免预览器依赖自定义主题和 tint 解析。
    header_fill = PatternFill("solid", fgColor="FFFFC000")
    serial_fill = PatternFill("solid", fgColor="FFF2F2F2")
    amount_fill = PatternFill("solid", fgColor="FFFFF2CC")
    body_font = copy(sheet.cell(1, 2).font)
    body_font.color = Color(rgb="FF000000")
    sheet.column_dimensions["A"].width = 12
    centered = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 16
    for col, field in enumerate(fields, start=2):
        cell = sheet.cell(1, col, field)
        cell.data_type = "s"
        cell.fill = header_fill
        cell.alignment = centered
        header_font = copy(body_font)
        header_font.bold = True
        cell.font = header_font
        dimension = sheet.column_dimensions[get_column_letter(col)]
        dimension.width = 12
        dimension.hidden = field not in VISIBLE_FIELDS
    for row_no, row in enumerate(rows, start=2):
        sheet.row_dimensions[row_no].height = 16
        for col, field in enumerate(fields, start=2):
            value = row.get(field, "")
            if field in MONEY_FIELDS and value not in (None, ""):
                value = Decimal(str(value))
            cell = sheet.cell(row_no, col, value)
            cell.alignment = centered
            cell.font = body_font
            if isinstance(value, str):
                cell.data_type = "s"
            if field in MONEY_FIELDS:
                cell.number_format = "0.00"
            if field == "单号":
                cell.fill = serial_fill
            elif field == highlighted_amount:
                cell.fill = amount_fill
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    save_xlsx(workbook, path)
    workbook.close()
