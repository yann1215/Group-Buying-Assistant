"""指令帮助的单一内容来源及聊天窗口内部导航。"""
from functools import lru_cache
import html
import json
from pathlib import Path
import re
import sys


HELP_HINT = '如需了解更多可用指令，请输入“帮助”查询。'


@lru_cache(maxsize=1)
def help_topics():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
    return json.loads((root / 'knowledge' / 'commands_help.json').read_text(encoding='utf-8'))


def parse_help_command(text):
    match = re.fullmatch(r'帮助(?:\s+(.+)|\s*[:：]\s*(.*))?', str(text).strip())
    if match:
        return {'intent': 'show_help', 'help_topic': (match[1] or match[2] or '').strip()}
    return None


def help_reply(topic=''):
    topics = help_topics()
    selected = next((item for item in topics if topic in (item['id'], item['title'])), None)
    if selected is None:
        lines = ([] if not topic else [f'未找到帮助分类“{topic}”，请选择以下分类。', ''])
        lines += ['帮助目录', '', '点击分类查看说明，也可输入“帮助 分类名称”。',
                  '帮助可在所有会话查看；具体业务请在标注的会话类型中执行。', '']
        lines += [f"[{item['title']}](help:{item['id']})" for item in topics]
        return '\n'.join(lines)
    lines = [f"帮助 · {selected['title']}", '', f"适用：{selected['scope']}",
             selected['description'], '', '使用前提：', selected['prerequisites'], '', '操作步骤：']
    lines += [f'{index}. {step}' for index, step in enumerate(selected['steps'], 1)]
    lines += ['', '注意事项：', selected['notes'], '', '支持的指令：']
    for index, command in enumerate(selected['commands'], 1):
        lines += [f"{index}. 指令功能：{command['description']}",
                  ' 示例：' + ' / '.join(command['examples'])]
    lines += ['[返回帮助目录](help:index)']
    return '\n'.join(lines)


def help_command_from_link(link):
    if link == 'help:index':
        return '帮助'
    for topic in help_topics():
        if link == f"help:{topic['id']}":
            return '帮助 ' + topic['title']
    return None


def message_html(text, *, allow_help_links=False):
    """普通内容始终转义；帮助消息支持标题加粗、缩进和已知分类导航。"""
    escaped = html.escape(text).replace('\n', '<br>')
    if not allow_help_links or not (text.startswith('帮助目录\n') or text.startswith('帮助 · ')
                                   or text.startswith('未找到帮助分类“')):
        return escaped
    links = [('返回帮助目录', 'index')] + [(item['title'], item['id']) for item in help_topics()]
    navigation = {f'[{title}](help:{topic_id})':
                  f'<a href="help:{topic_id}">{html.escape(title)}</a>'
                  for title, topic_id in links}
    headings = {'帮助目录', '使用前提：', '操作步骤：', '注意事项：', '支持的指令：'}
    headings.update(f"帮助 · {item['title']}" for item in help_topics())
    rendered = []
    for line in text.split('\n'):
        safe_line = re.sub(r'^ +', lambda match: '&nbsp;' * len(match[0]), html.escape(line))
        if line in headings:
            safe_line = f'<b>{safe_line}</b>'
        rendered.append(navigation.get(line, safe_line))
    return '<br>'.join(rendered)
