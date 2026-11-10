from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable

import httpx
from bs4 import BeautifulSoup

from .models import Candidate
from .prefilter import prescreen
from .settings import Source

log = logging.getLogger(__name__)

USER_AGENT = "demand-miner/0.1 (personal research; 1 request/sec)"
GITHUB_SEARCH = "https://api.github.com/search/issues"


def _new_stats() -> dict:
    return {"listed": 0, "seen": 0, "new": 0, "filtered": 0, "candidates": 0}


def html_to_text(raw: str) -> str:
    soup = BeautifulSoup(raw or "", "html.parser")
    for tag in soup(["img", "video", "iframe", "script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------- Discourse


class DiscourseClient:
    """Discourse 论坛的公开 JSON API 客户端。"""

    def __init__(self, base_url: str, delay: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self._last_request = 0.0
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=25.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, **params):
        response = None
        for _ in range(3):
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            response = self._http.get(f"{self.base_url}{path}", params=params)
            self._last_request = time.monotonic()

            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", "10") or 10)
                pause = max(retry_after, 5)
                log.info("  · 触发限流，等待 %ds 后重试", pause)
                time.sleep(pause)
                continue

            response.raise_for_status()
            return response.json()

        assert response is not None
        response.raise_for_status()
        raise RuntimeError("unreachable")

    def latest_topics(self, pages: int = 2) -> list[dict]:
        found: dict[int, dict] = {}
        for page in range(pages):
            payload = self._get("/latest.json", page=page)
            for topic in payload.get("topic_list", {}).get("topics", []):
                found.setdefault(int(topic["id"]), topic)
        return list(found.values())

    def topic_detail(self, topic_id: int) -> dict:
        return self._get(f"/t/{topic_id}.json")

    def topic_url(self, topic: dict) -> str:
        return f"{self.base_url}/t/{topic.get('slug', 'topic')}/{topic['id']}"


def _build_discourse_candidate(
    detail: dict, listed: dict, source: Source, client: DiscourseClient
) -> Candidate:
    posts = detail.get("post_stream", {}).get("posts", [])
    op = next((p for p in posts if p.get("post_number") == 1), posts[0] if posts else None)
    body = html_to_text(op.get("cooked", "")) if op else ""

    replies: list[str] = []
    for post in posts:
        if post.get("post_number") == 1:
            continue
        text = html_to_text(post.get("cooked", ""))
        if text:
            replies.append(text[:1000])
        if len(replies) >= 8:
            break

    topic_id = int(detail.get("id", listed["id"]))
    return Candidate(
        key=f"discourse:{source.name}:{topic_id}",
        source=source.name,
        platform=source.platform,
        url=client.topic_url(detail),
        title=detail.get("title") or listed.get("title", ""),
        body=body,
        excerpt=listed.get("excerpt", ""),
        replies=replies,
        meta={
            "views": detail.get("views", 0),
            "posts_count": detail.get("posts_count", 0),
            "like_count": detail.get("like_count", 0),
            "created_at": detail.get("created_at", ""),
            "tags": detail.get("tags", []),
        },
    )


def collect_discourse(
    source: Source,
    *,
    delay: float,
    is_seen: Callable[[str], bool],
    mark_seen: Callable[[str, str, str, str, str], None],
) -> tuple[list[Candidate], dict]:
    client = DiscourseClient(source.base_url, delay=source.delay or delay)
    stats = _new_stats()
    out: list[Candidate] = []

    try:
        listed_topics = client.latest_topics(source.pages)
    except httpx.HTTPError as exc:
        log.warning("  ! 列表页抓取失败 %s: %s", source.base_url, exc)
        client.close()
        return out, stats

    try:
        for listed in listed_topics:
            stats["listed"] += 1
            topic_id = int(listed["id"])
            key = f"discourse:{source.name}:{topic_id}"
            if is_seen(key):
                stats["seen"] += 1
                continue

            stats["new"] += 1

            if not prescreen(listed.get("title", ""), listed.get("excerpt", "")):
                mark_seen(
                    key,
                    source.name,
                    source.platform,
                    client.topic_url(listed),
                    listed.get("title", ""),
                )
                stats["filtered"] += 1
                continue

            stats["candidates"] += 1
            try:
                detail = client.topic_detail(topic_id)
            except httpx.HTTPError as exc:
                log.warning("  ! 帖子 %s 抓取失败: %s", topic_id, exc)
                continue

            out.append(_build_discourse_candidate(detail, listed, source, client))
    finally:
        client.close()

    return out, stats


# ---------------------------------------------------------------- GitHub


def collect_github(
    source: Source,
    *,
    delay: float,
    is_seen: Callable[[str], bool],
    mark_seen: Callable[[str, str, str, str, str], None],
) -> tuple[list[Candidate], dict]:
    stats = _new_stats()
    out: list[Candidate] = []

    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        log.info("  · 未设置 GITHUB_TOKEN，搜索接口限 10 次/分钟")

    with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as http:
        for query in source.queries:
            time.sleep(delay)
            try:
                response = http.get(
                    GITHUB_SEARCH,
                    params={
                        "q": query,
                        "sort": "created",
                        "order": "desc",
                        "per_page": source.per_page,
                    },
                )
                if response.status_code == 403:
                    log.warning("  ! GitHub 限流，跳过查询：%s", query)
                    continue
                response.raise_for_status()
                items = response.json().get("items", [])
            except httpx.HTTPError as exc:
                log.warning("  ! GitHub 查询失败 %s: %s", query, exc)
                continue

            for issue in items:
                # 搜索接口会把 PR 也返回，这里只要 issue
                if issue.get("pull_request"):
                    continue
                stats["listed"] += 1
                key = f"github:{issue['id']}"
                if is_seen(key):
                    stats["seen"] += 1
                    continue

                stats["new"] += 1
                title = issue.get("title") or ""
                body = issue.get("body") or ""
                url = issue.get("html_url") or ""

                if not prescreen(title, body[:1200]):
                    mark_seen(key, source.name, source.platform, url, title)
                    stats["filtered"] += 1
                    continue

                stats["candidates"] += 1
                out.append(
                    Candidate(
                        key=key,
                        source=source.name,
                        platform=source.platform,
                        url=url,
                        title=title,
                        body=body,
                        excerpt=body[:300],
                        replies=[],
                        meta={
                            "views": 0,
                            "posts_count": issue.get("comments", 0),
                            "like_count": 0,
                            "created_at": issue.get("created_at", ""),
                            "tags": [label.get("name", "") for label in issue.get("labels", [])],
                        },
                    )
                )

    return out, stats


# ---------------------------------------------------------------- WordPress


def collect_wordpress(
    source: Source,
    *,
    delay: float,
    is_seen: Callable[[str], bool],
    mark_seen: Callable[[str, str, str, str, str], None],
) -> tuple[list[Candidate], dict]:
    stats = _new_stats()
    out: list[Candidate] = []

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=25.0, follow_redirects=True
    ) as http:
        for feed in source.feeds:
            time.sleep(delay)
            try:
                response = http.get(feed)
                response.raise_for_status()
                root = ET.fromstring(response.content)
            except (httpx.HTTPError, ET.ParseError) as exc:
                log.warning("  ! WordPress feed 抓取失败 %s: %s", feed, exc)
                continue

            for item in root.iter("item"):
                stats["listed"] += 1
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                body = html_to_text(item.findtext("description") or "")
                key = f"wordpress:{link}"

                if is_seen(key):
                    stats["seen"] += 1
                    continue

                stats["new"] += 1
                if not title or not link or not prescreen(title, body[:1200]):
                    mark_seen(key, source.name, source.platform, link, title)
                    stats["filtered"] += 1
                    continue

                stats["candidates"] += 1
                out.append(
                    Candidate(
                        key=key,
                        source=source.name,
                        platform=source.platform,
                        url=link,
                        title=title,
                        body=body,
                        excerpt=body[:300],
                        replies=[],
                        meta={
                            "views": 0,
                            "posts_count": 0,
                            "like_count": 0,
                            "created_at": item.findtext("pubDate") or "",
                            "tags": [],
                        },
                    )
                )

    return out, stats


# ---------------------------------------------------------------- Reddit


def collect_reddit(
    source: Source,
    *,
    delay: float,
    is_seen: Callable[[str], bool],
    mark_seen: Callable[[str, str, str, str, str], None],
) -> tuple[list[Candidate], dict]:
    """Reddit 在部分网络环境不可达；配置 proxy 后可用。"""
    stats = _new_stats()
    out: list[Candidate] = []
    proxy = source.proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")

    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=20.0,
        follow_redirects=True,
        proxy=proxy or None,
    ) as http:
        for sub in source.subs:
            time.sleep(delay)
            try:
                response = http.get(
                    f"https://www.reddit.com/r/{sub}/new.json", params={"limit": 25}
                )
                response.raise_for_status()
                children = response.json().get("data", {}).get("children", [])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("  ! Reddit r/%s 抓取失败（可能需要代理）: %s", sub, exc)
                continue

            for child in children:
                post = child.get("data", {})
                stats["listed"] += 1
                key = f"reddit:{sub}:{post.get('id')}"
                if is_seen(key):
                    stats["seen"] += 1
                    continue

                stats["new"] += 1
                title = post.get("title") or ""
                body = post.get("selftext") or ""
                url = f"https://www.reddit.com{post.get('permalink', '')}"

                if not prescreen(title, body[:1200]):
                    mark_seen(key, source.name, source.platform, url, title)
                    stats["filtered"] += 1
                    continue

                stats["candidates"] += 1
                out.append(
                    Candidate(
                        key=key,
                        source=source.name,
                        platform=source.platform,
                        url=url,
                        title=title,
                        body=body,
                        excerpt=body[:300],
                        replies=[],
                        meta={
                            "views": 0,
                            "posts_count": post.get("num_comments", 0),
                            "like_count": post.get("score", 0),
                            "created_at": post.get("created_utc", ""),
                            "tags": [sub],
                        },
                    )
                )

    return out, stats


# ---------------------------------------------------------------- dispatch


def collect_source(
    source: Source,
    *,
    delay: float,
    is_seen: Callable[[str], bool],
    mark_seen: Callable[[str, str, str, str, str], None],
) -> tuple[list[Candidate], dict]:
    if source.kind == "github":
        return collect_github(source, delay=delay, is_seen=is_seen, mark_seen=mark_seen)
    if source.kind == "wordpress":
        return collect_wordpress(source, delay=delay, is_seen=is_seen, mark_seen=mark_seen)
    if source.kind == "reddit":
        return collect_reddit(source, delay=delay, is_seen=is_seen, mark_seen=mark_seen)
    return collect_discourse(source, delay=delay, is_seen=is_seen, mark_seen=mark_seen)
