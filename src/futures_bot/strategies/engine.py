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
    lookback = min(max(120, len(frame) // 2), len(frame))
    window = frame[-lookback:]

    support, resistance = _recent_swing_levels(window, current)
    avg_range = _average_true_range(window)
    momentum = max((abs(signal.score) for signal in signals), default=0.0)
    reward_multiple = max(risk_reward_ratio, 1.0)
    momentum_bonus = min(momentum, 1.0) * 0.2

    min_risk = max(avg_range * 0.7, current * 0.0025)
    max_risk = max(avg_range * 2.4, current * 0.015)
    swing_padding = max(avg_range * 0.2, current * 0.0008)

    if action == "long":
        if support > 0 and support < current:
            stop_loss = support - swing_padding
        else:
            stop_loss = current - min_risk
        risk = _clamp(current - stop_loss, min_risk, max_risk)
        stop_loss = current - risk
        take_profit = current + risk * (reward_multiple + momentum_bonus)
        trailing = max(current - max(avg_range, risk * 0.55), stop_loss)
        rationale = [
            f"swing_support {support:.4f}",
            f"swing_resistance {resistance:.4f}",
            f"avg_range {avg_range:.4f}",
            f"risk_pct {(risk / max(current, 1e-9)) * 100:.3f}",
        ]
        return ExitPlan(
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            trailing_stop_price=trailing,
            support_price=support,
            resistance_price=resistance,
            rationale=rationale,
        )

    if resistance > current:
        stop_loss = resistance + swing_padding
    else:
        stop_loss = current + min_risk
    risk = _clamp(stop_loss - current, min_risk, max_risk)
    stop_loss = current + risk
    take_profit = current - risk * (reward_multiple + momentum_bonus)
    trailing = min(current + max(avg_range, risk * 0.55), stop_loss)
    rationale = [
        f"swing_support {support:.4f}",
        f"swing_resistance {resistance:.4f}",
        f"avg_range {avg_range:.4f}",
        f"risk_pct {(risk / max(current, 1e-9)) * 100:.3f}",
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


def _recent_swing_levels(candles: list[Candle], current: float, strength: int = 3) -> tuple[float, float]:
    if len(candles) < (strength * 2 + 1):
        lows = [float(item["low"]) for item in candles]
        highs = [float(item["high"]) for item in candles]
        return (min(lows) if lows else current, max(highs) if highs else current)

    pivot_lows: list[float] = []
    pivot_highs: list[float] = []
    for index in range(strength, len(candles) - strength):
        low = float(candles[index]["low"])
        high = float(candles[index]["high"])
        left_lows = [float(candles[index - step]["low"])
                     for step in range(1, strength + 1)]
        right_lows = [float(candles[index + step]["low"])
                      for step in range(1, strength + 1)]
        left_highs = [float(candles[index - step]["high"])
                      for step in range(1, strength + 1)]
        right_highs = [float(candles[index + step]["high"])
                       for step in range(1, strength + 1)]

        if low <= min(left_lows) and low < min(right_lows):
            pivot_lows.append(low)
        if high >= max(left_highs) and high > max(right_highs):
            pivot_highs.append(high)

    lower_levels = [level for level in pivot_lows if level < current]
    upper_levels = [level for level in pivot_highs if level > current]

    support = max(lower_levels) if lower_levels else min(
        float(item["low"]) for item in candles)
    resistance = min(upper_levels) if upper_levels else max(
        float(item["high"]) for item in candles)
    return support, resistance


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))
