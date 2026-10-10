"""python -m scripts.verify_transfer_extraction"""
import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.analysis.transfer_extraction import extract_records, parse_products, split_message, FILES
from app.core import chat_history_workflow as history


def message(no, body, wxid="a", kind="文本", **extra):
    return {"消息ID": str(no), "内容": body, "类型": kind, "wxid": wxid,
            "群昵称": wxid, "昵称": wxid, "时间": f"2026-10-10 10:{int(no):02d}:00", **extra}


PRODUCTS = [{"商品名称": "本体"}, {"商品名称": "特典"}]
IDENTITIES = [{"wxid": w, "serial": str(n), "wechat_name": w, "name": w}
              for w, n in (("a", 12), ("b", 23))]


class ExtractionChecks(unittest.TestCase):
    def test_strict_delimiters_and_nested_colons(self):
        self.assertEqual(split_message(message(1, "接\n引用：a: 转本体给@23: 备注", kind="引用消息"))[:3],
                         ("接\n", "a", "转本体给@23: 备注"))
        for text in ("接引用:a: 转本体给@23", "接引用：a： 转本体给@23", "接引用：a:转本体给@23"):
            self.assertTrue(split_message(message(1, text, kind="引用消息"))[3])

    def test_link_and_json_products(self):
        rows = [message(1, "转本体2、特典3给@23"),
                message(2, "接\n引用：a: 转本体2、特典3给@23", "b", "引用消息")]
        records, starts, ends = extract_records(rows, PRODUCTS, IDENTITIES)
        self.assertEqual((len(records), starts, ends), (1, [], []))
        record = records[0]
        self.assertEqual(record["发起消息ID"], "1")
        self.assertEqual(record["接收消息ID"], "2")
        self.assertEqual(record["确认状态"], "规则匹配完整")
        self.assertEqual([p["数量"] for p in json.loads(record["商品转移"])], [2, 3])

    def test_defaults_and_whole_order(self):
        scope, items, notes = parse_products("转单本体给@23", PRODUCTS)
        self.assertEqual(items[0]["数量"], 1)
        self.assertEqual(items[0]["数量来源"], "默认")
        self.assertTrue(notes)
        self.assertEqual(parse_products("转蒸蛋给@23", PRODUCTS), ("整单", [], []))
        self.assertEqual(parse_products("转整单除特典给@23", PRODUCTS)[0], "未知")

    def test_quantity_variants_and_overlapping_product_names(self):
        for text, expected in (("转本体两份给@23", 2), ("转2个本体给@23", 2), ("转本体x3给@23", 3)):
            self.assertEqual(parse_products(text, PRODUCTS)[1][0]["数量"], expected)
        items = parse_products("转本体特典2给@23", [*PRODUCTS, {"商品名称": "本体特典"}])[1]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["商品名称"], "本体特典")
        self.assertIsNone(parse_products("转本体1-2给@23", PRODUCTS)[1][0]["数量"])
        self.assertEqual(parse_products("转商品123给@23", [{"商品名称": "商品123"}])[1][0]["商品名称"], "商品123")
        self.assertEqual([p["数量"] for p in parse_products("转2个本体、3个特典给@23", PRODUCTS)[1]], [2, 3])

    def test_mapping_does_not_merge_different_people(self):
        rows = [message(1, "接引用：重名: 转本体给@23", "b", "引用消息")]
        identities = [{"wxid": "c", "wechat_name": "重名", "serial": "1"},
                      {"wxid": "d", "wechat_name": "重名", "serial": "2"}]
        record = extract_records(rows, PRODUCTS, identities)[0][0]
        self.assertEqual(record["发起人wxid"], "")
        self.assertEqual(record["发起人微信昵称"], "")
        self.assertEqual(record["确认状态"], "存在冲突")

    def test_publish_rollback(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            files = []
            for i in range(5):
                source, dest = root / f"source{i}", root / f"dest{i}"
                source.write_text("new")
                dest.write_text("old")
                files.append((source, dest))
            original = history.os.replace
            def replace(source, dest):
                if Path(source).name == "source3":
                    raise OSError("simulated publication failure")
                return original(source, dest)
            with patch.object(history.os, "replace", side_effect=replace):
                with self.assertRaises(OSError):
                    history.publish_history_files(files, root)
            self.assertTrue(all(dest.read_text() == "old" for _, dest in files))

    def test_candidates_and_exclusions(self):
        rows = [message(1, "分本体给@23"), message(2, "接下来"), message(3, "接下来我接")]
        records, starts, ends = extract_records(rows, PRODUCTS)
        self.assertEqual((len(records), len(starts), len(ends)), (0, 1, 1))
        self.assertEqual(ends[0]["消息ID"], "3")

    def test_duplicate_text_not_arbitrarily_linked(self):
        rows = [message(1, "转本体给@23"), message(2, "转本体给@23"),
                message(3, "接引用：a: 转本体给@23", "b", "引用消息")]
        records, _, _ = extract_records(rows, PRODUCTS)
        self.assertEqual(records[0]["发起消息ID"], "")
        self.assertIn("多个精准匹配", records[0]["问题备注"])

    def test_repeated_receipts_and_negative(self):
        rows = [message(1, "转本体2给@23"),
                message(2, "接引用：a: 转本体2给@23", "b", "引用消息"),
                message(3, "不接引用：a: 转本体2给@23", "b", "引用消息")]
        records, _, _ = extract_records(rows + [rows[1]], PRODUCTS, IDENTITIES)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["确认状态"] == "待核实" for r in records))
        self.assertIn("否定", records[1]["问题备注"])

    def test_quoted_initiation(self):
        rows = [message(1, "转本体2给@23\n引用：c: 之前的话", kind="引用消息"),
                message(2, "接引用：a: 转本体2给@23", "b", "引用消息")]
        self.assertEqual(extract_records(rows, PRODUCTS, IDENTITIES)[0][0]["发起消息ID"], "1")

    def test_ambiguous_product_and_identity(self):
        _, items, notes = parse_products("转特典给@23", [{"商品名称": "甲特典"}, {"商品名称": "乙特典"}])
        self.assertIsNone(items[0]["商品名称"])
        self.assertIn("多个候选", "".join(notes))
        rows = [message(1, "转本体2给@23"), message(2, "接引用：a: 转本体2给@23", "b", "引用消息")]
        records, _, _ = extract_records(rows, PRODUCTS, [*IDENTITIES, dict(IDENTITIES[1], serial="24")])
        self.assertEqual(records[0]["确认状态"], "存在冲突")
        self.assertEqual(records[0]["接收人单号"], "")

    def test_chat_command_integration_and_empty_rerun(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            ctx = SimpleNamespace(session_id=1, group_name="测试", chat_history_period=history.DEFAULT_HISTORY_PERIOD)
            rows = [message(1, "转本体给@23"), message(2, "接引用：a: 转本体给@23", "b", "引用消息")]
            def export(**kwargs):
                folder = Path(kwargs["output_dir"]) / "测试(room@chatroom)"
                folder.mkdir()
                path = folder / "raw.csv"
                with path.open("w", encoding="utf-8-sig", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=list(message(1, "")))
                    writer.writeheader()
                    writer.writerows(rows)
                return {"ok": True, "csv_path": str(path)}
            with patch.object(history, "get_workspace_dir", return_value=root), \
                 patch.object(history, "get_chat_history_path", side_effect=lambda *a, **k: root / ("filtered.csv" if k.get("filtered", True) else "raw.csv")), \
                 patch.object(history, "get_wechat_group_messages", side_effect=export), \
                 patch("app.core.transfer_extraction_workflow.mapping_path", return_value=root / "missing.json"):
                result = history.handle_chat_history(SimpleNamespace(key_input_func=None), ctx, {})
                self.assertIn("转单提取完成", result)
                for name in FILES:
                    self.assertTrue((root / name).is_file())
                with (root / "filtered.csv").open(encoding="utf-8-sig") as file:
                    self.assertIn("消息ID", next(csv.reader(file)))
                with (root / FILES[0]).open(encoding="utf-8-sig", newline="") as file:
                    saved = list(csv.DictReader(file))
                self.assertEqual(json.loads(saved[0]["商品转移"])[0]["数量来源"], "默认")
                rows.clear()
                history.handle_chat_history(SimpleNamespace(key_input_func=None), ctx, {})
                with (root / FILES[0]).open(encoding="utf-8-sig") as file:
                    self.assertEqual(list(csv.DictReader(file)), [])


if __name__ == "__main__":
    unittest.main()
