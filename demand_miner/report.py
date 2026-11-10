from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .extract import Demand
from .models import Candidate


def _safe(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _write(
    report_dir: Path,
    body: str,
    name: str,
    *,
    update_latest: bool = True,
    archive: bool = False,
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / name
    path.write_text(body, encoding="utf-8")

    if update_latest:
        (report_dir / "latest.md").write_text(body, encoding="utf-8")

    if archive:
        runs_dir = report_dir / "runs"
        runs_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        (runs_dir / f"{stamp}.md").write_text(body, encoding="utf-8")

    return path


def _item_block(index: int, item: dict) -> list[str]:
    demand: Demand = item["demand"]
    candidate: Candidate = item["candidate"]
    solutions = "、".join(demand.existing_solutions) or "暂无"

    urls = item.get("urls") or [candidate.url]
    merged = item.get("merged_count", 1)
    source_line = " · ".join(urls[:3])
    if merged > 1:
        platforms = "、".join(item.get("sources") or [candidate.platform])
        source_line += f"（合并 {merged} 个帖子，跨来源：{platforms}）"

    lines = [
        "---",
        "",
        f"## 🔥 需求 #{index} — {demand.title_en}",
        "",
        f"💬 用户诉求：「{demand.quote}」",
        "",
        f"📝 需求描述: {demand.problem_zh}",
        "",
        f"💡 说明: {demand.analysis_zh}",
        "",
        f"🚀 独立开发者的机会: {demand.opportunity_zh}",
        "",
        f"🏷️ 分类: {demand.category}",
        f"🏷️ 潜力: {item['label']}（{item['score']}/10）",
        f"🏷️ 变现: {demand.business_model}",
        f"🏷️ 用户: {demand.user_segment}",
        f"🔍 已有方案: {solutions}",
        f"🔗 来源: {source_line}",
        "",
        "🎯 应对:",
    ]
    lines += [f"{i}. {step}" for i, step in enumerate(demand.action_plan[:6], 1)]
    lines.append("")
    return lines


def write_dry_run_report(
    candidates: list[Candidate], stats: dict, report_dir: Path
) -> Path:
    lines = [
        "# 🔍 预过滤结果（dry-run）",
        "",
        f"> {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"扫描 {stats['listed']} 个新帖，列表页预筛命中 {stats['candidates']} 条，"
        f"正文复核命中 {len(candidates)} 条。",
        "",
    ]
    for index, candidate in enumerate(candidates, 1):
        signal = candidate.meta.get("signal", "")
        lines.append(
            f"{index}. **{candidate.title}** — {candidate.platform} · "
            f"命中 `{signal}` · [原帖]({candidate.url})"
        )
    lines.append("")
    return _write(
        report_dir,
        "\n".join(lines),
        f"dry-run-{datetime.now():%Y-%m-%d}.md",
        update_latest=False,
    )


def write_report(items: list[dict], stats: dict, report_dir: Path, top_n: int) -> Path:
    opportunities = sorted(
        [item for item in items if item["is_opportunity"]],
        key=lambda item: item["score"],
        reverse=True,
    )
    issues = sorted(
        [item for item in items if not item["is_opportunity"]],
        key=lambda item: item["score"],
        reverse=True,
    )
    shown = opportunities[:top_n]

    lines = [
        f"# 📊 今日发现 {len(shown)} 个潜在机会",
        "",
        f"> {datetime.now().strftime('%Y-%m-%d %H:%M')} · "
        f"扫描 {stats['listed']} 个新帖 · 分析 {stats['analyzed']} 条 · "
        f"合并后 {len(items)} 条 · 通过硬门槛 {len(opportunities)} 条",
        "",
    ]

    if not shown:
        lines += ["本轮没有通过硬门槛的需求。可以放宽 config.yaml 的 max_llm_calls，或增加来源。", ""]

    for index, item in enumerate(shown, 1):
        lines += _item_block(index, item)

    if issues:
        lines += [
            "---",
            "",
            "## 📎 未通过硬门槛（观察名单 / issue 池）",
            "",
            "| 标题 | 来源 | 分数 | 类型 | 原因 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in issues[:15]:
            demand: Demand = item["demand"]
            reason = demand.why_not_buildable or (
                "不构成可付费的独立产品需求"
                if not demand.is_pain_point
                else "付费 / 竞争 / 可行性未过硬门槛"
            )
            lines.append(
                f"| [{_safe(demand.title_en)}]({item['candidate'].url}) | "
                f"{_safe(item['candidate'].platform)} | "
                f"{item['score']} | {demand.demand_kind} | {_safe(reason)} |"
            )
        lines.append("")

    return _write(
        report_dir,
        "\n".join(lines),
        f"{datetime.now():%Y-%m-%d}.md",
        archive=True,
    )
