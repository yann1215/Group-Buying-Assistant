"""全局密钥指令：在业务解析和聊天持久化之前识别。"""
import re


def parse_key_update_command(text: str) -> str | None:
    match = re.fullmatch(r"\s*更新\s*(?:key|密钥)\s*[:：]?\s*(.*?)\s*", text, re.I | re.S)
    return match.group(1).strip() if match else None


def redact_key_update_command(text: str) -> str:
    value = parse_key_update_command(text)
    if value is None:
        return text
    return "更新key" + (" [已隐藏]" if value else "")
