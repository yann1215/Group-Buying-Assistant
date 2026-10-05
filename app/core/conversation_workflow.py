"""两类会话共用的操作。"""
from app.core.path_manager import sanitize_filename
from app.core.session_types import MERGED_SHIPPING


def rename_conversation(ctx, intent):
    title = intent.get("conversation_title", "").strip()
    if not title:
        return "会话名称不能为空，请输入“修改会话名称为新名称”。"
    try:
        sanitize_filename(title)
    except ValueError as error:
        return f"会话名称无效：{error}"
    ctx.conversation_title_override = title
    if ctx.session_type == MERGED_SHIPPING:
        ctx.merge_title = title
    return f"会话名称已修改为：{title}"
