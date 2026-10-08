"""不访问实际订单或微信：用固定样例验证两个模块的真实本地推理。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.intent_parser import parse_user_intent
from app.llm.llama_client import LlamaClient
from app.llm.instruction_normalizer import InstructionNormalizer
from app.llm.knowledge import load_knowledge
from app.llm.transfer_analyzer import TransferAnalyzer


def main():
    client = LlamaClient()
    try:
        normalizer = InstructionNormalizer(client)
        result = normalizer.normalize("帮我看看群里谁名字没改好", {
            "session_type": "single_car", "group_name": "样例车", "waiting": {}, "recent_messages": []})
        print("Instruction:", result.model_dump_json(), flush=True)
        if result.status != "normalized" or parse_user_intent(result.normalized_command)["intent"] != "member_check":
            raise ValueError("样例指令未正确规范化，需要检查 prompt 或模型。")
        payload = {
            "knowledge": load_knowledge(), "focus_products": [], "chat_coverage": "固定样例，不代表真实业务",
            "chat_start": "2026-10-08 10:00:00", "chat_end": "2026-10-08 10:01:00",
            "orders": {
                "old": [{"单号": "1", "昵称": "甲", "商品A": "3"}, {"单号": "2", "昵称": "乙", "商品A": "1"}],
                "new": [{"单号": "1", "昵称": "甲", "商品A": "2"}, {"单号": "2", "昵称": "乙", "商品A": "2"}]},
            "messages": [
                {"id": "M000001", "类型": "文本", "wxid": "wxid_a", "群昵称": "1甲", "昵称": "甲", "时间": "2026-10-08 10:00:00", "内容": "转商品A 1件给 @2乙"},
                {"id": "M000002", "类型": "引用消息", "wxid": "wxid_b", "群昵称": "2乙", "昵称": "乙", "时间": "2026-10-08 10:01:00", "内容": "引用甲：转商品A 1件给 @2乙\n接"}],
            "differences": [
                {"id": "D00001", "差异类型": "修改订单", "单号": "1", "变化字段": "商品A", "旧值": "3", "新值": "2"},
                {"id": "D00002", "差异类型": "修改订单", "单号": "2", "变化字段": "商品A", "旧值": "1", "新值": "2"}],
        }
        analyzer = TransferAnalyzer(client)
        print(analyzer.analyze(payload), flush=True)
        print("Transfer JSON:", json.dumps(analyzer.last_result, ensure_ascii=False), flush=True)
        if not analyzer.last_result["events"] or not analyzer.last_result["findings"]:
            raise ValueError("样例未识别到转单事件或差异。")
        if (analyzer.last_result["quantity_checks"]["D00001"]["event_delta"] != -1
                or analyzer.last_result["quantity_checks"]["D00002"]["event_delta"] != 1):
            raise ValueError("样例的转单净数量未正确识别。")
        print("Both local inference modules verified.", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
