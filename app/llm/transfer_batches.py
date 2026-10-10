"""有界批次恢复及逐次持久化诊断；不接受截断 JSON 作为结果。"""
from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter

from app.config import TRANSFER_MAX_TOKENS
from app.llm.llama_client import LlamaClient, OutputLimitError, ContextBudgetError
from app.llm.schemas.transfer import TransferResult


class BatchRunner:
    def __init__(self, client, prompts, validate, directory=None, progress=None):
        self.client, self.prompts, self.validate = client, prompts, validate
        self.directory = Path(directory) if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.progress = progress
        self.attempts = []
        self.failures = []
        self.successful_message_ids = set()

    def save(self, record):
        if self.directory:
            path = self.directory / f"batch_{record['attempt']:04}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)

    def run(self, context, depth=0, budget=None):
        budget = [7] if budget is None else budget
        stage = context["stage"]
        key = "messages" if stage == "extract" else "differences"
        rows = context[key]
        record = {"attempt": len(self.attempts) + 1, "stage": stage, "depth": depth,
                  "ids": [r["id"] for r in rows], "input": context, "generations": [], "status": "running"}
        self.attempts.append(record)
        self.save(record)
        started = perf_counter()

        def diagnostic(value):
            record["generations"].append(value)
            self.save(record)

        error = None
        try:
            if budget[0] <= 0:
                raise ValueError("本批恢复次数已达上限")
            budget[0] -= 1
            kwargs = {"max_tokens": TRANSFER_MAX_TOKENS}
            if isinstance(self.client, LlamaClient):
                kwargs["diagnostic_callback"] = diagnostic
                # 重试预算由本层统一管理，禁止底层再次隐式重试。
                kwargs["validation_attempts"] = 1
                kwargs["schema_after_payload"] = True
            result = self.client.structured(self.prompts[stage], context, TransferResult, **kwargs)
            if result.stage != stage:
                raise ValueError("模型返回错误的分析阶段")
            self.validate(result, context["messages"], context.get("differences", []), context["knowledge"])
            record.update(status="success", result=result.model_dump())
            if stage == "extract":
                self.successful_message_ids.update(r["id"] for r in rows)
        except (ValueError, RuntimeError) as caught:
            error = caught
            record.update(status="failed", error=str(caught), error_type=type(caught).__name__)
        finally:
            record["elapsed_seconds"] = perf_counter() - started
            self.save(record)
        if error is None:
            return [result]
        if isinstance(error, (OutputLimitError, ContextBudgetError)) and len(rows) > 1 and depth < 3 and budget[0] >= 2:
            middle = len(rows) // 2
            # 大批次的两半保留边界上下文；两个子批都必须严格小于原批。
            overlap = 1 if stage == "extract" and len(rows) >= 4 else 0
            parts = (rows[:middle + overlap], rows[middle - overlap:])
            if self.progress:
                label = "聊天提取" if stage == "extract" else "差异核查"
                self.progress(f"{label}批次超出预算，拆小重试（第 {depth + 1} 层）……")
            results = []
            for part in parts:
                child = {**context, key: part}
                if stage == "review":
                    child["quantity_checks"] = {r["id"]: context.get("quantity_checks", {})[r["id"]]
                                                for r in part if r["id"] in context.get("quantity_checks", {})}
                results.extend(self.run(child, depth + 1, budget))
            return results
        self.failures.append({"stage": stage, "ids": record["ids"], "error": str(error)})
        if self.progress:
            self.progress(f"本批未完成，保留其他批次结果：{record['ids'][0]} 至 {record['ids'][-1]}。")
        return []
