"""共享名称检索：精准优先，模糊匹配返回全部候选，由调用方消歧。"""
import re


def normalize_name(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def fuzzy_name_match(query, candidate, min_query_length=2):
    query, candidate = normalize_name(query), normalize_name(candidate)
    return bool(len(query) >= min_query_length and candidate and
                (query in candidate or candidate in query))


def match_names(query, items, fields, min_query_length=2):
    query = normalize_name(query)
    if not query:
        return []
    exact = [item for item in items if any(
        normalize_name(item.get(key)) == query for key in fields)]
    return exact or [item for item in items if any(
        fuzzy_name_match(query, item.get(key), min_query_length) for key in fields)]
