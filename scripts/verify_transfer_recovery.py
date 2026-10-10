"""离线回归：漏答补查、证据校验、CPU 两阶段线程限制。"""
import sys
import unittest
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import LLM_THREADS
from app.llm.llama_client import LlamaClient, OutputLimitError
from app.llm.transfer_batches import BatchRunner
from app.llm.schemas.transfer import TransferResult
from app.llm.transfer_analyzer import TransferAnalyzer, bounded_batches, check_refs


def review(*refs):
    return TransferResult(stage="review", events=[], limitations=[], findings=[
        {"status": "unresolved", "summary": ref, "order_refs": [ref],
         "message_refs": [], "knowledge_refs": ["K001"]} for ref in refs])


def example():
    payload = {"knowledge": {"entries": [{"id": "K1", "text": "规则"}], "sha256": "test"},
               "focus_products": [], "chat_coverage": "测试", "chat_start": "start", "chat_end": "end",
               "orders": {"old": [{"单号": "1", "昵称": "甲", "商品A": "2"},
                                  {"单号": "2", "昵称": "乙", "商品A": "2"},
                                  {"单号": "3", "昵称": "丙", "商品A": "0"}],
                          "new": [{"单号": "1", "昵称": "甲", "商品A": "1"},
                                  {"单号": "2", "昵称": "乙", "商品A": "3"},
                                  {"单号": "3", "昵称": "丙", "商品A": "0"}]},
               "messages": [{"id": "M1", "群昵称": "1甲", "内容": "转商品A 1件给@2乙"},
                            {"id": "M2", "群昵称": "2乙", "内容": "接"}],
               "differences": [{"id": "D1", "单号": "1", "变化字段": "商品A"},
                               {"id": "D2", "单号": "2", "变化字段": "商品A"}]}
    event = {"sender_order": "1", "receiver_order": "2", "product": "商品A", "quantity": 1,
             "state": "confirmed", "message_refs": ["M1", "M2"], "knowledge_refs": ["K1"], "explanation": "转出与接收"}
    return payload, event


def example_client(events, fail_extract=False):
    client = Mock()

    def complete(prompt, context, schema, **kwargs):
        if context["stage"] == "extract":
            if fail_extract:
                raise OutputLimitError("length")
            return TransferResult(stage="extract", events=events, findings=[], limitations=[])
        return TransferResult(stage="review", events=[], limitations=[], findings=[
            {"status": "unresolved", "summary": "证据不足", "order_refs": [r["id"]],
             "message_refs": [], "knowledge_refs": ["K1"]} for r in context["differences"]])

    client.structured.side_effect = complete
    return client


