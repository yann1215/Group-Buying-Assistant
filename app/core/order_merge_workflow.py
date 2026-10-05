"""合发车序配置与订单导出；只读取微信身份，不执行完整成员检查。"""
from datetime import datetime, timezone, timedelta
from pathlib import PureWindowsPath
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from app.analysis.order_merge import merge_order_files, read_merge_orders
from app.core.order_identity_cache import mapping_path, load_mapping, resolve_with_mapping, save_mapping
from app.core.order_version_manager import validate_order_path
from app.core.path_manager import ORDER_OUTPUT_DIR, sanitize_filename
from app.database.repositories import list_sessions, get_order_versions, get_session
from integrations.wechatmsg_lite_client import get_wechat_group_members


def get_merge_title(ctx):
    if ctx.conversation_title_override:
        return ctx.conversation_title_override
    session = get_session(ctx.session_id)
    if session is None or not session.get("created_at"):
        raise ValueError("无法读取对话创建时间，不能确定合发名称")
    created = datetime.fromisoformat(str(session["created_at"]))
    # SQLite CURRENT_TIMESTAMP 存储 UTC，不使用导出日期或最后更新时间。
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created.astimezone(timezone(timedelta(hours=8))).strftime("%y%m%d") + "合发"


def handle_order_merge(tools, ctx, intent):
    if intent["intent"] == "show_merge_groups":
        if not ctx.merge_groups:
            return "尚未设置合发车名，请先输入“合发：车1，车2”。"
        sessions = list_sessions(limit=None)
        lines = []
        for index, group in enumerate(ctx.merge_groups, 1):
            matches = [session for session in sessions if str(session.get("group_name") or "").strip() == group]
            if len(matches) != 1:
                display = "未找到对应车群" if not matches else "车群名称不唯一，请核对"
            else:
                source_id = int(matches[0]["id"])
                source_ctx = tools.contexts.get(source_id)
                path = source_ctx.new_order_file if source_ctx is not None else get_order_versions(source_id)["new_order_file"]
                display = PureWindowsPath(str(path)).name if path else "未设置订单"
            lines.append(f"{index}. {group}：{display}")
        return "\n".join(lines)
    if intent["intent"] == "rename_conversation":
        title = intent["conversation_title"]
        if not title:
            return "会话名称不能为空，请输入“修改会话名称为合发5.0”。"
        try:
            sanitize_filename(title)
        except ValueError as error:
            return f"会话名称无效：{error}"
        ctx.conversation_title_override = title
        if ctx.merge_groups:
            ctx.merge_title = title
        return f"会话名称已修改为：{title}"
    if intent["intent"] == "set_merge_groups":
        groups = intent["merge_groups"]
        if len(groups) < 2 or any(not group for group in groups):
            return "请至少输入两个非空车名，例如“合发：车1，车2，车3”。"
        if len(set(groups)) != len(groups):
            return "合发车名不能重复，请重新录入。"
        try:
            title = get_merge_title(ctx)
        except ValueError as error:
            return f"无法保存合发配置：{error}"
        ctx.merge_groups = list(groups)
        ctx.merge_title = title
        return ("已保存合发车序：" + " → ".join(groups) +
                "。\n每人归入其参加的最靠前车补邮清单，收货信息也取自该车。\n输入“输出合发表”生成总清单和各车补邮清单。")

    groups = ctx.merge_groups
    if len(groups) < 2:
        return "请先录入合发车名，例如“合发：车1，车2，车3”。"
    try:
        ctx.merge_title = get_merge_title(ctx)
        sessions = list_sessions(limit=None)
        sources = []
        source_ids = {}
        for group in groups:
            matches = [session for session in sessions if str(session.get("group_name") or "").strip() == group]
            if len(matches) != 1:
                return f"车群{group}合发清单存在以下问题，请核对：\n1. 无法唯一定位车群，请核对已登记的群聊名称。"
            session_id = int(matches[0]["id"])
            # 当前对话上下文可能还未写回数据库。
            source_ctx = tools.contexts.get(session_id)
            path = source_ctx.new_order_file if source_ctx is not None else get_order_versions(session_id)["new_order_file"]
            valid, reason = validate_order_path(path or "")
            if not valid:
                return f"车群{group}合发清单存在以下问题，请核对：\n1. 最新原始订单无效（{reason}），请先登记有效订单。"
            sources.append((group, path))
            source_ids[group] = session_id
        output = ORDER_OUTPUT_DIR / (sanitize_filename(
            ctx.merge_title + "_补邮清单_" + "_".join(groups)) + ".xlsx")
        def fetch_members(group):
            member_result = get_wechat_group_members(group_name=group, key_input_func=tools.key_input_func)
            if not member_result.get("ok"):
                raise ValueError(f"车群{group}无法读取微信身份（{member_result.get('message') or '未知错误'}）")
            return member_result.get("members") or []
        cache_path = mapping_path(ctx.session_id)
        orders_by_group = {group: read_merge_orders(path, group) for group, path in sources}
        resolved, entries = resolve_with_mapping(orders_by_group, source_ids, load_mapping(cache_path), fetch_members,
                                                refresh=intent["intent"] == "refresh_merge_mapping")
        result = merge_order_files(sources, output, {}, resolved_orders_by_group=resolved)
        save_mapping(cache_path, entries)
        reply = [f"合发表已生成：总计{result.total}人，合发{result.combined}人。"]
        reply.extend(f"{group}补邮清单：{count}人" for group, count in result.counts.items())
        reply.append(f"[打开合发表]({result.path})")
        if result.warnings:
            reply.append("\n" + "\n\n".join(result.warnings))
        return "\n".join(reply)
    except (OSError, ValueError, RuntimeError, BadZipFile, InvalidFileException) as error:
        issues = [line for line in str(error).splitlines() if line.strip()]
        return "合发计算已中止，请核对：\n" + "\n".join(
            f"{index}. {issue}" for index, issue in enumerate(issues, 1))
