"""单车聊天记录提取：保存查询时间设置，输出原始和筛选后的 CSV。"""
from __future__ import annotations

import calendar
import csv
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.path_manager import get_chat_history_path, get_workspace_dir, is_within
from integrations.wechatmsg_lite_client import get_wechat_group_messages

DEFAULT_HISTORY_PERIOD = {"count": 1, "unit": "week"}
HISTORY_KEYWORDS = ("转单", "转", "接", "合单", "合", "核弹", "给", "掰", "吐", "分", "姐", "截", "收", "1", "已")
HISTORY_MESSAGE_TYPES = {"文本", "引用消息"}
UNIT_LABELS = {"hour": "小时", "day": "天", "week": "周", "month": "个月", "year": "年"}
TIME_ERROR = "无法识别聊天记录时间，请使用“近7天”“时间1个月”或“从9.1开始”等格式。原时间设置未修改。"


def history_now():
    return datetime.now(timezone(timedelta(hours=8)))


def normalize_history_period(value):
    if isinstance(value, dict) and isinstance(value.get("start_date"), str):
        try:
            return {"start_date": date.fromisoformat(value["start_date"]).isoformat()}
        except ValueError:
            return dict(DEFAULT_HISTORY_PERIOD)
    if (isinstance(value, dict) and type(value.get("count")) is int
            and value["count"] > 0 and value.get("unit") in UNIT_LABELS):
        return {"count": value["count"], "unit": value["unit"]}
    return dict(DEFAULT_HISTORY_PERIOD)


def _number(value):
    if value.isascii() and value.isdecimal():
        return int(value)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if value.count("十") == 1:
        first, last = value.split("十")
        if (not first or first in digits) and (not last or last in digits):
            return digits.get(first, 1) * 10 + digits.get(last, 0)
    raise ValueError(TIME_ERROR)


def parse_history_start_date(value, now):
    """不写年份时使用设置当年的年份，保存后不随年份变化。"""
    number = r"[0-9零一二两三四五六七八九十]+"
    match = re.fullmatch(rf"(?:(\d{{4}})年)?({number})月({number})(?:日|号)?", value)
    if match is None:
        match = re.fullmatch(r"(?:(\d{4})[./-])?(\d{1,2})[./-](\d{1,2})", value)
    if match is None:
        raise ValueError(TIME_ERROR)
    try:
        start = date(int(match[1]) if match[1] else now.year, _number(match[2]), _number(match[3]))
    except ValueError:
        raise ValueError(TIME_ERROR) from None
    if start > now.date():
        raise ValueError("开始日期不能晚于今天。原时间设置未修改。")
    return {"start_date": start.isoformat()}


def parse_history_command(text):
    """转单记录是保留指令；时间只在明确聊天记录动作内解析。"""
    compact = re.sub(r"\s+", "", text)
    if "转单记录" in compact:
        return None
    if not re.search(r"聊天(?:记录|消息)", compact):
        return None
    if not re.search(r"提取|获取|导出|查|搜索|检索|拉取|找出|给我", compact):
        return None
    if re.search(r"(?:不(?:要|用|需要|想)?|取消|暂不|别)(?:再)?(?:提取|获取|导出|查|搜索|检索|拉取|找出)", compact):
        return None
    result = {"intent": "extract_chat_history"}
    # 群名中的数字、时间词不参与范围解析。
    command = "，".join(clause for clause in re.split(r"[，,；;\n]+", text)
                       if not re.match(r"\s*(?:群聊名称|当前群聊|群聊|群名|车名)\s*[:：]", clause))
    command = re.sub(r"\s+", "", command)
    pattern = r"(?<![\d.\-])([0-9零一二两三四五六七八九十]+)(?:个)?(小时|天|日|周|星期|礼拜|月|年)"
    if "从" in command:
        starts = list(re.finditer(r"从(.+?)(?:开始|起)", command))
        if len(starts) != 1:
            result["chat_history_error"] = TIME_ERROR
            return result
        match = starts[0]
        rest = command[:match.start()] + command[match.end():]
        if re.search(pattern, rest) or re.search(r"\d|从|最近|过去", rest):
            result["chat_history_error"] = TIME_ERROR
            return result
        try:
            result["chat_history_period"] = parse_history_start_date(match[1], history_now())
        except ValueError as error:
            result["chat_history_error"] = str(error)
        return result
    matches = list(re.finditer(pattern, command))
    units = {"小时": "hour", "天": "day", "日": "day", "周": "week",
             "星期": "week", "礼拜": "week", "月": "month", "年": "year"}
    if len(matches) == 1:
        match = matches[0]
        try:
            count = _number(match[1])
            if count <= 0 or re.search(r"[.\-]\d|半|\d{4}[-/]\d", command):
                raise ValueError(TIME_ERROR)
            result["chat_history_period"] = {"count": count, "unit": units[match[2]]}
        except ValueError:
            result["chat_history_error"] = TIME_ERROR
    elif matches or re.search(r"时间|最近|近期|近|过去|半|昨天|今天|上周|上个月|\d", command):
        result["chat_history_error"] = TIME_ERROR
    return result


