"""参摊命令、候选检索和已保存对象的预览。"""
import hashlib
import json
import re
from pathlib import Path

from app.utils.name_matching import match_names
from app.utils.csv_utils import read_csv_dict_rows
from app.analysis.special_member import normalize_serial, extract_leading_serial


def parse_participation(text):
    text = str(text).strip().rstrip("。！!？?")
    match = re.fullmatch(r"(?:请)?(?P<kind>商品|成员)?\s*[：:]?\s*(?P<name>.+?)\s*(?:设置为|设为|改为|改成)?\s*(?P<state>不参摊|参摊)", text)
    if not match:
        return None
    name = match['name'].strip().strip('（）()“”" ')
    # 旧式带身份/字段的指令仍由特殊成员解析器处理。
    if any(word in name for word in ('昵称=', '单号=', '群昵称=', '，', ',', '；', ';')):
        return None
    if name.startswith(('不要', '取消', '先别', '不想', '查看', '查询')):
        return None
    return {'kind': match['kind'], 'query': name, 'include': match['state'] == '参摊'}


def confirmation(text):
    text = re.sub(r"[\s，,。！!？?]", "", text).casefold()
    words = {'确认', '确认修改', '确定', '确定修改', '确认执行', '对', '对的',
                    '是', '是的', '没错', '正确', '可以', '好的', '好', 'ok',
                    '没问题', '就这样', '执行', '继续', '可以的', '嗯', '嗯嗯',
                    '好啊', '可以修改', '确认保存', '保存', '没问题执行吧', '执行吧'}
    pattern = '|'.join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return bool(re.fullmatch(r'(?:请)?(?:(?:' + pattern + r')[吧啊呀]?)+', text))


def cancelled(text):
    return bool(re.fullmatch(r"\s*(?:取消|取消修改|不对|不正确|先别改|不要改|暂不修改|否|不是|不确认)[。！!\s]*", text))


def fingerprint(ctx):
    files = {}
    for value in (ctx.new_order_file, ctx.share_config_file):
        if value:
            path = Path(value)
            files[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    payload = [ctx.group_name, ctx.config_owner_id, files, ctx.special_members]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def member_candidates(ctx, group_members=()):
    # 同单号的群成员和订单记录合并；不同 wxid 的同名群成员不能合并。
    people = [dict(m, _special_index=i) for i, m in enumerate(ctx.special_members)]
    rows = read_csv_dict_rows(ctx.parsed_order_file)[0] if ctx.parsed_order_file else []
    for source in [*group_members, *rows]:
        item = dict(source)
        no = normalize_serial(item.get('单号') or extract_leading_serial(item.get('群昵称')))
        item = {k: item.get(k, '') for k in ('昵称', '群昵称', 'wxid')}
        item['单号'] = no
        matches = [p for p in people if
                   (no and normalize_serial(p.get('单号')) == no and
                    not (p.get('wxid') and item.get('wxid') and p['wxid'] != item['wxid']))
                   or (item.get('wxid') and p.get('wxid') == item['wxid'])]
        if len(matches) == 1:
            for key, value in item.items():
                if value and not matches[0].get(key):
                    matches[0][key] = value
        else:
            people.append(item)
    return people


def resolve_products(query, configs):
    serial = re.fullmatch(r"(?:第)?(\d+)(?:号|款)?", query)
    if serial:
        return [c for c in configs if str(c.get('商品序号')) == str(int(serial[1]))]
    matches = match_names(query, configs, ('商品名称',), min_query_length=1)
    # 商品本名也可能以“商品”开头，例如“商品A”。
    return matches or [c for c in configs if c.get('商品名称') == '商品' + query]


def resolve_members(query, members):
    serial = re.fullmatch(r"单号\s*(\d+)", query)
    if serial:
        return [m for m in members if normalize_serial(m.get('单号')) == str(int(serial[1]))]
    return match_names(query, members, ('昵称', '群昵称')) or [m for m in members if m.get('角色') == query]


def label(kind, item):
    if kind == '商品':
        return f"商品{item.get('商品序号')}：{item['商品名称']}"
    return f"成员：{item.get('昵称') or item.get('群昵称')}｜群昵称={item.get('群昵称') or '未提供'}｜单号={item.get('单号') or '未提供'}"


def preview(draft):
    return '匹配到多个候选，请回复“选择1”等指定对象，选定后直接保存：\n' + '\n'.join(
        f"{i}. {label(draft['kind'], item)}" for i, item in enumerate(draft['candidates'], 1))


def saved_preview(kind, item):
    if kind == '商品':
        return product_preview(item)
    state = '不参摊' if item.get('参摊') is False else '参摊'
    return label(kind, item) + f"｜身份={item.get('角色') or '普通成员'}｜{state}"


def product_preview(item):
    state = '参摊' if item.get('计入均摊', True) else '不参摊'
    fields = ('商品数量', '商品均摊', '单份均摊', '商品单价', '商品大货总价')
    return label('商品', item) + '｜' + state + ''.join(
        f"｜{field}={item.get(field) if item.get(field) not in (None, '') else '未设置'}" for field in fields)
