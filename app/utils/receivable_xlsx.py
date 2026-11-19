"""三种应收结果共用的 Excel 样式与布局。"""
from copy import copy
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl import Workbook
from openpyxl.styles import Alignment, Color, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.writer.theme import theme_xml

VISIBLE_FIELDS = {"单号", "昵称", "均摊金额", "大货金额", "总金额"}
MONEY_FIELDS = {"均摊金额", "大货金额", "总金额"}


def write_receivable_xlsx(path, fields, rows, highlighted_amount):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "收款明细"
    # 固定主题中的着色 3 为用户指定的金色，使用主题色及 tint 保存深浅关系。
    theme = ET.fromstring(theme_xml)
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    accent = theme.find("a:themeElements/a:clrScheme/a:accent3/a:srgbClr", ns)
    accent.set("val", "FFC000")
    workbook.loaded_theme = ET.tostring(theme, encoding="utf-8", xml_declaration=True)
    header_fill = PatternFill("solid", fgColor=Color(theme=6))
    serial_fill = PatternFill("solid", fgColor=Color(theme=0, tint=-0.05))
    amount_fill = PatternFill("solid", fgColor=Color(theme=6, tint=0.8))
    sheet.column_dimensions["A"].width = 12
    sheet.column_dimensions["B"].width = 12
    centered = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 16
    for col, field in enumerate(fields, start=3):
        cell = sheet.cell(1, col, field)
        cell.data_type = "s"
        cell.fill = header_fill
        cell.alignment = centered
        header_font = copy(cell.font)
        header_font.bold = True
        cell.font = header_font
        dimension = sheet.column_dimensions[get_column_letter(col)]
        dimension.width = 12
        dimension.hidden = field not in VISIBLE_FIELDS
    for row_no, row in enumerate(rows, start=2):
        sheet.row_dimensions[row_no].height = 16
        for col, field in enumerate(fields, start=3):
            value = row.get(field, "")
            if field in MONEY_FIELDS and value not in (None, ""):
                value = Decimal(str(value))
            cell = sheet.cell(row_no, col, value)
            cell.alignment = centered
            if isinstance(value, str):
                cell.data_type = "s"
            if field in MONEY_FIELDS:
                cell.number_format = "0.00"
            if field == "单号":
                cell.fill = serial_fill
            elif field == highlighted_amount:
                cell.fill = amount_fill
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    workbook.close()