def history_time_range(period, now=None):
    now = now or history_now()
    if "start_date" in period:
        start_date = date.fromisoformat(period["start_date"])
        if start_date > now.date():
            raise ValueError("开始日期不能晚于今天。原时间设置未修改。")
        start = datetime.combine(start_date, datetime.min.time(), tzinfo=now.tzinfo)
        return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    count, unit = period["count"], period["unit"]
    if unit in {"month", "year"}:
        index = now.year * 12 + now.month - 1 - count * (12 if unit == "year" else 1)
        year, month_index = divmod(index, 12)
        month = month_index + 1
        start = now.replace(year=year, month=month,
                            day=min(now.day, calendar.monthrange(year, month)[1]))
    else:
        start = now - timedelta(**{unit + "s": count})
    return start.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")


def filter_history_csv(path):
    """筛选文本或引用消息，保留消息ID，仅移除备注列。"""
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source)
        header = next(reader, None)
        if not header or "内容" not in header:
            raise ValueError("导出文件缺少“内容”列")
        if "类型" not in header:
            raise ValueError("导出文件缺少“类型”列")
        content_index = header.index("内容")
        type_index = header.index("类型")
        output_indices = [index for index, name in enumerate(header) if name != "备注"]
        rows = []
        total = 0
        for row in reader:
            total += 1
            if len(row) != len(header):
                raise ValueError("导出文件存在字段数量不一致的消息")
            content = row[content_index]
            if row[type_index] not in HISTORY_MESSAGE_TYPES:
                continue
            if any(word in content for word in HISTORY_KEYWORDS) or "@" in content:
                rows.append([row[index] for index in output_indices])
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.writer(target)
        writer.writerow([header[index] for index in output_indices])
        writer.writerows(rows)
    return total, len(rows)


def publish_history_files(files, staging):
    """所有文件准备完毕后更新；写入失败时回滚已经更新的文件。"""
    backups = {}
    for index, (_, destination) in enumerate(files):
        if destination.exists():
            backup = Path(staging) / f"previous_{index}.csv"
            shutil.copyfile(destination, backup)
            backups[destination] = backup
    published = []
    try:
        for source, destination in files:
            os.replace(source, destination)
            published.append(destination)
    except OSError:
        for destination in reversed(published):
            if destination in backups:
                os.replace(backups[destination], destination)
            else:
                destination.unlink()
        raise


