"""重放保存的分析输入；写入独立诊断目录，不修改订单、聊天或原分析结果。"""
import argparse
import json
import os
import sys
from pathlib import Path
from time import perf_counter, process_time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.llama_client import LlamaClient, OutputLimitError, ContextBudgetError
from app.llm.transfer_analyzer import TransferAnalyzer
from app.llm.schemas.transfer import TransferResult


class RecordedClient:
    """用保存的模型返回验证当前后处理，不把缓存回放冒充新推理。"""
    model = "recorded-model-responses"

    def __init__(self, directory):
        self.records = {}
        for path in sorted(directory.glob("batch_*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] != "running":
                key = json.dumps(record["input"], sort_keys=True, ensure_ascii=False)
                self.records.setdefault(key, []).append(record)

    def structured(self, prompt, payload, schema, **kwargs):
        key = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if not self.records.get(key):
            raise RuntimeError("没有完全对应的已保存模型响应，不能推测结果")
        record = self.records[key].pop(0)
        if record["status"] == "success":
            return TransferResult.model_validate(record["result"])
        error_type = {"OutputLimitError": OutputLimitError, "ContextBudgetError": ContextBudgetError}.get(
            record.get("error_type"), ValueError)
        raise error_type(record.get("error", "已保存的失败批次"))

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--responses", type=Path, help="仅回放此目录的模型响应，验证当前后处理；不调用模型")
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    directory = args.input.parent / "transfer_diagnostics" / ("replay_" + uuid4().hex)
    directory.mkdir(parents=True)
    print(f"Diagnostics: {directory}", flush=True)
    started, cpu_started = perf_counter(), process_time()
    client = RecordedClient(args.responses) if args.responses else LlamaClient()
    try:
        analyzer = TransferAnalyzer(client)
        report = analyzer.analyze(payload, lambda value: print(value, flush=True), diagnostics_dir=directory)
        elapsed, cpu = perf_counter() - started, process_time() - cpu_started
        result = analyzer.last_result
        summary = {"mode": "recorded_responses" if args.responses else "live_model",
                   "response_source": str(args.responses) if args.responses else None,
                   "elapsed_seconds": elapsed, "cpu_seconds": cpu,
                   "average_cpu_cores": cpu / elapsed,
                   "average_machine_cpu_percent": 100 * cpu / elapsed / (os.cpu_count() or 1),
                   "coverage": result["coverage"], "events": len(result["events"]),
                   "findings": len(result["findings"]), "failures": result["failures"]}
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "report.txt").write_text(report, encoding="utf-8")
        (directory / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        expected = {r["id"] for r in payload["differences"]}
        actual = {ref for f in result["findings"] for ref in f["order_refs"]}
        if actual != expected or any(f["relationship_status"] != "unverified" for f in result["findings"]):
            raise AssertionError("差异覆盖或关系状态不符合要求")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
