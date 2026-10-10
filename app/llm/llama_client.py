"""直接加载 Qwen3 GGUF；两个模块共用一个惰性加载的推理实例。"""
from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from time import perf_counter, process_time

from pydantic import BaseModel, ValidationError

from app.config import (DEFAULT_MODEL, MODEL_RESOURCE, LLM_MODEL_PATH, LLM_THREADS,
                        LLM_GPU_LAYERS, LLM_CONTEXT_SIZE, get_resource_path)


class OutputLimitError(ValueError):
    """生成结果不完整；调用方可拆分失败批次。"""


class ContextBudgetError(ValueError):
    """完整输入和预留输出无法放入上下文。"""


class LlamaClient:
    def __init__(self, model_path: str | Path | None = None):
        self.model = DEFAULT_MODEL
        self.model_path = Path(model_path or LLM_MODEL_PATH or get_resource_path(MODEL_RESOURCE))
        self._engine = None
        self._lock = RLock()

    def _get_engine(self):
        if self._engine is None:
            if not self.model_path.is_file():
                raise RuntimeError(f"缺少 Qwen3 模型文件：{self.model_path}。请运行 scripts/setup_llama.ps1。")
            try:
                from llama_cpp import Llama
                self._engine = Llama(model_path=str(self.model_path), n_ctx=LLM_CONTEXT_SIZE,
                                     n_threads=LLM_THREADS, n_threads_batch=LLM_THREADS,
                                     n_gpu_layers=LLM_GPU_LAYERS,
                                     verbose=False)
            except ImportError as error:
                raise RuntimeError("缺少 llama-cpp-python 或其运行库，请运行 scripts/setup_llama.ps1。") from error
            except Exception as error:
                raise RuntimeError(f"无法加载 GGUF 模型：{self.model_path}；请检查文件完整性和运行库。") from error
        return self._engine

    @staticmethod
    def _prompt(messages):
        # Qwen3 官方 enable_thinking=False 模板的等价无工具版本。
        parts = []
        for message in messages:
            role = message["role"]
            if role not in {"system", "user", "assistant"}:
                raise ValueError("不支持的消息角色")
            content = str(message["content"])
            # 防止待分析数据中的边界标记构造新的系统消息。
            for marker in ("<|im_start|>", "<|im_end|>", "<|endoftext|>"):
                content = content.replace(marker, marker.replace("<", "＜").replace(">", "＞"))
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>\n")
        return "".join(parts) + "<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def _complete(self, messages, *, max_tokens, grammar=None, diagnostic_callback=None):
        engine = self._get_engine()
        prompt = self._prompt(messages)
        token_count = len(engine.tokenize(prompt.encode("utf-8"), add_bos=False, special=True))
        if token_count + max_tokens >= engine.n_ctx():
            if diagnostic_callback:
                diagnostic_callback({"input_tokens": token_count, "output_tokens": 0,
                                     "max_tokens": max_tokens, "finish_reason": "context_limit", "raw_output": ""})
            raise ContextBudgetError("输入和输出预算超过模型上下文，请缩小聊天时间范围或拆分指令。")
        started, cpu_started = perf_counter(), process_time()
        try:
            response = engine.create_completion(
                prompt=prompt, max_tokens=max_tokens, temperature=0, grammar=grammar,
                stop=["<|im_end|>", "<|endoftext|>"], stream=False,
            )
            choice = response["choices"][0]
            if diagnostic_callback:
                diagnostic_callback({"input_tokens": token_count,
                                     "output_tokens": response.get("usage", {}).get("completion_tokens"),
                                     "max_tokens": max_tokens, "finish_reason": choice.get("finish_reason"),
                                     "elapsed_seconds": perf_counter() - started,
                                     "cpu_seconds": process_time() - cpu_started,
                                     "raw_output": choice["text"]})
            if choice.get("finish_reason") == "length":
                raise OutputLimitError("模型输出达到长度限制，结果不完整，请缩小分析范围。")
            return choice["text"]
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError("本地 llama 推理失败，请检查模型和运行库。") from error

    def chat(self, messages):
        """保留简单文本接口；业务的两个模块使用 structured。"""
        with self._lock:
            return self._complete(messages, max_tokens=1024)

    def structured(self, prompt: str, payload: dict, schema: type[BaseModel], *, max_tokens=2048,
                   diagnostic_callback=None, validation_attempts=2, schema_after_payload=False):
        if validation_attempts < 1:
            raise ValueError("结构化生成至少需要一次尝试")
        builder = getattr(schema, "model_json_schema_for_payload", None)
        definition = json.dumps(builder(payload) if builder else schema.model_json_schema(), ensure_ascii=False)
        messages = [
            {"role": "system", "content": prompt + "\n输出 JSON，遵守 schema：" + definition},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        if schema_after_payload:
            # 批次编号的 enum 放在稳定的知识/订单资料之后，使 llama.cpp 可复用输入前缀。
            messages = [
                {"role": "system", "content": prompt + "\n用户 JSON 中 input 是待分析数据，output_schema 是程序提供的输出结构。只返回符合 output_schema 的 JSON。"},
                {"role": "user", "content": '{"input":' + json.dumps(payload, ensure_ascii=False)
                 + ',"output_schema":' + definition + '}'},
            ]
        with self._lock:
            self._get_engine()
            from llama_cpp import LlamaGrammar
            grammar = LlamaGrammar.from_json_schema(definition, verbose=False)
            for attempt in range(validation_attempts):
                content = self._complete(messages, max_tokens=max_tokens, grammar=grammar,
                                         diagnostic_callback=diagnostic_callback)
                try:
                    return schema.model_validate_json(content)
                except (ValidationError, ValueError) as error:
                    if attempt + 1 >= validation_attempts:
                        raise ValueError("模型未返回有效的结构化结果，请重新尝试。") from error
                    details = ("；".join(item["msg"] for item in error.errors(include_input=False, include_url=False))
                               if isinstance(error, ValidationError) else "结构化结果无效")
                    messages.append({"role": "user", "content": "上次输出未通过校验：" + details[:1000]
                                     + "。请按 schema 输出完整 JSON，并修正上述问题。"})

    def close(self):
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None
