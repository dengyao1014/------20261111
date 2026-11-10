from __future__ import annotations

import html
import re

from .models import Candidate

# 抱怨 / 求助 / 找替代品的信号词
COMPLAINT = re.compile(
    r"(?:"
    r"how (?:do|can|would|should) (?:i|we|you)"
    r"|is there (?:a|any|an) (?:way|tool|app|plugin|service|solution|workaround)"
    r"|(?:i|we) (?:need|want|wish|would love|am looking for|are looking for|have been looking for)"
    r"|wish (?:there|it|i|we)"
    r"|alternatives? to|replacement for"
    r"|frustrat|painful|annoying|unusable|terrible|hate (?:that|how)"
    r"|can'?t|cannot|unable to|doesn'?t work|does not work"
    r"|feature request|please add|please support|would be (?:great|nice|useful|helpful)"
    r"|recommend|any suggestions?|anyone know"
    r"|missing|no way to|there'?s no"
    r")",
    re.IGNORECASE,
)

# 标题党 / 广告 / 招聘
SKIP_TITLE = re.compile(
    r"(?:\bfor sale\b|hiring|job posting|we'?re hiring|spam|giveaway|coupon|discount code"
    r"|^\s*\[?solved\]?\b)",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return html.unescape(text or "")


def prescreen(title: str, excerpt: str) -> bool:
    """列表页的廉价预筛：只花一次正则，不请求详情。"""
    title = _clean(title)
    if not title or SKIP_TITLE.search(title):
        return False
    return bool(COMPLAINT.search(f"{title}\n{_clean(excerpt)}"))


def candidate_signal(candidate: Candidate) -> str | None:
    """正文级过滤。返回命中的信号词，没命中返回 None。"""
    title = _clean(candidate.title)
    if SKIP_TITLE.search(title):
        return None
    if len(candidate.body) < 150:
        return None
    match = COMPLAINT.search(f"{title}\n{candidate.body[:4000]}")
    return match.group(0).lower() if match else None
