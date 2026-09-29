from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


CSV_ENCODINGS = (
    "utf-8-sig",
    "gb18030",
)


class CsvEncodingError(RuntimeError):
    pass


def read_csv_dict_rows(
    file_path: str | Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    读取 CSV。

    编码顺序：
    1. UTF-8 / UTF-8 BOM
    2. GB18030（兼容常见 Windows GBK/ANSI 中文 CSV）

    返回：
        rows
        fieldnames
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(
            f"CSV 文件不存在：{file_path}"
        )

    last_error: UnicodeDecodeError | None = None

    for encoding in CSV_ENCODINGS:
        try:
            with file_path.open(
                "r",
                encoding=encoding,
                newline="",
            ) as f:
                reader = csv.DictReader(f)

                fieldnames = list(
                    reader.fieldnames or []
                )

                rows = list(reader)

                return rows, fieldnames

        except UnicodeDecodeError as exc:
            last_error = exc

    raise CsvEncodingError(
        f"CSV 文件编码无法识别：{file_path}\n"
        "支持 UTF-8、UTF-8 BOM、GBK/GB18030。"
    ) from last_error