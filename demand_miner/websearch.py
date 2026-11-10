from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx

from .models import Candidate

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


_limiter = RateLimiter(1.0)
_client = httpx.Client(timeout=20.0, follow_redirects=True)


def provider() -> str:
    """按环境变量自动选择搜索服务；都没配就返回空字符串。"""
    if os.getenv("BRAVE_API_KEY"):
        return "brave"
    if os.getenv("TAVILY_API_KEY"):
        return "tavily"
    if os.getenv("SERPER_API_KEY"):
        return "serper"
    return ""


def search_available() -> bool:
    return bool(provider())


def _query(candidate: Candidate) -> str:
    text = re.sub(r"[^\w\s]", " ", candidate.title)
    text = re.sub(r"\s+", " ", text).strip()
    return f"{text} {candidate.platform} alternative software tool"[:180]


def _brave(query: str, limit: int) -> list[dict]:
    response = _client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": os.environ["BRAVE_API_KEY"],
        },
    )
    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("description", "")[:220],
        }
        for item in results[:limit]
    ]


def _tavily(query: str, limit: int) -> list[dict]:
    response = _client.post(
        "https://api.tavily.com/search",
        json={
            "api_key": os.environ["TAVILY_API_KEY"],
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
        },
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": (item.get("content") or "")[:220],
        }
        for item in results[:limit]
    ]


def _serper(query: str, limit: int) -> list[dict]:
    response = _client.post(
        "https://google.serper.dev/search",
        headers={
            "X-API-KEY": os.environ["SERPER_API_KEY"],
            "Content-Type": "application/json",
        },
        json={"q": query, "num": limit},
    )
    response.raise_for_status()
    results = response.json().get("organic", [])
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", "")[:220],
        }
        for item in results[:limit]
    ]


def search_competitors(candidate: Candidate, limit: int = 5) -> list[dict]:
    """搜索该需求是否已有成熟产品。没配搜索 API 时返回空列表。"""
    selected = provider()
    if not selected:
        return []

    _limiter.wait()
    query = _query(candidate)
    try:
        if selected == "brave":
            return _brave(query, limit)
        if selected == "tavily":
            return _tavily(query, limit)
        return _serper(query, limit)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        log.debug("竞品搜索失败 %s: %s", candidate.title[:40], exc)
        return []


def format_results(results: list[dict]) -> str:
    if not results:
        return "（没有搜到结果）"
    lines = []
    for index, result in enumerate(results, 1):
        lines.append(f"{index}. {result['title']}\n   {result['url']}\n   {result['snippet']}")
    return "\n".join(lines)