class RecoveryTests(unittest.TestCase):
    def test_wrong_recipient_with_same_total_is_not_quantity_matched(self):
        payload, event = example()
        payload["orders"]["new"][1]["商品A"] = "2"
        payload["orders"]["new"][2]["商品A"] = "1"
        payload["differences"][1]["单号"] = "3"
        analyzer = TransferAnalyzer(example_client([event]))
        analyzer.analyze(payload)
        result = analyzer.last_result
        wrong = next(f for f in result["findings"] if "D2" in f["order_refs"])
        self.assertEqual(wrong["quantity_status"], "different")
        self.assertEqual(wrong["status"], "unresolved")
        self.assertTrue(any("单号2" in value for value in result["limitations"]))

    def test_netting_does_not_verify_relationships(self):
        payload, event = example()
        payload["orders"]["new"] = deepcopy(payload["orders"]["old"])
        payload["differences"] = []
        payload["messages"].extend([{"id": "M3", "群昵称": "2乙", "内容": "转商品A 1件给@1甲"},
                                    {"id": "M4", "群昵称": "1甲", "内容": "接"}])
        second = {**event, "sender_order": "2", "receiver_order": "1", "message_refs": ["M3", "M4"]}
        analyzer = TransferAnalyzer(example_client([event, second]))
        analyzer.analyze(payload)
        self.assertEqual(len(analyzer.last_result["events"]), 2)
        self.assertTrue(all(e["relationship_status"] == "unverified" for e in analyzer.last_result["events"]))
        self.assertTrue(any("净额相抵" in value for value in analyzer.last_result["limitations"]))

    def test_repeated_event_explanations_do_not_double_count(self):
        payload, event = example()
        duplicate = {**event, "explanation": "换个说法", "message_refs": ["M2", "M1"]}
        analyzer = TransferAnalyzer(example_client([event, duplicate]))
        analyzer.analyze(payload)
        self.assertEqual(len(analyzer.last_result["events"]), 1)
        self.assertEqual(analyzer.last_result["quantity_checks"]["D1"]["event_delta"], -1)

    def test_conflicting_shared_evidence_cannot_be_counted(self):
        payload, event = example()
        analyzer = TransferAnalyzer(example_client([event, {**event, "quantity": 2}]))
        analyzer.analyze(payload)
        self.assertEqual(analyzer.last_result["quantity_checks"]["D1"]["event_delta"], 0)

    def test_failed_extraction_is_partial_and_all_differences_reported(self):
        payload, _ = example()
        analyzer = TransferAnalyzer(example_client([], fail_extract=True))
        text = analyzer.analyze(payload)
        self.assertIn("部分完成", text)
        self.assertFalse(analyzer.last_result["coverage"]["complete"])
        self.assertEqual(analyzer.last_result["coverage"]["unprocessed_message_ids"], ["M1", "M2"])
        self.assertEqual({ref for f in analyzer.last_result["findings"] for ref in f["order_refs"]}, {"D1", "D2"})
        self.assertTrue(all(f["analysis_status"] == "incomplete" for f in analyzer.last_result["findings"]))

    def test_empty_result_cannot_hide_obvious_unexamined_transfer_candidate(self):
        payload, _ = example()
        analyzer = TransferAnalyzer(example_client([]))
        text = analyzer.analyze(payload)
        self.assertIn("部分完成", text)
        self.assertEqual(analyzer.last_result["coverage"]["unresolved_candidate_message_ids"], ["M1"])
        self.assertEqual(analyzer.last_result["events"], [])

    def test_transfer_schema_after_payload_preserves_stable_prefix(self):
        client = LlamaClient(__file__)
        client._engine = Mock()
        client._complete = Mock(return_value='{"stage":"extract","events":[],"findings":[],"limitations":[]}')
        grammar = SimpleNamespace(from_json_schema=Mock(return_value=None))
        with patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(LlamaGrammar=grammar)}):
            for message_id in ("M1", "M2"):
                client.structured("提取", {"knowledge": [{"id": "K1"}], "stage": "extract",
                                          "messages": [{"id": message_id}]}, TransferResult,
                                  schema_after_payload=True)
        first, second = [call.args[0] for call in client._complete.call_args_list]
        self.assertEqual(first[0], second[0])
        self.assertIn('"output_schema":', first[1]["content"])
        self.assertLess(first[1]["content"].index('"knowledge"'), first[1]["content"].index('"output_schema"'))

    def test_verified_quantity_does_not_require_model_review(self):
        client = Mock()
        client.structured.return_value = TransferResult.model_validate({
            "stage": "extract", "findings": [], "limitations": [], "events": [{
                "sender_order": "1", "receiver_order": "2", "product": "商品A", "quantity": 1,
                "state": "confirmed", "message_refs": ["M1", "M2"], "knowledge_refs": ["K1"],
                "explanation": "转出与接收"}]})
        payload = {"knowledge": {"entries": [{"id": "K1", "text": "规则"}], "sha256": "test"},
                   "focus_products": [], "chat_coverage": "测试", "chat_start": "start", "chat_end": "end",
                   "orders": {"old": [{"单号": "1", "昵称": "甲", "商品A": "2"},
                                      {"单号": "2", "昵称": "乙", "商品A": "0"}],
                              "new": [{"单号": "1", "昵称": "甲", "商品A": "1"},
                                      {"单号": "2", "昵称": "乙", "商品A": "1"}]},
                   "messages": [{"id": "M1", "群昵称": "1甲", "内容": "转商品A 1件给@2乙"},
                                {"id": "M2", "群昵称": "2乙", "内容": "接"}],
                   "differences": [{"id": "D1", "单号": "1", "变化字段": "商品A"},
                                   {"id": "D2", "单号": "2", "变化字段": "商品A"}]}
        analyzer = TransferAnalyzer(client)
        analyzer.analyze(payload)
        self.assertEqual(client.structured.call_count, 1)
        self.assertEqual([f["status"] for f in analyzer.last_result["findings"]], ["unresolved", "unresolved"])
        self.assertTrue(all(f["quantity_status"] == "consistent" and f["relationship_status"] == "unverified"
                            for f in analyzer.last_result["findings"]))
        self.assertEqual(analyzer.last_result["findings"][1]["message_refs"], ["M1", "M2"])
        # 缺少旧订单库存时，不能因为模型声称 confirmed 就自动匹配。
        payload["orders"]["old"][0]["商品A"] = "0"
        client.structured.side_effect = [client.structured.return_value,
            TransferResult(stage="review", events=[], limitations=[], findings=[
                {"status": "unresolved", "summary": ref, "order_refs": [ref],
                 "message_refs": [], "knowledge_refs": ["K1"]} for ref in ("D1", "D2")])]
        analyzer.analyze(payload)
        self.assertTrue(all(f["status"] == "unresolved" for f in analyzer.last_result["findings"]))

    def run_analysis(self, responses):
        client = Mock()
        client.structured.side_effect = responses
        payload = {
            "knowledge": {"entries": [{"id": "K001", "text": "规则"}], "sha256": "test"},
            "orders": {"old": [], "new": []}, "focus_products": [], "messages": [],
            "chat_coverage": "测试", "chat_start": "start", "chat_end": "end",
            "differences": [{"id": "D00001"}, {"id": "D00002"}],
        }
        analyzer = TransferAnalyzer(client)
        progress = []
        analyzer.analyze(payload, progress.append)
        return analyzer.last_result, client, progress

    def test_only_missing_differences_retried(self):
        result, client, progress = self.run_analysis([review("D00001"), review("D00002")])
        self.assertEqual(len(result["findings"]), 2)
        retry = client.structured.call_args_list[1].args[1]
        self.assertEqual(retry["differences"], [{"id": "D00002"}])
        self.assertTrue(any("补查" in item for item in progress))

    def test_persistent_omission_is_explicit_not_matched(self):
        result, client, _ = self.run_analysis([review("D00001"), review()])
        self.assertEqual(client.structured.call_count, 2)
        missing = next(f for f in result["findings"] if f["order_refs"] == ["D00002"])
        self.assertEqual(missing["status"], "unresolved")
        self.assertIn("仍未返回", missing["summary"])
        self.assertEqual(missing["message_refs"], [])
        self.assertTrue(any("漏答" in item for item in result["limitations"]))

    def test_complete_result_does_not_retry(self):
        _, client, _ = self.run_analysis([review("D00001", "D00002")])
        self.assertEqual(client.structured.call_count, 1)

    def test_retry_cannot_reference_previous_batch_findings(self):
        result, _, _ = self.run_analysis([review("D00001"), review("D00001")])
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("不存在的订单差异", result["failures"][0]["error"])
        self.assertEqual(len(result["findings"]), 2)

    def test_cpu_threads_limited_for_prefill_and_generation(self):
        factory = Mock()
        with patch.dict(sys.modules, {"llama_cpp": SimpleNamespace(Llama=factory)}):
            client = LlamaClient(model_path=__file__)
            client._get_engine()
        self.assertEqual(factory.call_args.kwargs["n_threads"], LLM_THREADS)
        self.assertEqual(factory.call_args.kwargs["n_threads_batch"], LLM_THREADS)
        self.assertLessEqual(LLM_THREADS, 4)

    def test_grammar_avoids_duplicate_object_branches(self):
        definition = TransferResult.model_json_schema_for_payload({
            "stage": "extract", "messages": [{"id": "M1"}],
            "knowledge": [{"id": "K1"}],
        })
        self.assertNotIn("anyOf", definition)
        self.assertNotIn("anyOf", definition["$defs"]["Event"])
        self.assertEqual(definition["properties"]["stage"]["const"], "extract")
        refs = definition["$defs"]["Event"]["properties"]["message_refs"]
        self.assertEqual(refs["items"]["enum"], ["M1"])
        with self.assertRaises(ValueError):
            TransferResult.model_validate({"stage": "extract", "findings": [], "limitations": [],
                "events": [{"sender_order": "1", "receiver_order": "2", "product": "商品",
                            "quantity": 1, "state": "confirmed", "message_refs": ["M1"],
                            "knowledge_refs": ["K1"], "explanation": "不足两条证据"}]})

    def test_review_without_messages_only_allows_unresolved(self):
        definition = TransferResult.model_json_schema_for_payload({
            "stage": "review", "messages": [], "differences": [{"id": "D1"}],
            "knowledge": [{"id": "K1"}],
        })
        self.assertEqual(definition["$defs"]["Finding"]["properties"]["status"]["const"], "unresolved")
        self.assertEqual(definition["properties"]["findings"]["minItems"], 1)
        self.assertEqual(definition["properties"]["findings"]["maxItems"], 1)

    def test_review_conclusions_require_evidence_in_grammar(self):
        definition = TransferResult.model_json_schema_for_payload({
            "stage": "review", "messages": [{"id": "M1"}],
            "differences": [{"id": "D1"}, {"id": "D2"}], "knowledge": [{"id": "K1"}],
        })
        self.assertEqual(definition["properties"]["findings"]["minItems"], 2)
        items = definition["properties"]["findings"]["prefixItems"]
        for item, ref in zip(items, ["D1", "D2"]):
            for branch in item["anyOf"]:
                self.assertEqual(branch["properties"]["order_refs"]["items"]["const"], ref)
        for branch in definition["$defs"]["Finding"]["anyOf"]:
            fields = branch["properties"]
            self.assertEqual(fields["order_refs"]["maxItems"], 1)
            if fields["status"]["const"] != "unresolved":
                self.assertEqual(fields["message_refs"]["minItems"], 1)

    def context(self, count=6):
        return {"stage": "extract", "messages": [{"id": f"M{i}", "内容": "接"} for i in range(count)],
                "knowledge": [{"id": "K1"}]}

    def test_limit_splits_only_failed_batch_and_preserves_overlap(self):
        client = Mock()
        calls = []

        def complete(prompt, context, schema, **kwargs):
            ids = [r["id"] for r in context["messages"]]
            calls.append(ids)
            if len(ids) > 4:
                raise OutputLimitError("length")
            return TransferResult(stage="extract", events=[], findings=[], limitations=[])

        client.structured.side_effect = complete
        root = Path(__file__).resolve().parent.parent / "temp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            runner = BatchRunner(client, {"extract": "提取"}, check_refs, directory)
            results = runner.run(self.context())
            self.assertEqual(len(results), 2)
            self.assertEqual(calls, [[f"M{i}" for i in range(6)], ["M0", "M1", "M2", "M3"], ["M2", "M3", "M4", "M5"]])
            self.assertEqual(len(runner.successful_message_ids), 6)
            self.assertEqual(runner.failures, [])
            saved = json.loads((Path(directory) / "batch_0002.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "success")
            self.assertIn("result", saved)

    def test_limit_recovery_has_finite_calls_and_reports_unprocessed_ids(self):
        client = Mock()
        client.structured.side_effect = OutputLimitError("length")
        runner = BatchRunner(client, {"extract": "提取"}, check_refs)
        self.assertEqual(runner.run(self.context(12)), [])
        self.assertLessEqual(client.structured.call_count, 7)
        self.assertTrue(runner.failures)
        self.assertEqual({ref for f in runner.failures for ref in f["ids"]}, {f"M{i}" for i in range(12)})

    def test_long_individual_message_is_not_silently_dropped(self):
        rows = [{"id": "M1", "内容": "a" * 4000}, {"id": "M2", "内容": "接"}]
        batches = list(bounded_batches(rows, limit=3000, overlap=3, max_rows=12))
        self.assertEqual(batches, [[rows[0]], [rows[1]]])

    def test_batch_bounds_and_boundary_reply(self):
        rows = [{"id": f"M{i}"} for i in range(25)]
        batches = list(bounded_batches(rows, limit=3000, overlap=3, max_rows=12))
        self.assertTrue(all(len(batch) <= 12 for batch in batches))
        self.assertTrue(any(rows[11] in batch and rows[12] in batch for batch in batches))
        self.assertEqual({r["id"] for batch in batches for r in batch}, {r["id"] for r in rows})

    def test_confirmation_across_initial_boundary_keeps_both_messages(self):
        payload, event = example()
        payload["messages"] = [{"id": f"M{i:02}", "内容": "普通聊天"} for i in range(14)]
        payload["messages"][11].update(群昵称="1甲", 内容="转商品A 1件给@2乙")
        payload["messages"][12].update(群昵称="2乙", 内容="引用：转商品A 1件给@2乙；接")
        event["message_refs"] = ["M11", "M12"]
        client = Mock()

        def complete(prompt, context, schema, **kwargs):
            ids = {r["id"] for r in context["messages"]}
            extracted = [event] if {"M11", "M12"} <= ids else [
                {**event, "state": "proposed", "message_refs": ["M11"]}]
            return TransferResult(stage="extract", events=extracted, findings=[], limitations=[])

        client.structured.side_effect = complete
        analyzer = TransferAnalyzer(client)
        analyzer.analyze(payload)
        self.assertEqual(client.structured.call_count, 2)
        self.assertEqual(analyzer.last_result["quantity_checks"]["D1"]["event_delta"], -1)
        self.assertTrue(all(f["relationship_status"] == "unverified" for f in analyzer.last_result["findings"]))

    def test_raw_truncated_generation_is_recorded_before_error(self):
        client = LlamaClient(model_path=__file__)
        engine = Mock()
        engine.tokenize.return_value = [1, 2, 3]
        engine.n_ctx.return_value = 16384
        engine.create_completion.return_value = {"choices": [{"text": '{"events":[', "finish_reason": "length"}],
                                                "usage": {"completion_tokens": 4096}}
        client._engine = engine
        records = []
        with self.assertRaises(OutputLimitError):
            client._complete([{"role": "user", "content": "分析"}], max_tokens=4096,
                             diagnostic_callback=records.append)
        self.assertEqual(records[0]["raw_output"], '{"events":[')
        self.assertEqual(records[0]["input_tokens"], 3)
        self.assertEqual(records[0]["output_tokens"], 4096)
        self.assertEqual(records[0]["finish_reason"], "length")
        self.assertIn("cpu_seconds", records[0])

    def test_runner_disables_hidden_validation_retries_and_saves_raw_output(self):
        class FakeLocalClient(LlamaClient):
            def structured(self, prompt, payload, schema, **kwargs):
                assert kwargs["validation_attempts"] == 1
                kwargs["diagnostic_callback"]({"raw_output": '{"events":[', "finish_reason": "length",
                                               "input_tokens": 12, "output_tokens": 4096})
                raise OutputLimitError("length")

        root = Path(__file__).resolve().parent.parent / "temp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            runner = BatchRunner(FakeLocalClient(__file__), {"extract": "提取"}, check_refs, directory)
            runner.run(self.context(1))
            record = json.loads((Path(directory) / "batch_0001.json").read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["generations"][0]["raw_output"], '{"events":[')
            self.assertEqual(runner.failures[0]["ids"], ["M0"])


if __name__ == "__main__":
    unittest.main()