def handle_chat_history(tools, ctx, intent, progress_callback=None, *, time_range=None, structured=False):
    if intent.get("chat_history_error"):
        return intent["chat_history_error"]
    period = normalize_history_period(intent.get("chat_history_period", ctx.chat_history_period))
    try:
        start, end = time_range or history_time_range(period)
    except ValueError as error:
        return str(error) if "原时间设置未修改" in str(error) else TIME_ERROR
    except OverflowError:
        return TIME_ERROR
    ctx.chat_history_period = period
    label = (f"从{period['start_date']} 00:00开始" if "start_date" in period
             else f"最近{period['count']}{UNIT_LABELS[period['unit']]}")
    if not ctx.group_name:
        return f"聊天记录时间设置已保存：{label}。请先设置车名（群名：实际微信群名称），再提取聊天记录。"
    if progress_callback:
        progress_callback(f"正在提取聊天记录：{start} 至 {end}……")
    try:
        workspace = get_workspace_dir(ctx.session_id)
        # 在临时目录中完成导出、筛选；失败时保留已有的最终文件。
        with TemporaryDirectory(prefix="chat_history_", dir=workspace) as staging:
            result = get_wechat_group_messages(
                group_name=ctx.group_name, start_time=start, end_time=end,
                output_dir=staging, key_input_func=tools.key_input_func,
            )
            if not result.get("ok"):
                return f"聊天记录提取失败：{result.get('message') or '未知错误'}。\n当前时间设置：{label}。"
            raw = result.get("csv_path")
            if not raw:
                raise ValueError("导出函数未返回 CSV 文件路径")
            path = Path(raw).resolve()
            if not is_within(path, staging):
                raise ValueError("导出文件超出当前工作目录")
            # 现有获取函数未返回 wxid；导出目录固定为“群名(wxid)”。
            room = re.search(r"\(([A-Za-z0-9_-]+@chatroom)\)$", path.parent.name)
            if room is None:
                raise ValueError("无法从导出结果确定群聊 wxid")
            destination = get_chat_history_path(ctx.session_id, room[1])
            raw_destination = get_chat_history_path(ctx.session_id, room[1], filtered=False)
            if progress_callback:
                progress_callback("正在筛选聊天记录……")
            filtered_path = Path(staging) / "filtered.csv"
            shutil.copyfile(path, filtered_path)
            total, kept = filter_history_csv(filtered_path)
            if progress_callback:
                progress_callback("正在从聊天记录提取转单记录……")
            from app.core.transfer_extraction_workflow import prepare_transfer_extraction
            extraction = prepare_transfer_extraction(ctx, filtered_path, staging)
            publish_history_files([(path, raw_destination), (filtered_path, destination),
                                   *[(Path(staging) / name, workspace / name)
                                     for name in extraction["files"]]], staging)
        from app.analysis.order_compare import file_signature
        ctx.chat_history_metadata = {
            "group_name": ctx.group_name, "room_wxid": room[1],
            "fetched_at": history_now().isoformat(), "start": start, "end": end,
            "raw_path": str(raw_destination.resolve()), "filtered_path": str(destination.resolve()),
            "filtered_signature": file_signature(destination), "raw_signature": file_signature(raw_destination),
            "total": total, "kept": kept,
            "transfer_extraction": extraction,
        }
        if structured:
            return dict(ctx.chat_history_metadata)
        empty = "\n没有匹配消息，筛选文件只有表头。" if kept == 0 else ""
        return (f"聊天记录已提取并筛选。\n当前时间设置：{label}（后续沿用）。"
                f"\n查询范围：{start} 至 {end}（北京时间）。"
                f"\n共{total}条，保留{kept}条。{empty}"
                f"\n未筛选文件：{raw_destination.resolve()}\n筛选文件：{destination.resolve()}"
                f"\n转单提取完成：转单记录{extraction['counts']['transfer_records.csv']}条，"
                f"候选发起{extraction['counts']['candidate_initiations.csv']}条，"
                f"候选接收{extraction['counts']['candidate_receipts.csv']}条，"
                f"转单待核实{extraction['counts']['needs_review']}条。"
                + "".join(f"\n{name}：{workspace / name}" for name in extraction["files"])
                + ("\n" + "；".join(extraction["warnings"]) if extraction["warnings"] else ""))
    except (OSError, ValueError, RuntimeError) as error:
        return f"聊天记录提取失败：{error}。\n当前时间设置：{label}。"
