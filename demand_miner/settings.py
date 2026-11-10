from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Source:
    name: str
    platform: str
    kind: str = "discourse"
    base_url: str = ""
    pages: int = 2
    delay: float | None = None
    queries: tuple[str, ...] = ()
    feeds: tuple[str, ...] = ()
    subs: tuple[str, ...] = ()
    per_page: int = 15
    proxy: str | None = None


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    db_path: Path
    report_dir: Path
    top_n: int
    max_llm_calls: int
    extract_workers: int
    request_delay: float
    verify_competitors: bool
    cluster_threshold: float
    sources: list[Source]


def _tuple(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def load_settings(config_path: Path | None = None) -> Settings:
    path = config_path or (ROOT / "config.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    sources = [
        Source(
            name=item["name"],
            platform=item.get("platform", item["name"]),
            kind=item.get("kind", "discourse"),
            base_url=item.get("base_url", ""),
            pages=int(item.get("pages", 2)),
            delay=item.get("delay"),
            queries=_tuple(item.get("queries")),
            feeds=_tuple(item.get("feeds")),
            subs=_tuple(item.get("subs")),
            per_page=int(item.get("per_page", 15)),
            proxy=item.get("proxy"),
        )
        for item in raw.get("sources", [])
    ]
    if not sources:
        raise SystemExit("config.yaml 里没有配置任何 sources")

    provider = os.getenv("DEMAND_PROVIDER", raw.get("provider", "anthropic")).strip().lower()
    if provider not in {"anthropic", "deepseek"}:
        raise SystemExit(f"不支持的 provider: {provider}（只支持 anthropic / deepseek）")
    default_model = "deepseek-chat" if provider == "deepseek" else "claude-opus-5-5"

    return Settings(
        provider=provider,
        model=os.getenv("DEMAND_MODEL", raw.get("model", default_model)),
        db_path=ROOT / raw.get("db_path", "data/demand.db"),
        report_dir=ROOT / raw.get("report_dir", "out"),
        top_n=int(raw.get("top_n", 30)),
        max_llm_calls=int(raw.get("max_llm_calls", 40)),
        extract_workers=max(1, int(raw.get("extract_workers", 4))),
        request_delay=float(raw.get("request_delay", 1.0)),
        verify_competitors=bool(raw.get("verify_competitors", True)),
        cluster_threshold=float(raw.get("cluster_threshold", 0.72)),
        sources=sources,
    )


def credential_error(provider: str) -> str | None:
    """返回缺 key 的提示；有 key 返回 None。"""
    if provider == "deepseek":
        if os.getenv("DEEPSEEK_API_KEY"):
            return None
        return "未检测到 DEEPSEEK_API_KEY，请在 .env 里填入。"
    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return None
    return "未检测到 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，请在 .env 里填入。"
