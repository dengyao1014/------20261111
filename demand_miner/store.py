from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .extract import Demand
from .models import Candidate

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    platform    TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS demands (
    key             TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    platform        TEXT NOT NULL,
    url             TEXT NOT NULL,
    title_en        TEXT,
    payload         TEXT NOT NULL,
    score           REAL NOT NULL,
    label           TEXT NOT NULL,
    is_opportunity  INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    key         TEXT PRIMARY KEY,
    vote        INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 看板是多线程 HTTP 服务，允许跨线程使用同一个连接
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def is_seen(self, key: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone()
        return row is not None

    def mark_seen(
        self, key: str, source: str, platform: str, url: str, title: str
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (key, source, platform, url, title, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, source, platform, url, title, _now()),
        )
        self.conn.commit()

    def save_demand(
        self,
        candidate: Candidate,
        demand: Demand,
        *,
        score: float,
        label: str,
        opportunity: bool,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO demands "
            "(key, source, platform, url, title_en, payload, score, label, "
            " is_opportunity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.key,
                candidate.source,
                candidate.platform,
                candidate.url,
                demand.title_en,
                json.dumps(demand.model_dump(), ensure_ascii=False),
                score,
                label,
                1 if opportunity else 0,
                _now(),
            ),
        )
        self.conn.commit()

    def vote(self, key: str, vote: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO feedback (key, vote, created_at) VALUES (?, ?, ?)",
            (key, 1 if vote > 0 else -1, _now()),
        )
        self.conn.commit()

    def votes(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT key, vote FROM feedback").fetchall()
        return {row["key"]: row["vote"] for row in rows}

    def recent_demands(self, days: int = 14) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM demands WHERE created_at >= ? ORDER BY score DESC, created_at DESC",
            (cutoff,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["demand"] = json.loads(item["payload"])
            out.append(item)
        return out

    def source_weights(self) -> dict[str, float]:
        """根据人工反馈给来源加权：好评率高的来源加分，差的减分。

        样本少于 5 票的源不参与调权，避免噪声。
        """
        rows = self.conn.execute(
            """
            SELECT d.source AS source,
                   SUM(CASE WHEN f.vote = 1 THEN 1 ELSE 0 END) AS good,
                   SUM(CASE WHEN f.vote = -1 THEN 1 ELSE 0 END) AS bad
            FROM feedback f JOIN demands d ON d.key = f.key
            GROUP BY d.source
            """
        ).fetchall()
        weights: dict[str, float] = {}
        for row in rows:
            good = row["good"] or 0
            bad = row["bad"] or 0
            total = good + bad
            if total < 5:
                continue
            rate = good / total
            if rate >= 0.7:
                weights[row["source"]] = 1.15
            elif rate <= 0.3:
                weights[row["source"]] = 0.85
            else:
                weights[row["source"]] = 1.0
        return weights
