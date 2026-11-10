from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidate:
    """一个抓取到并且通过预过滤的论坛帖子 / issue / 帖子条目。"""

    key: str  # 全局唯一，例如 discourse:Shopify 社区:553677
    source: str
    platform: str
    url: str
    title: str
    body: str
    excerpt: str = ""
    replies: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
