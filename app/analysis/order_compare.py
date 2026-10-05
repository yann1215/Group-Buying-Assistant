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


def compare_orders(old_file, new_file, group_name, expected_signatures=None):
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
    fields = list(dict.fromkeys(old_fields + new_fields))
    for key in sorted(set(old) | set(new), key=int):
        if key not in old or key not in new:
            kind = "新增订单" if key in new else "删除订单"
            row = new.get(key) or old[key]
            for field in fields:
                if field != "单号" and row.get(field, ""):
                    changes.append((kind, key, field, old.get(key, {}).get(field, ""), new.get(key, {}).get(field, "")))
        else:
            for field in fields:
                if field == "单号":
                    continue
                before, after = old[key].get(field, ""), new[key].get(field, "")
                # 缺失的商品列代表该商品数量为零。
                if field not in {"昵称", "总金额"}:
                    before, after = before or "0", after or "0"
                if before != after:
                    changes.append(("修改订单", key, field, before, after))
    # 即使商品全为零，新增或删除商品列也需要记录。
    for field in fields:
        if field not in old_fields:
            changes.append(("新增商品", "", "商品名称", "", field))
        elif field not in new_fields:
            changes.append(("删除商品", "", "商品名称", field, ""))
    if expected_signatures is not None and [file_signature(value) for value in (old_file, new_file)] != expected_signatures:
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
        writer.writerow(["群聊名称", "旧订单", "新订单", "比较方向", "差异类型", "单号", "变化字段", "旧值", "新值"])
        for change in changes or [("无差异", "", "", "", "")]:
            writer.writerow([group_name, str(old_file), str(new_file), "旧订单 → 新订单", *change])
    counts = Counter(kind for kind, *_ in changes)
    summary = "；".join(f"{kind}：{len({row[1] for row in changes if row[0] == kind})} 单" if kind.endswith("订单") else f"{kind}：{count} 项"
                        for kind, count in counts.items()) or "未发现变化"
    return f"订单比较完成。{summary}。\n报告：{target.resolve()}"
