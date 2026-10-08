"""合发车序配置与订单导出；只读取微信身份，不执行完整成员检查。"""
from datetime import datetime, timezone, timedelta
from pathlib import PureWindowsPath
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from app.analysis.order_merge import merge_order_files, read_merge_orders
from app.core.order_identity_cache import mapping_path, load_mapping, resolve_with_mapping, save_mapping
from app.core.order_version_manager import validate_order_path
from app.core.path_manager import ORDER_OUTPUT_DIR, sanitize_filename, get_order_input_path
from app.core.session_types import SINGLE_CAR, MERGED_SHIPPING
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


def resolve_sources(ctx, sessions):
    """已绑定 ID 不重新按名字匹配，删除来源后必须明确重新录入。"""
    sources = [s for s in sessions if s.get("session_type", SINGLE_CAR) == SINGLE_CAR]
    if len(ctx.merge_source_ids) != len(ctx.merge_groups):
        ctx.merge_source_ids = [None] * len(ctx.merge_groups)
    resolved = []
    for index, group in enumerate(ctx.merge_groups):
        if group in ctx.merge_order_files:
            resolved.append((group, None, None))
            continue
        source_id = ctx.merge_source_ids[index]
        matches = [s for s in sources if s["id"] == source_id] if source_id is not None else [
            s for s in sources if str(s.get("group_name") or "").strip() == group]
        if len(matches) != 1:
            resolved.append((group, None, "来源单车会话已删除或不可用，请重新录入合发车名" if source_id is not None
                             else "无法唯一定位车群，请核对已登记的群聊名称"))
            continue
        source = matches[0]
        current_name = str(source.get("group_name") or "").strip()
        if not current_name:
            resolved.append((group, None, "来源单车尚未设置群聊名称"))
            continue
        ctx.merge_source_ids[index] = int(source["id"])
        ctx.merge_groups[index] = current_name
        resolved.append((current_name, int(source["id"]), None))
    return resolved


def source_order_path(tools, source_id):
    source_ctx = tools.contexts.get(source_id)
    if source_ctx is not None and source_ctx.session_type == SINGLE_CAR:
        return source_ctx.new_order_file
    return get_order_versions(source_id)["new_order_file"]


def handle_order_merge(tools, ctx, intent):
    if ctx.session_type != MERGED_SHIPPING:
        return "当前为单车会话，不支持合发指令。请进入合发会话执行。"
    if intent["intent"] == "start_merged_shipping":
        return "当前对话类型已设置为合发对话，请录入车群信息。\n示例：车名 xxx，订单 xxx"
    if intent["intent"] == "add_merge_group":
        group = intent.get("merge_group_name", "").strip()
        order = intent.get("merge_order_input", "").strip()
        if not group or not order:
            return "请同时录入车名和订单，例如：车名 xxx，订单 xxx。"
        if group in ctx.merge_groups:
            return f"车群“{group}”已录入，合发车群不能重复。"
        path = get_order_input_path(order)
        valid, reason = validate_order_path(path)
        if not valid:
            return f"未录入车群“{group}”：订单无效（{reason}），请核对后重新录入。"
        try:
            title = get_merge_title(ctx)
        except ValueError as error:
            return f"无法保存合发配置：{error}"
        ctx.merge_groups.append(group)
        ctx.merge_source_ids.append(None)
        ctx.merge_order_files[group] = path
        ctx.merge_title = title
        count = len(ctx.merge_groups)
        reply = (f"已录入信息：车群{count} {group}，订单 {order}\n"
                 f"当前合发车群数量：{count}，" + ("请继续录入车群信息" if count < 2 else "可继续录入车群信息"))
        if count >= 2:
            reply += '\n如需计算合发补邮清单，请输入指令“输出合发表”'
        return reply
    if intent["intent"] == "show_merge_groups":
        if not ctx.merge_groups:
            return "尚未设置合发车名，请先输入“合发：车1，车2”。"
        lines = []
        for index, (group, source_id, error) in enumerate(resolve_sources(ctx, list_sessions(limit=None)), 1):
            if error:
                display = error
            else:
                path = ctx.merge_order_files.get(group) or source_order_path(tools, source_id)
                display = PureWindowsPath(str(path)).name if path else "未设置订单"
            lines.append(f"{index}. {group}：{display}")
        return "\n".join(lines)
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
        ctx.merge_source_ids = [None] * len(groups)
        ctx.merge_order_files = {}
        resolve_sources(ctx, list_sessions(limit=None))
        ctx.merge_title = title
        return ("已保存合发车序：" + " → ".join(groups) +
                "。\n每人归入其参加的最靠前车补邮清单，收货信息也取自该车。\n输入“输出合发表”生成总清单和各车补邮清单。")

    groups = ctx.merge_groups
    if len(groups) < 2:
        return "请至少录入两个车群及订单，例如“车名 xxx，订单 xxx”；也可输入“合发：车1，车2”。"
    try:
        ctx.merge_title = get_merge_title(ctx)
        sources = []
        source_ids = {}
        for group, session_id, error in resolve_sources(ctx, list_sessions(limit=None)):
            if error:
                return f"车群{group}合发清单存在以下问题，请核对：\n1. {error}。"
            path = ctx.merge_order_files.get(group) or source_order_path(tools, session_id)
            valid, reason = validate_order_path(path or "")
            if not valid:
                return f"车群{group}合发清单存在以下问题，请核对：\n1. 最新原始订单无效（{reason}），请先登记有效订单。"
            sources.append((group, path))
            source_ids[group] = session_id if session_id is not None else f"merge:{ctx.session_id}:{group}"
        groups = ctx.merge_groups
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
        if str(result.path) not in ctx.merge_output_files:
            ctx.merge_output_files.append(str(result.path))
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
