import re

from app.config import get_resource_path, INSTRUCTION_MAX_TOKENS
from app.llm.schemas.instruction import InstructionResult


def is_help_request(text: str) -> bool:
    """识别使用咨询；“帮我算均摊”等操作请求仍走业务流程。"""
    return bool(re.fullmatch(r"\s*(?:帮助|help|使用说明|操作说明|教程)[?？!！。\s]*", text, re.I)
                or re.search(
                    r"怎么|如何|什么意思|有什么功能|有哪些功能|能做什么|能干什么|支持哪些|支持什么"
                    r"|有哪些(?:指令|命令)|什么(?:指令|命令)|(?:列出|介绍|说明).*功能"
                    r"|(?:功能|指令|命令).*(?:介绍|说明|列表|有哪些|是什么|格式|用法)"
                    r"|(?:介绍|说明).*(?:功能|指令|用法)"
                    r"|(?:输入|发送|用|下达)(?:什么|哪些)(?:指令|命令)?"
                    r"|(?:使用|操作)(?:说明|教程)|帮助\s*[:：]", text))


class InstructionNormalizer:
    def __init__(self, client):
        self.client = client

    def normalize(self, text, context):
        if len(text) > 6000:
            raise ValueError("指令过长，请分开输入。")
        prompt = get_resource_path("app/llm/prompts/instruction.md").read_text(encoding="utf-8")
        help_mode = is_help_request(text)
        if help_mode:
            manual = get_resource_path("app/llm/prompts/software_help.md").read_text(encoding="utf-8")
            prompt += "\n本轮是软件帮助咨询，必须用 chat 状态和 chat_reply 回答，不输出 normalized_command。" \
                      "根据以下资料解释功能和指令，不执行示例、不声称已保存或计算。" \
                      "结合当前会话类型说明适用范围；只回答所问内容，概览可分组简述。" \
                      "资料未列出的功能说明尚未支持或无法确定，不编造指令。\n" + manual
        return self.client.structured(prompt, {"text": text, "context": context, "help_mode": help_mode}, InstructionResult,
                                      max_tokens=INSTRUCTION_MAX_TOKENS)
