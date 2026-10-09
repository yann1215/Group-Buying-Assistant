"""只读取来源订单，在独立临时目录解析并导出比较报告。"""
from __future__ import annotations
import csv
import hashlib
import tempfile
from collections import Counter
from pathlib import Path
from app.analysis.order_parser import parse_order_file
from app.core import path_manager as paths


def file_signature(value):
    with Path(value).open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_orders(old_file, new_file, group_name, expected_signatures=None, *, structured=False):
    signatures = [file_signature(value) for value in (old_file, new_file)]
    if expected_signatures is not None and signatures != expected_signatures:
        raise ValueError("订单文件发生变化，请重新确认")
    with tempfile.TemporaryDirectory() as directory:
        tables = []
        for index, source in enumerate((old_file, new_file)):
            parsed = parse_order_file(source, Path(directory) / f"{index}.csv")
            with Path(parsed).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                fields = reader.fieldnames or []
                rows = {}
                for row in reader:
                    key = row["单号"]
                    if key in rows:
                        raise ValueError(f"{Path(source).name} 存在重复单号 {key}，无法可靠比较")
                    rows[key] = row
                tables.append((fields, rows))
    (old_fields, old), (new_fields, new) = tables
    changes = []
    fields = [field for field in dict.fromkeys(old_fields + new_fields)
              if field not in {"单号", "昵称", "总金额"}]
    for key in sorted(set(old) | set(new), key=int):
        kind = "新增订单" if key not in old else "删除订单" if key not in new else "修改订单"
        old_row, new_row = old.get(key, {}), new.get(key, {})
        for field in fields:
            # 缺失的订单、商品列及空数量均按零计算。
            before = int(old_row.get(field) or "0")
            after = int(new_row.get(field) or "0")
            if before != after:
                changes.append((kind, key, new_row.get("昵称", ""), field, before, after, after - before))
    if [file_signature(value) for value in (old_file, new_file)] != signatures:
        raise ValueError("比较期间订单文件发生变化，请重新输入比较订单并确认")
    directory = paths.ORDERS_DIR / "comparisons"
    directory.mkdir(parents=True, exist_ok=True)
    name = "_".join(paths.sanitize_filename(value) for value in
                    (group_name, "订单比较", Path(old_file).stem, Path(new_file).stem))
    index = 1
    while True:
        target = directory / (name + (f"_{index}" if index > 1 else "") + ".csv")
        try:
            stream = target.open("x", encoding="utf-8-sig", newline="")
            break
        except FileExistsError:
            index += 1
    with stream:
        writer = csv.writer(stream)
        writer.writerow(["修改类型", "单号", "昵称", "变动商品", "旧值", "新值", "变化量"])
        writer.writerows(changes)
    counts = Counter(kind for kind, *_ in changes)
    summary = "；".join(f"{kind}：{len({row[1] for row in changes if row[0] == kind})} 单" if kind.endswith("订单") else f"{kind}：{count} 项"
                        for kind, count in counts.items()) or "未发现商品变化"
    message = f"订单比较完成。{summary}。\n报告：{target.resolve()}"
    result = {"values": [str(Path(value).resolve()) for value in (old_file, new_file)],
              "signatures": signatures, "group_name": group_name,
              "report_path": str(target.resolve()), "report_signature": file_signature(target),
              "summary": summary, "message": message}
    return result if structured else message
