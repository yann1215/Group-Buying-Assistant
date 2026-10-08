import hashlib
import re
from app.config import get_resource_path, KNOWLEDGE_RESOURCE


def load_knowledge():
    path = get_resource_path(KNOWLEDGE_RESOURCE)
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("知识库为空，请填写 knowledge/knowledge.md。")
    sections = re.split(r"(?m)(?=^## )", text)
    entries = [{"id": f"K{index:03}", "text": section.strip()}
               for index, section in enumerate(sections) if section.strip()]
    # 全局、索引、转单、商品限制、合单与输出约定始终保留。
    relevant = [e for e in entries if re.match(r"## (?:0|1|7|8|9|10|11)\.", e["text"])]
    if not relevant:
        relevant = entries
    if sum(len(e["text"]) for e in relevant) > 18000:
        raise ValueError("分析知识章节过长，请拆分或精简相关章节。")
    return {"path": str(path), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "entries": relevant}


def select_transfer_knowledge(entries, text):
    """核心规则始终保留，相关商品/流程章节按字面触发；不裁剪规则内容。"""
    core = [e for e in entries if re.match(r"## (?:0|1|7)\.", e["text"])]
    if not core:
        return entries
    triggers = {8: r"本体|特典|底胚|车位|镀金|工艺|胚",
                9: r"合单|合发|核弹|囤|补邮|售后"}
    selected_ids = {e["id"] for e in core}
    for number, pattern in triggers.items():
        if re.search(pattern, text):
            selected_ids.update(e["id"] for e in entries if re.match(rf"## {number}\.", e["text"]))
    return [e for e in entries if e["id"] in selected_ids]
