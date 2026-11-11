from __future__ import annotations

import logging
import os
from datetime import datetime

import httpx
from dotenv import load_dotenv

from .settings import ROOT

load_dotenv(ROOT / ".env")

log = logging.getLogger(__name__)


def build_message(items: list[dict], report_path, stats: dict, limit: int = 8) -> str:
    opportunities = sorted(
        [item for item in items if item["is_opportunity"]],
        key=lambda item: item["score"],
        reverse=True,
    )
    lines = [
        f"📊 需求挖掘日报 · {datetime.now():%Y-%m-%d}",
        f"扫描 {stats['listed']} 帖 → 机会 {len(opportunities)} 条",
        "",
    ]
    for index, item in enumerate(opportunities[:limit], 1):
        demand = item["demand"]
        lines.append(f"{index}. [{item['label']} {item['score']}] {demand.title_en}")
        lines.append(f"   {item['candidate'].platform} · {item['candidate'].url}")
    if len(opportunities) > limit:
        lines.append(f"…另有 {len(opportunities) - limit} 条")
    lines += ["", f"完整报告：{report_path}"]
    return "\n".join(lines)


def send_text(text: str) -> None:
    """发送纯文本到已配置的飞书 / Telegram；没配环境变量就跳过。"""
    webhook = os.getenv("FEISHU_WEBHOOK")
    if webhook:
        try:
            response = httpx.post(
                webhook,
                json={"msg_type": "text", "content": {"text": text}},
                timeout=10.0,
            )
            response.raise_for_status()
            log.info("已推送到飞书")
        except httpx.HTTPError as exc:
            log.warning("飞书推送失败: %s", exc)

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            response = httpx.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": text[:4000],
                    "disable_web_page_preview": "true",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            log.info("已推送到 Telegram")
        except httpx.HTTPError as exc:
            log.warning("Telegram 推送失败: %s", exc)


def notify(items: list[dict], report_path, stats: dict) -> None:
    """推送当天报告摘要。"""
    send_text(build_message(items, report_path, stats))


if __name__ == "__main__":
    # 供 run_daily.sh 在定时任务失败时发告警：
    #   python -m demand_miner.notify "失败信息"
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    send_text((sys.argv[1] if len(sys.argv) > 1 else "需求挖掘系统告警").replace("\\n", "\n"))
