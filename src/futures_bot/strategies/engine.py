from __future__ import annotations

from dataclasses import dataclass

from ..models import Signal, StrategyProfile
from .builtins import build_strategy


@dataclass(slots=True)
class StrategyEvaluation:
    score: float
    action: str
    reasons: list[str]
    signals: list[Signal]


def evaluate_profile(profile: StrategyProfile, candles: list[dict[str, float]], symbol: str) -> StrategyEvaluation:
    signals: list[Signal] = []
    weighted_total = 0.0
    total_weight = 0.0
    reasons: list[str] = []

    for rule in profile.rules:
        if not rule.enabled:
            continue
        strategy = build_strategy(rule.name, rule.params)
        signal = strategy.generate(candles, symbol)
        signals.append(signal)
        weighted_total += signal.score * rule.weight
        total_weight += abs(rule.weight)
        reasons.append(f"{signal.strategy}: {signal.reason}")

    score = weighted_total / total_weight if total_weight else 0.0
    action = "hold"
    if score >= profile.threshold:
        action = "long"
    elif score <= -profile.threshold:
        action = "short"
    return StrategyEvaluation(score=score, action=action, reasons=reasons, signals=signals)
