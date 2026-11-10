from __future__ import annotations

import difflib
import re

STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "how", "do", "does", "i", "we", "you", "my", "our", "is", "are", "it",
    "this", "that", "tool", "tools", "app", "apps", "way", "need", "want",
    "please", "help", "any", "can", "cant", "not", "no", "from", "be", "been",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if len(word) > 2 and word not in STOPWORDS}


def similarity(left: str, right: str) -> float:
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    ratio = difflib.SequenceMatcher(None, left.lower(), right.lower()).ratio()
    return 0.6 * jaccard + 0.4 * ratio


def _prepare(item: dict) -> dict:
    prepared = dict(item)
    prepared.setdefault("sources", [item["candidate"].platform])
    prepared.setdefault("urls", [item["candidate"].url])
    prepared.setdefault("merged_count", 1)
    return prepared


def _absorb(primary: dict, other: dict) -> None:
    primary["merged_count"] += 1
    platform = other["candidate"].platform
    url = other["candidate"].url
    if platform not in primary["sources"]:
        primary["sources"].append(platform)
    if url not in primary["urls"]:
        primary["urls"].append(url)
    primary["demand"].frequency = min(
        10, max(primary["demand"].frequency, other["demand"].frequency) + 1
    )


def merge_groups(items: list[dict], groups: list[list[int]]) -> list[dict]:
    """按 LLM 给出的下标分组合并条目。每组保留分数最高的一条。"""
    assigned: set[int] = set()
    merged: list[dict] = []

    for group in groups:
        valid = sorted({index for index in group if 0 <= index < len(items) and index not in assigned})
        if len(valid) < 2:
            continue
        assigned.update(valid)
        ordered = sorted(valid, key=lambda index: items[index]["score"], reverse=True)
        primary = _prepare(items[ordered[0]])
        for index in ordered[1:]:
            _absorb(primary, items[index])
        merged.append(primary)

    for index, item in enumerate(items):
        if index not in assigned:
            merged.append(_prepare(item))
    return merged


def cluster_items(items: list[dict], threshold: float = 0.72) -> list[dict]:
    """词面兜底聚类：标题/引语高度相似就合并。"""
    clusters: list[dict] = []

    for item in sorted(items, key=lambda entry: entry["score"], reverse=True):
        text = f"{item['demand'].title_en} {item['demand'].quote}"
        for cluster in clusters:
            if max(similarity(text, member["text"]) for member in cluster["members"]) >= threshold:
                _absorb(cluster["primary"], item)
                cluster["members"].append({"item": item, "text": text})
                break
        else:
            primary = _prepare(item)
            clusters.append(
                {"primary": primary, "members": [{"item": item, "text": text}]}
            )

    return [cluster["primary"] for cluster in clusters]
