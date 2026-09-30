"""参摊属性直接保存；只有对象不唯一时等待选择。"""
import re
from copy import deepcopy

from app.analysis import participation as p
from app.analysis.order_parser import parse_order_file
from app.analysis.product_config import (
    _read_product_config_rows, _write_product_config_rows,
    load_product_share_config_file, owns_product_config,
)
from app.analysis.special_constants import AUTO_NON_SHARE_ROLE
from app.core.path_manager import get_parsed_orders_path
from integrations.wechatmsg_lite_client import get_wechat_group_members


def _suspend_calculation(ctx):
    ctx.share_request.pending_config_confirmation = False
    ctx.share_request.config_confirmed = False
    ctx.share_request.confirmation_signature = None
    ctx.share_request.force = False
    ctx.bulk_request.pending_confirmation = False
    ctx.bulk_request.confirmed = False


def _finish(ctx):
    _suspend_calculation(ctx)
    ctx.last_share_signature = None
    ctx.share_results_invalidated = True
    ctx.member_checked = False
    ctx.member_check_result = None
    ctx.pending_participation = None


def _load_products(tools, ctx):
    if not ctx.group_name or not ctx.new_order_file:
        return '请先设置群聊名称和订单文件，再识别商品。'
    tools.ensure_config_ownership(ctx)
    ctx.parsed_order_file = parse_order_file(ctx.new_order_file, get_parsed_orders_path(ctx.session_id))
    tools.ensure_share_config_loaded(ctx, ctx.parsed_order_file)
    return None


def _save(ctx, draft):
    target = draft['candidates'][0]
    if draft['kind'] == '商品':
        if not owns_product_config(ctx.share_config_file, ctx.config_owner_id):
            ctx.pending_participation = None
            return '商品配置归属已变化，本次未保存。请重新输入修改指令。'
        rows = _read_product_config_rows(ctx.share_config_file)
        matches = [row for row in rows if row['商品名称'] == target['商品名称']]
        if len(matches) != 1:
            return '商品已变化，请重新输入修改指令。'
        matches[0]['计入均摊'] = draft['include']
        _write_product_config_rows(ctx.share_config_file, rows)
        ctx.product_configs = load_product_share_config_file(ctx.share_config_file)
        target = next(row for row in ctx.product_configs if row['商品名称'] == target['商品名称'])
    else:
        if not target.get('角色') and draft['include']:
            ctx.pending_participation = None
            return '该普通成员已经参摊，无需修改。\n' + p.saved_preview('成员', target)
        updated = deepcopy(ctx.special_members)
        if target.get('角色'):
            index = target.get('_special_index')
            if not isinstance(index, int) or not 0 <= index < len(updated):
                return '成员身份无法唯一定位，请补充单号后重新输入。'
            updated[index].update({k: v for k, v in target.items() if v and not k.startswith('_')})
            updated[index]['参摊'] = draft['include']
        else:
            updated.append({**target, '角色': AUTO_NON_SHARE_ROLE, '参摊': False})
        ctx.special_members = updated
        target = updated[index] if target.get('角色') else updated[-1]
    _finish(ctx)
    return '参摊设置已保存；旧均摊结果已失效，本次未执行计算。\n' + p.saved_preview(draft['kind'], target)


def handle_participation(tools, ctx, intent, text):
    command = intent.get('participation')
    draft = ctx.pending_participation
    if draft and draft.get('stage') != 'select_target':
        # 旧版本保存的是待确认修改，不允许升级后按新规则自动执行。
        ctx.pending_participation = None
        if not command and intent['intent'] not in {'update_special_members', 'update_share_config', 'calculate_share', 'calculate_bulk_goods'}:
            return '旧版待确认修改已清除，未执行任何修改；如仍需修改，请重新输入指令。'
        draft = None
    if draft and not command:
        if p.cancelled(text):
            ctx.pending_participation = None
            return '已取消对象选择，原设置未改变。'
        selection = re.fullmatch(r'\s*(?:选择|选|第)?\s*(\d+)\s*(?:个|项)?\s*', text)
        if selection:
            if draft['signature'] != p.fingerprint(ctx):
                ctx.pending_participation = None
                return '订单、商品配置或成员设置已变化，本次未保存。请重新输入修改指令。'
            index = int(selection[1]) - 1
            if not 0 <= index < len(draft['candidates']):
                return '候选编号无效。\n' + p.preview(draft)
            draft['candidates'] = [draft['candidates'][index]]
            return _save(ctx, draft)
        if p.confirmation(text):
            return p.preview(draft)
        correction = re.search(r'(?:但是|但|改成|改为|应该是|是指)\s*(.+)', text)
        if correction and intent['intent'] != 'update_special_members':
            query = re.sub(r'^(?:改成|改为)', '', correction[1]).strip()
            command = p.parse_participation(query) or p.parse_participation(
                query + ('参摊' if draft['include'] else '不参摊'))
        if not command and intent['intent'] in {'chat', 'confirm_share_config'}:
            return p.preview(draft)
        if not command and intent['intent'] not in {'show_share', 'show_special_members'}:
            ctx.pending_participation = None

    if command:
        ctx.pending_participation = None
        kind = command['kind']
        candidates = []
        if kind != '成员':
            error = _load_products(tools, ctx)
            if error:
                return error
            candidates = p.resolve_products(command['query'], ctx.product_configs)
            if candidates:
                kind = '商品'
            elif kind == '商品':
                return '未找到匹配商品，请提供完整名称或商品序号。'
        if kind != '商品':
            if ctx.new_order_file:
                ctx.parsed_order_file = parse_order_file(ctx.new_order_file, get_parsed_orders_path(ctx.session_id))
            members = p.member_candidates(ctx)
            # 本地身份/订单足以精准定位时无需访问微信；否则补充群成员。
            from app.utils.name_matching import normalize_name
            exact = [m for m in members if any(normalize_name(m.get(k)) == normalize_name(command['query']) for k in ('昵称', '群昵称', '角色'))]
            if not exact and not re.fullmatch(r'单号\s*\d+', command['query']) and ctx.group_name:
                try:
                    result = get_wechat_group_members(group_name=ctx.group_name)
                except Exception as exc:
                    return f'群成员检索失败，未修改设置：{exc}'
                if result.get('ok'):
                    members = p.member_candidates(ctx, result.get('members', []))
                elif not p.resolve_members(command['query'], members):
                    return '群成员读取失败，且本地未匹配到成员：' + str(result.get('message', ''))
            candidates = p.resolve_members(command['query'], members)
            kind = '成员'
        if not candidates:
            return '未找到匹配成员，请提供完整昵称、群昵称或“成员单号12不参摊”。'
        draft = {**command, 'kind': kind, 'candidates': candidates,
                 'signature': p.fingerprint(ctx), 'stage': 'select_target'}
        if len(candidates) == 1:
            return _save(ctx, draft)
        ctx.pending_participation = draft
        _suspend_calculation(ctx)
        return p.preview(draft)
    return None
