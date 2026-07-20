from __future__ import annotations

from dataclasses import dataclass

from ..models import ExitPlan, Signal, StrategyProfile
from .base import Candle
from .builtins import build_strategy, prepare_candles


@dataclass(slots=True)
class StrategyEvaluation:
    score: float
    action: str
    reasons: list[str]
    signals: list[Signal]
    exit_plan: ExitPlan | None = None


def evaluate_profile(
    profile: StrategyProfile,
    candles: list[dict[str, float]],
    symbol: str,
    risk_reward_ratio: float = 2.0,
) -> StrategyEvaluation:
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
    exit_plan = derive_exit_plan(
        profile, candles, signals, action, risk_reward_ratio)
    return StrategyEvaluation(score=score, action=action, reasons=reasons, signals=signals, exit_plan=exit_plan)


def derive_exit_plan(
    profile: StrategyProfile,
    candles: list[Candle],
    signals: list[Signal],
    action: str,
    risk_reward_ratio: float,
) -> ExitPlan | None:
    if action not in {"long", "short"} or not candles:
        return None

    candle_style = _profile_candle_style(profile)
    frame = prepare_candles(candles, candle_style)
    current = float(frame[-1]["close"])
    lookback = min(max(20, len(frame) // 3), len(frame))
    window = frame[-lookback:]
    support = min(float(item["low"]) for item in window)
    resistance = max(float(item["high"]) for item in window)
    avg_range = _average_true_range(window)
    buffer = max(avg_range * 0.5, current * 0.003)
    momentum = max((abs(signal.score) for signal in signals), default=0.0)
    reward_multiple = max(risk_reward_ratio, 0.5)
    momentum_bonus = min(momentum, 1.0) * 0.35

    if action == "long":
        stop_loss = min(current - buffer, support - buffer)
        risk = max(current - stop_loss, buffer)
        take_profit = max(resistance, current + risk *
                          (reward_multiple + momentum_bonus))
        trailing = max(current - max(avg_range * 0.8, risk * 0.6), stop_loss)
        rationale = [
            f"support {support:.4f}",
            f"resistance {resistance:.4f}",
            f"avg_range {avg_range:.4f}",
        ]
        return ExitPlan(
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            trailing_stop_price=trailing,
            support_price=support,
            resistance_price=resistance,
            rationale=rationale,
        )

    stop_loss = max(current + buffer, resistance + buffer)
    risk = max(stop_loss - current, buffer)
    take_profit = min(support, current - risk *
                      (reward_multiple + momentum_bonus))
    trailing = min(current + max(avg_range * 0.8, risk * 0.6), stop_loss)
    rationale = [
        f"support {support:.4f}",
        f"resistance {resistance:.4f}",
        f"avg_range {avg_range:.4f}",
    ]
    return ExitPlan(
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        trailing_stop_price=trailing,
        support_price=support,
        resistance_price=resistance,
        rationale=rationale,
    )


def _profile_candle_style(profile: StrategyProfile) -> str:
    for rule in profile.rules:
        style = str(rule.params.get("candle_style", "")).strip()
        if style:
            return style
    return "raw"


def _average_true_range(candles: list[Candle]) -> float:
    if len(candles) < 2:
        return max(float(candles[-1]["high"]) - float(candles[-1]["low"]), 0.0) if candles else 0.0
    ranges: list[float] = []
    previous_close = float(candles[0]["close"])
    for candle in candles[1:]:
        high = float(candle["high"])
        low = float(candle["low"])
        ranges.append(max(high - low, abs(high - previous_close),
                      abs(low - previous_close)))
        previous_close = float(candle["close"])
    return sum(ranges) / max(len(ranges), 1)
