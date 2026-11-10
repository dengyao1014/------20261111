from __future__ import annotations

from .extract import Demand

# 六个维度，权重和 = 1.0
WEIGHTS = {
    "severity": 0.20,
    "frequency": 0.20,
    "pay": 0.20,
    "competition_gap": 0.20,
    "buildable": 0.10,
    "channel": 0.10,
}


def score_for(demand: Demand) -> float:
    return round(
        sum(getattr(demand, key) * weight for key, weight in WEIGHTS.items()), 2
    )


def label_for(score: float) -> str:
    if score >= 7.5:
        return "high"
    if score >= 6.0:
        return "medium-high"
    if score >= 4.5:
        return "medium"
    return "low"


def is_opportunity(demand: Demand) -> bool:
    """硬门槛：光有痛点不够，还得有人付钱、有地方分发、能一个人做出来。"""
    return (
        demand.is_pain_point
        and demand.pay >= 4
        and demand.competition_gap >= 4
        and demand.buildable >= 6
        and demand.channel >= 3
    )
