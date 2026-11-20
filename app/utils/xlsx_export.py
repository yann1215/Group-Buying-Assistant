"""保存 XLSX，并显式启用填充色以兼容微信预览。"""
from io import BytesIO
from pathlib import Path
import re
from zipfile import ZipFile


def save_xlsx(workbook, path):
    raw = BytesIO()
    workbook.save(raw)
    result = BytesIO()
    with ZipFile(raw) as source, ZipFile(result, 'w') as target:
        for entry in source.infolist():
            data = source.read(entry.filename)
            if entry.filename == 'xl/styles.xml':
                def patch_styles(match):
                    def patch_xf(xf):
                        tag = xf[0]
                        fill = re.search(r'\bfillId="(\d+)"', tag)
                        if fill and int(fill[1]) and 'applyFill=' not in tag:
                            tag = tag.replace('<xf ', '<xf applyFill="1" ', 1)
                        return tag
                    return re.sub(r'<xf\b[^>]*>', patch_xf, match[0])
                data = re.sub(r'<cellXfs\b.*?</cellXfs>', patch_styles,
                              data.decode('utf-8'), flags=re.S).encode('utf-8')
            target.writestr(entry, data)
    Path(path).write_bytes(result.getvalue())
