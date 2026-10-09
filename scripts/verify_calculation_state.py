"""订单切换和分阶段计算回归：python -m scripts.verify_calculation_state。

使用临时 Excel 和配置文件运行真实计算，仅替代微信成员核对。
"""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from app.analysis.order_parser import parse_order_file
from app.analysis.product_config import load_product_share_config_file
from app.core.chat_service import ChatService
from app.core.order_version_manager import empty_order_versions
from app.core.path_manager import get_parsed_orders_path
from app.core.tool_orchestrator import SingleCarContext, ToolOrchestrator


class CalculationStateChecks(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory(dir=Path(__file__).resolve().parent)))
        assert self.root.resolve().is_relative_to(Path(__file__).resolve().parent)
        for name in ("ORDER_CONFIG_DIR", "ORDER_OUTPUT_DIR", "ORDER_ARCHIVE_DIR", "WORKSPACE_DIR"):
            self.stack.enter_context(patch(f"app.core.path_manager.{name}", self.root / name))
        self.orders = []
        for index, (quantity, price) in enumerate(((2, 10), (3, 20)), start=1):
            workbook = Workbook()
            prices = workbook.active
            prices.append(["商品名称", "单价"])
            prices.append(["商品A", price])
            orders = workbook.create_sheet("订单")
            orders.append([])
            orders.append([])
            orders.append(["单号", "昵称", "总金额", "发货状态", "商品A"])
            orders.append([1, "成员A", quantity * price, "", quantity])
            path = self.root / f"订单{index}.xlsx"
            workbook.save(path)
            workbook.close()
            self.orders.append(str(path))
        self.tools = ToolOrchestrator()
        self.ctx = self.tools.get_context(1)
        self.ctx.group_name = "回归测试群"
        self.ctx.new_order_file = self.orders[0]
        # 直接调用订单同步入口，避免初始化真实数据库或模型客户端。
        self.service = ChatService.__new__(ChatService)
        self.service.tools = self.tools
        self.stack.enter_context(patch.object(self.tools, "ensure_member_checked", side_effect=self.check_members))

    def check_members(self, ctx, **kwargs):
        ctx.parsed_order_file = parse_order_file(ctx.new_order_file, get_parsed_orders_path(ctx.session_id))
        self.tools.ensure_share_config_loaded(ctx, ctx.parsed_order_file)
        return {"ok": True, "parsed_order_file": ctx.parsed_order_file}

    def switch_order(self, index=1):
        versions = empty_order_versions()
        versions["new_order_file"] = self.orders[index]
        self.service._sync_new_order_to_tools(1, versions)

    def calculate_share(self):
        if not self.ctx.share_request.share_mode:
            self.tools.handle(1, "个数摊30")
        self.tools.handle(1, "算均摊")
        self.assertTrue(self.ctx.share_request.pending_config_confirmation)
        self.tools.handle(1, "确认计算")
        self.assertFalse(self.ctx.share_results_invalidated)
        self.assertIsNotNone(self.ctx.last_share_result)

    def calculate_bulk(self):
        self.tools.handle(1, "算大货")
        self.assertTrue(self.ctx.bulk_request.pending_confirmation)
        self.tools.handle(1, "是")
        self.assertTrue(self.ctx.bulk_request.confirmed)
        self.assertIsNotNone(self.ctx.last_bulk_result)

    def test_share_then_new_order_bulk_and_reload(self):
        self.calculate_share()
        share = deepcopy(self.ctx.last_share_result)
        summary = self.tools.handle(1, "查看均摊")
        share_file = Path(share["result_file"]).read_bytes()
        self.switch_order()
        self.assertFalse(self.ctx.share_results_invalidated)
        self.assertEqual(self.tools.handle(1, "查看均摊"), summary)
        self.calculate_bulk()
        self.assertEqual(self.ctx.last_share_result, share)
        self.assertEqual(Path(share["result_file"]).read_bytes(), share_file)
        config = load_product_share_config_file(self.ctx.share_config_file)[0]
        self.assertEqual(config["商品均摊"], "30.00")
        self.assertEqual(config["单份均摊"], "15.00")
        self.assertEqual(config["商品数量"], 3)
        self.assertEqual(config["商品单价"], "20.00")
        self.assertEqual(config["商品大货总价"], "60.00")
        self.assertEqual(self.ctx.last_bulk_result["source_order_file"], self.orders[1])
        saved = json.loads(json.dumps(self.ctx.to_dict(), ensure_ascii=False))
        self.ctx = self.tools.load_context(1, saved)
        self.assertEqual(self.tools.handle(1, "查看均摊"), summary)
        self.assertEqual(self.ctx.last_share_result, share)
        self.assertEqual(self.ctx.last_bulk_result, saved["last_bulk_result"])
        self.assertTrue(self.ctx.bulk_request.confirmed)

    def test_bulk_then_new_order_share_preserves_bulk(self):
        self.calculate_bulk()
        bulk = deepcopy(self.ctx.last_bulk_result)
        bulk_file = Path(bulk["result_file"]).read_bytes()
        self.switch_order()
        self.assertTrue(self.ctx.bulk_request.confirmed)
        self.calculate_share()
        self.assertEqual(self.ctx.last_bulk_result, bulk)
        self.assertEqual(Path(bulk["result_file"]).read_bytes(), bulk_file)
        self.assertTrue(self.ctx.bulk_request.confirmed)
        config = load_product_share_config_file(self.ctx.share_config_file)[0]
        self.assertEqual(config["商品单价"], "10.00")
        self.assertEqual(config["商品大货总价"], "20.00")
        self.assertEqual(config["单份均摊"], "10.00")

    def test_switch_cancels_pending_operations_preserves_completed_results(self):
        self.calculate_share()
        self.calculate_bulk()
        self.tools.handle(1, "算均摊")
        self.ctx.bulk_request.pending_confirmation = True
        self.ctx.member_checked = True
        self.ctx.member_checked_at = 123
        self.ctx.member_check_signature = "old-order"
        self.switch_order()
        self.assertFalse(self.ctx.share_request.pending_config_confirmation)
        self.assertFalse(self.ctx.share_request.config_confirmed)
        self.assertIsNone(self.ctx.share_request.confirmation_signature)
        self.assertFalse(self.ctx.bulk_request.pending_confirmation)
        self.assertTrue(self.ctx.bulk_request.confirmed)
        self.assertFalse(self.ctx.share_results_invalidated)
        self.assertFalse(self.ctx.member_checked)
        self.assertIsNone(self.ctx.member_checked_at)
        self.assertIsNone(self.ctx.member_check_signature)
        self.assertIsNone(self.ctx.parsed_order_file)

    def test_cancel_or_failed_recalculation_keeps_previous_results(self):
        self.calculate_share()
        self.calculate_bulk()
        share = deepcopy(self.ctx.last_share_result)
        bulk = deepcopy(self.ctx.last_bulk_result)
        for command in ("算大货", "算均摊"):
            self.tools.handle(1, command)
            self.tools.handle(1, "取消")
            self.assertEqual(self.ctx.last_share_result, share)
            self.assertEqual(self.ctx.last_bulk_result, bulk)
            self.assertFalse(self.ctx.share_results_invalidated)
            self.assertTrue(self.ctx.bulk_request.confirmed)
        self.tools.handle(1, "算大货")
        with patch("app.core.tool_orchestrator.create_bulk_receivable_orders", return_value={"ok": False, "message": "测试失败"}):
            self.assertEqual(self.tools.handle(1, "是"), "测试失败")
        self.assertEqual(self.ctx.last_bulk_result, bulk)
        self.assertTrue(self.ctx.bulk_request.confirmed)
        self.tools.handle(1, "算均摊")
        with patch("app.core.tool_orchestrator.calculate_share", return_value={"ok": False, "message": "测试失败"}):
            self.assertEqual(self.tools.handle(1, "确认计算"), "测试失败")
        self.assertEqual(self.ctx.last_share_result, share)
        self.assertFalse(self.ctx.share_results_invalidated)

    def test_explicit_share_parameter_update_keeps_other_parameters_and_bulk(self):
        self.calculate_share()
        self.calculate_bulk()
        bulk = deepcopy(self.ctx.last_bulk_result)
        self.switch_order()
        self.tools.handle(1, "金额60")
        self.assertEqual(self.ctx.share_request.share_mode, "quantity")
        self.assertEqual(self.ctx.share_request.calculation_scope, "flat")
        self.assertEqual(Decimal(self.ctx.share_request.amount), Decimal("60.00"))
        self.assertTrue(self.ctx.share_results_invalidated)
        self.assertEqual(self.ctx.last_bulk_result, bulk)
        self.assertTrue(self.ctx.bulk_request.confirmed)
        self.calculate_share()
        self.assertEqual(self.ctx.last_share_result["total_amount"], "60.00")
        self.assertEqual(self.ctx.last_share_result["total_share_quantity"], 3)
        self.assertEqual(self.ctx.last_share_result["unit_share_amount"], "20.00")
        self.assertEqual(self.ctx.last_bulk_result, bulk)

    def test_combined_calculation_replaces_both_results(self):
        self.calculate_share()
        self.calculate_bulk()
        self.switch_order()
        self.tools.handle(1, "均摊大货一起算")
        self.assertEqual(self.ctx.combined_stage, "share")
        self.tools.handle(1, "确认计算")
        self.assertEqual(self.ctx.combined_stage, "bulk")
        reply = self.tools.handle(1, "是")
        self.assertIn("均摊和大货总金额表已生成", reply)
        self.assertEqual(self.ctx.last_share_result["unit_share_amount"], "10.00")
        self.assertEqual(self.ctx.last_bulk_result["items"][0]["大货应收金额"], "60.00")
        self.assertTrue(self.ctx.bulk_request.confirmed)

    def test_failed_combined_share_does_not_combine_old_share_with_new_bulk(self):
        self.calculate_share()
        self.calculate_bulk()
        share = deepcopy(self.ctx.last_share_result)
        bulk = deepcopy(self.ctx.last_bulk_result)
        self.switch_order()
        self.tools.handle(1, "均摊大货一起算")
        self.tools.handle(1, "确认计算")
        with patch("app.core.tool_orchestrator.calculate_share", return_value={"ok": False, "message": "测试失败"}), patch(
            "app.core.combined_calculation_workflow.create_bulk_receivable_orders"
        ) as bulk_calculator:
            self.assertEqual(self.tools.handle(1, "是"), "测试失败")
            bulk_calculator.assert_not_called()
        self.assertEqual(self.ctx.last_share_result, share)
        self.assertEqual(self.ctx.last_bulk_result, bulk)

    def test_legacy_saved_share_fields_survive_order_switch(self):
        self.calculate_share()
        self.ctx.last_share_result = None
        summary = self.tools.handle(1, "查看均摊")
        self.assertIn("历史配置记录", summary)
        self.switch_order()
        self.assertEqual(self.tools.handle(1, "查看均摊"), summary)
        self.calculate_bulk()
        self.assertEqual(self.tools.handle(1, "查看均摊"), summary)


if __name__ == "__main__":
    unittest.main()
