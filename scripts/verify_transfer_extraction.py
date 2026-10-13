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
from app.core.transfer_extraction_workflow import prepare_transfer_extraction


def history_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


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

    def test_segmented_product_abbreviations(self):
        products = [{"商品名称": "大黄喵 镀金底胚"}, {"商品名称": "大黄喵 本体"}]
        for alias in ("大黄喵底胚", "大黄底胚", "大黄 底胚"):
            with self.subTest(alias=alias):
                _, items, notes = parse_products(f"转{alias}给@23", products)
                self.assertEqual(items[0]["商品名称"], "大黄喵 镀金底胚")
                self.assertEqual(items[0]["商品原文"], alias)
                self.assertEqual((items[0]["数量"], items[0]["数量来源"]), (1, "默认"))
                self.assertIn("未注明数量", "".join(notes))
        self.assertEqual(parse_products("转大黄底胚2给@23", products)[1][0]["数量"], 2)

    def test_segmented_abbreviations_require_unique_candidates(self):
        products = [{"商品名称": "大黄喵 镀金底胚"}, {"商品名称": "大黄喵 银色底胚"}]
        _, items, notes = parse_products("转大黄底胚2给@23", products)
        self.assertIsNone(items[0]["商品名称"])
        self.assertIn("匹配多个候选：大黄喵 镀金底胚、大黄喵 银色底胚", "".join(notes))
        for alias in ("黄底胚", "大喵底胚", "大黄底", "大黄特典"):
            with self.subTest(alias=alias):
                self.assertIsNone(parse_products(f"转{alias}给@23", products)[1][0]["商品名称"])
        exact = [*products, {"商品名称": "大黄底胚"}]
        self.assertEqual(parse_products("转大黄底胚2给@23", exact)[1][0]["商品名称"], "大黄底胚")

    def test_bare_prefix_and_suffix_quantities(self):
        products = [{"商品名称": "嘴努子"}]
        for expression, count in (("1嘴努子", 1), ("嘴努子1", 1), ("2嘴努子", 2),
                                  ("嘴努子2", 2), ("两嘴努子", 2), ("2个嘴努子", 2)):
            with self.subTest(expression=expression):
                _, items, notes = parse_products(f"转{expression}给@23", products)
                self.assertEqual(items[0]["商品名称"], "嘴努子")
                self.assertEqual((items[0]["数量"], items[0]["数量来源"]), (count, "明确"))
                self.assertEqual(notes, [])
        for expression in ("0嘴努子", "嘴努子0", "嘴努子1-2"):
            self.assertIsNone(parse_products(f"转{expression}给@23", products)[1][0]["数量"])
        _, items, notes = parse_products("转1嘴努子、2大黄底胚给@23", [
            *products, {"商品名称": "大黄喵 镀金底胚"}])
        self.assertEqual([item["商品名称"] for item in items], ["嘴努子", "大黄喵 镀金底胚"])
        self.assertEqual([item["数量"] for item in items], [1, 2])
        self.assertEqual(notes, [])

    def test_exact_numeric_product_names_take_priority(self):
        products = [{"商品名称": name} for name in ("嘴努子", "1嘴努子", "嘴努子1", "商品123", "大黄喵 镀金底胚")]
        for expression, name in (("1嘴努子", "1嘴努子"), ("嘴努子1", "嘴努子1"),
                                 ("商品123", "商品123"), ("大黄喵镀金底胚", "大黄喵 镀金底胚")):
            with self.subTest(expression=expression):
                item = parse_products(f"转{expression}给@23", products)[1][0]
                self.assertEqual(item["商品名称"], name)
                self.assertEqual((item["数量"], item["数量来源"]), (1, "默认"))

    def test_abbreviations_in_linked_transfer_and_candidate(self):
        products = [{"商品名称": "大黄喵 镀金底胚"}, {"商品名称": "嘴努子"}]
        body = "转大黄底胚2、1嘴努子给@23"
        rows = [message(1, body), message(2, "接\n引用：a: " + body, "b", "引用消息")]
        records, starts, ends = extract_records(rows, products, IDENTITIES)
        self.assertEqual((len(records), starts, ends), (1, [], []))
        self.assertEqual(records[0]["确认状态"], "规则匹配完整")
        self.assertEqual([item["商品名称"] for item in json.loads(records[0]["商品转移"])],
                         ["大黄喵 镀金底胚", "嘴努子"])
        candidates = extract_records(rows[:1], products, IDENTITIES)[1]
        self.assertEqual([item["数量"] for item in json.loads(candidates[0]["商品转移"])], [2, 1])

    def test_mapping_does_not_merge_different_people(self):
        rows = [message(1, "接引用：重名: 转本体给@23", "b", "引用消息")]
        identities = [{"wxid": "c", "wechat_name": "重名", "serial": "1"},
                      {"wxid": "d", "wechat_name": "重名", "serial": "2"}]
        record = extract_records(rows, PRODUCTS, identities)[0][0]
        self.assertEqual(record["发起人wxid"], "")
        self.assertEqual(record["发起人微信昵称"], "")
        self.assertEqual(record["确认状态"], "存在冲突")

    def test_order_nicknames_from_source_mapping(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            local = root / "1"
            merged = root / "2"
            local.mkdir()
            merged.mkdir()
            entries = [dict(entry, group="测试", source_session_id="1", name="订单" + entry["wxid"])
                       for entry in IDENTITIES]
            # 其他群、其他来源以及未绑定单车的映射不能污染当前会话。
            entries.extend([dict(entries[0], group="其他群", name="错误群"),
                            dict(entries[0], source_session_id=3, name="错误来源"),
                            dict(entries[0], source_session_id="merge:2:测试", name="未绑定来源")])
            mapping = merged / "order_identity_mapping.json"
            mapping.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
            body = "转本体2给@23"
            rows = [message(1, body, 订单昵称="  "),
                    message(2, "接\n引用：a: " + body, "b", "引用消息")]
            chat = local / "chat.csv"
            fields = list(dict.fromkeys(key for row in rows for key in row))
            with chat.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            ctx = SimpleNamespace(session_id=1, group_name="测试")
            with patch("app.core.order_identity_cache.mapping_path", return_value=local / mapping.name):
                prepare_transfer_extraction(ctx, chat, local)
                records = history_rows(local / FILES[0])
                self.assertEqual(records[0]["发起人订单昵称"], "订单a")
                self.assertEqual(records[0]["接收人订单昵称"], "订单b")
                # 候选发起也要补齐同一昵称。
                with chat.open("w", encoding="utf-8-sig", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow(rows[0])
                prepare_transfer_extraction(ctx, chat, local)
                self.assertEqual(history_rows(local / FILES[1])[0]["订单昵称"], "订单a")
                # 多份映射中的订单昵称矛盾时不得任意补齐。
                entries.append(dict(entries[0], name="冲突昵称"))
                mapping.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")
                prepare_transfer_extraction(ctx, chat, local)
                candidate = history_rows(local / FILES[1])[0]
                self.assertEqual(candidate["订单昵称"], "")
                self.assertIn("身份映射冲突：订单昵称", candidate["问题备注"])

    def test_mapping_serial_fills_nickname_without_chat_serial(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            orders.write_text("单号,昵称\n12,订单a\n23,订单b\n", encoding="utf-8-sig")
            rows = [message(1, "转本体2给@23"),
                    message(2, "接\n引用：a: 转本体2给@23", "b", "引用消息")]
            chat = root / "chat.csv"
            with chat.open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            ctx = SimpleNamespace(session_id=1, group_name="测试", parsed_order_file=orders)
            identities = [dict(entry, name="") for entry in IDENTITIES]
            with patch("app.core.transfer_extraction_workflow.load_source_mapping", return_value=identities):
                prepare_transfer_extraction(ctx, chat, root)
            record = history_rows(root / FILES[0])[0]
            self.assertEqual(record["发起人订单昵称"], "订单a")
            self.assertEqual(record["接收人订单昵称"], "订单b")

    def test_existing_order_nickname_is_preserved(self):
        body = "转本体2给@23"
        rows = [message(1, body, 订单昵称="已有订单昵称"),
                message(2, "接\n引用：a: " + body, "b", "引用消息")]
        record = extract_records(rows, PRODUCTS, IDENTITIES)[0][0]
        self.assertEqual(record["发起人订单昵称"], "已有订单昵称")
        self.assertIn("身份映射冲突：订单昵称", record["问题备注"])
        # 缺少发起消息时，仍可按唯一引用对象从映射补齐。
        record = extract_records(rows[1:], PRODUCTS, IDENTITIES)[0][0]
        self.assertEqual(record["发起人订单昵称"], "a")
        self.assertEqual(record["接收人订单昵称"], "b")

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
                 patch("app.core.order_identity_cache.mapping_path", return_value=root / "missing.json"):
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
