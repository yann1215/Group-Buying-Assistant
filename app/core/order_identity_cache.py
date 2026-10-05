"""会话 workspace 中的合发身份映射与增量补查。"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from app.analysis.order_identity import resolve_order_identities
from app.analysis.order_parser import OrderParseError
from app.core.path_manager import get_workspace_dir


def mapping_path(session_id):
    return get_workspace_dir(session_id) / "order_identity_mapping.json"


def load_mapping(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        return []
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list):
        return []
    return [entry for entry in data["entries"] if isinstance(entry, dict)]


def _identity(entry):
    nickname = entry.get("wechat_name")
    nickname = nickname if isinstance(nickname, str) and nickname.strip() else ""
    return entry.get("wxid"), nickname, entry.get("name")


def validate_identities(entries):
    seen = {}
    issues = []
    def location(entry):
        row = f"，原始订单第{entry['row']}行" if entry.get('row') is not None else ""
        return (f"车群{entry.get('group') or '未知'}单号{entry.get('serial') or '未知'}"
                f"（订单昵称“{entry.get('name') or '空'}”{row}）")
    for entry in entries:
        wxid, wechat_name, name = _identity(entry)
        missing = [label for label, value in zip(("wxid", "订单昵称"), (wxid, name))
                   if not isinstance(value, str) or not value.strip()]
        if missing:
            issues.append(f"{location(entry)}身份映射字段缺失：{'、'.join(missing)}；"
                          f"wxid={wxid or '空'}，微信昵称={wechat_name or '空'}，"
                          f"匹配依据={entry.get('identity_basis') or '未知'}。请核对微信成员数据或缓存映射")
            continue
        previous_identity = _identity(seen[wxid]) if wxid in seen else None
        if previous_identity and (previous_identity[2] != name or
                                  (previous_identity[1] and wechat_name and previous_identity[1] != wechat_name)):
            previous = seen[wxid]
            issues.append(f"{location(entry)}与{location(previous)}的身份冲突："
                          f"同一 wxid（{wxid}）对应{previous['wechat_name']}（{previous['name']}）"
                          f"与{wechat_name}（{name}），请核对后输入“刷新合发映射”")
        if wxid not in seen or wechat_name:
            seen[wxid] = entry
    if issues:
        raise OrderParseError("\n".join(issues))


def resolve_with_mapping(orders_by_group, source_ids, cached, fetch_members, *, refresh=False):
    """返回本次已解析订单和待保存映射；调用方成功导出后才写盘。"""
    usable = []
    identities = {}
    for entry in ([] if refresh else cached):
        wxid, wechat_name, name = _identity(entry)
        if not all(isinstance(value, str) and value.strip() for value in (wxid, name)):
            continue
        usable.append(dict(entry, wechat_name=wechat_name))
        identities.setdefault(wxid, set()).add((wechat_name, name))
    conflicts = {wxid for wxid, values in identities.items()
                 if len({name for _, name in values}) > 1 or len({nickname for nickname, _ in values if nickname}) > 1}
    resolved = {}
    resolution_issues = []
    for group, orders in orders_by_group.items():
        group_resolved, pending = [], []
        for order in orders:
            exact = [entry for entry in usable if entry.get("source_session_id") == source_ids[group]
                     and entry.get("serial") == order["serial"]]
            # 已有单号关联改了昵称，必须重新确认，不能直接套用其他昵称映射。
            candidates = exact if exact else [entry for entry in usable if entry.get("name") == order["name"]]
            identity_keys = {(entry["wxid"], entry["name"]) for entry in candidates}
            if (len(identity_keys) == 1 and all(entry.get("name") == order["name"] for entry in candidates)
                    and next(iter(identity_keys))[0] not in conflicts):
                identity = candidates[0]
                group_resolved.append(dict(order, wxid=identity["wxid"], wechat_name=identity["wechat_name"],
                                           identity_basis="缓存", verified_at=identity.get("verified_at")))
            else:
                pending.append(order)
        if pending:
            try:
                members = fetch_members(group)
                fresh = resolve_order_identities(pending, members, group)
            except (OrderParseError, ValueError) as error:
                resolution_issues.append(str(error))
                continue
            verified_at = datetime.now(timezone.utc).isoformat()
            for order in fresh:
                order["verified_at"] = verified_at
            group_resolved.extend(fresh)
        # 保留原文件行序，地址优先级不受缓存命中顺序影响。
        resolved[group] = sorted(group_resolved, key=lambda order: order["row"])

    if resolution_issues:
        raise OrderParseError("\n".join(resolution_issues))
    current = [dict(order, group=group) for group, orders in resolved.items() for order in orders]
    validate_identities(current)
    # 自动补查不得静默替换既有身份。主动刷新才允许重新建立对照关系。
    if not refresh:
        current_wxids = {order["wxid"] for order in current}
        affected = [entry for entry in usable if entry["wxid"] in current_wxids]
        validate_identities(affected + current)
        for group, orders in resolved.items():
            for order in orders:
                previous = [entry for entry in usable if entry.get("source_session_id") == source_ids[group]
                            and entry.get("serial") == order["serial"]]
                if any(entry["wxid"] != order["wxid"] for entry in previous):
                    raise OrderParseError(f"{group}单号{order['serial']}的 wxid 与已保存映射冲突，请核对后输入“刷新合发映射”")

    active_sources = set(source_ids.values())
    entries = [] if refresh else [entry for entry in cached if entry.get("source_session_id") not in active_sources]
    for group, orders in resolved.items():
        for order in orders:
            entries.append({key: order.get(key) for key in
                            ("wxid", "wechat_name", "name", "serial", "identity_basis", "verified_at")} |
                           {"source_session_id": source_ids[group], "group": group})
    return resolved, entries


def save_mapping(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=".json", mode="w", encoding="utf-8", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "entries": entries}, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
