from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from demand_miner.cluster import cluster_items, merge_groups
from demand_miner.collect import collect_source
from demand_miner.extract import build_extractor
from demand_miner.notify import notify
from demand_miner.prefilter import candidate_signal
from demand_miner.report import write_dry_run_report, write_report
from demand_miner.scoring import is_opportunity, label_for, score_for
from demand_miner.settings import credential_error, load_settings
from demand_miner.store import Store
from demand_miner.websearch import search_available, search_competitors

log = logging.getLogger("demand_miner")


def _analyze(extractor, candidate, verify_competitors: bool):
    """单条候选：先联网核实竞品，再交给 LLM 抽取。"""
    if verify_competitors:
        candidate.meta["search_results"] = search_competitors(candidate)
    return extractor.extract(candidate)


def _interleave(candidates: list) -> list:
    """按来源轮流排列，避免 --limit 截断时只跑到前面几个来源。"""
    buckets: dict[str, list] = {}
    order: list[str] = []
    for candidate in candidates:
        if candidate.source not in buckets:
            buckets[candidate.source] = []
            order.append(candidate.source)
        buckets[candidate.source].append(candidate)

    result = []
    while any(buckets[name] for name in order):
        for name in order:
            if buckets[name]:
                result.append(buckets[name].pop(0))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="需求挖掘系统")
    parser.add_argument("--dry-run", action="store_true", help="只抓取和预过滤，不调用 LLM")
    parser.add_argument("--limit", type=int, default=None, help="最多分析多少条，控制成本")
    parser.add_argument("--source", action="append", default=None, help="只跑指定来源，可重复")
    parser.add_argument("--no-web", action="store_true", help="跳过联网竞品核实")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()

    if not args.dry_run:
        hint = credential_error(settings.provider)
        if hint:
            log.error(hint)
            log.error("复制 .env.example 为 .env 并填入 key，或先跑 --dry-run。")
            return 1

    sources = settings.sources
    if args.source:
        wanted = {name.lower() for name in args.source}
        sources = [
            source
            for source in sources
            if source.name.lower() in wanted or source.platform.lower() in wanted
        ]
        if not sources:
            log.error("没有匹配的来源：%s", ", ".join(args.source))
            return 1

    store = Store(settings.db_path)
    weights = store.source_weights()
    candidates = []
    stats = {
        "listed": 0,
        "seen": 0,
        "new": 0,
        "filtered": 0,
        "candidates": 0,
        "analyzed": 0,
    }

    if args.dry_run:
        is_seen = lambda key: False  # noqa: E731
        mark_seen = lambda *args: None  # noqa: E731
    else:
        is_seen = store.is_seen
        mark_seen = store.mark_seen

    try:
        for source in sources:
            log.info("→ 扫描 %s (%s)", source.name, source.kind)
            found, source_stats = collect_source(
                source,
                delay=settings.request_delay,
                is_seen=is_seen,
                mark_seen=mark_seen,
            )
            for key in ("listed", "seen", "new", "filtered", "candidates"):
                stats[key] += source_stats.get(key, 0)
            log.info(
                "  列表 %d / 新帖 %d / 预筛命中 %d",
                source_stats["listed"],
                source_stats["new"],
                source_stats["candidates"],
            )
            candidates.extend(found)

        matched = []
        for candidate in candidates:
            signal = candidate_signal(candidate)
            if signal is None:
                mark_seen(
                    candidate.key,
                    candidate.source,
                    candidate.platform,
                    candidate.url,
                    candidate.title,
                )
                stats["filtered"] += 1
                continue
            candidate.meta["signal"] = signal
            matched.append(candidate)

        matched = _interleave(matched)
        log.info("正文复核后候选：%d 条（已按来源轮流排列）", len(matched))

        if args.dry_run:
            path = write_dry_run_report(matched, stats, settings.report_dir)
            log.info("dry-run 报告：%s", path)
            return 0

        verify = settings.verify_competitors and not args.no_web
        web_ready = verify and search_available()
        if verify and not web_ready:
            log.info("未配置搜索 API，竞品核实跳过（支持 BRAVE_API_KEY / TAVILY_API_KEY / SERPER_API_KEY）")
        log.info(
            "使用 provider=%s model=%s（并发 %d，竞品核实=%s）",
            settings.provider,
            settings.model,
            settings.extract_workers,
            "开" if web_ready else "关",
        )
        limit = args.limit if args.limit is not None else settings.max_llm_calls
        matched = matched[:limit]
        extractor = build_extractor(settings.provider, settings.model)
        raw_items: list[dict] = []
        done = 0

        with ThreadPoolExecutor(max_workers=settings.extract_workers) as pool:
            futures = {
                pool.submit(_analyze, extractor, candidate, web_ready): candidate
                for candidate in matched
            }
            for future in as_completed(futures):
                candidate = futures[future]
                done += 1
                try:
                    demand = future.result()
                except Exception as exc:  # 单条失败不影响整轮
                    log.warning(
                        "[%d/%d] 失败 %s: %s", done, len(matched), candidate.title[:50], exc
                    )
                    continue

                if demand is None:
                    log.info("[%d/%d] 跳过 %s", done, len(matched), candidate.title[:50])
                    continue

                mark_seen(
                    candidate.key,
                    candidate.source,
                    candidate.platform,
                    candidate.url,
                    candidate.title,
                )
                stats["analyzed"] += 1
                weight = weights.get(candidate.source, 1.0)
                raw_items.append(
                    {
                        "candidate": candidate,
                        "demand": demand,
                        "weight": weight,
                        "score": min(10.0, score_for(demand) * weight),
                        "label": "",
                        "is_opportunity": False,
                    }
                )
                log.info(
                    "[%d/%d] %s → 初分 %.1f",
                    done,
                    len(matched),
                    candidate.title[:50],
                    min(10.0, score_for(demand) * weight),
                )

        # 跨帖去重：先让 LLM 找语义重复，再用词面相似度兜底
        pain_items = [item for item in raw_items if item["demand"].is_pain_point]
        issue_items = [item for item in raw_items if not item["demand"].is_pain_point]

        if len(pain_items) > 1:
            entries = [
                {
                    "title": item["demand"].title_en,
                    "quote": item["demand"].quote,
                    "problem": item["demand"].problem_zh,
                }
                for item in pain_items
            ]
            groups = extractor.group_duplicates(entries)
            if groups:
                pain_items = merge_groups(pain_items, groups)
                log.info("LLM 去重：合并 %d 组", len(groups))

        items = cluster_items(pain_items, settings.cluster_threshold) + issue_items

        for item in items:
            if item["demand"].is_pain_point:
                item["score"] = min(10.0, score_for(item["demand"]) * item.get("weight", 1.0))
                item["label"] = label_for(item["score"])
            else:
                item["score"], item["label"] = 0.0, "issue"
            item["is_opportunity"] = is_opportunity(item["demand"])
            store.save_demand(
                item["candidate"],
                item["demand"],
                score=item["score"],
                label=item["label"],
                opportunity=item["is_opportunity"],
            )

        items.sort(key=lambda entry: entry["score"], reverse=True)
        path = write_report(items, stats, settings.report_dir, settings.top_n)
        notify(items, path, stats)

        opportunities = sum(1 for item in items if item["is_opportunity"])
        merged = sum(item.get("merged_count", 1) - 1 for item in items)
        log.info(
            "完成：分析 %d 条，合并重复 %d 条，机会 %d 条，报告 %s",
            stats["analyzed"],
            merged,
            opportunities,
            path,
        )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
