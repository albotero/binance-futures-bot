from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import Signal
from .base import Candle, Strategy, validate_candles


def closes(candles: list[Candle]) -> list[float]:
    return [float(candle["close"]) for candle in candles]


def highs(candles: list[Candle]) -> list[float]:
    return [float(candle["high"]) for candle in candles]


def lows(candles: list[Candle]) -> list[float]:
    return [float(candle["low"]) for candle in candles]


def heikin_ashi(candles: list[Candle]) -> list[Candle]:
    if not candles:
        return []
    transformed: list[Candle] = []
    previous_open = float(candles[0]["open"])
    previous_close = float(candles[0]["close"])
    for candle in candles:
        open_price = float(candle["open"])
        high_price = float(candle["high"])
        low_price = float(candle["low"])
        close_price = float(candle["close"])
        ha_close = (open_price + high_price + low_price + close_price) / 4
        ha_open = (previous_open + previous_close) / 2
        ha_high = max(high_price, ha_open, ha_close)
        ha_low = min(low_price, ha_open, ha_close)
        transformed.append({
            **candle,
            "open": ha_open,
            "high": ha_high,
            "low": ha_low,
            "close": ha_close,
        })
        previous_open = ha_open
        previous_close = ha_close
    return transformed


def prepare_candles(candles: list[Candle], style: str = "raw") -> list[Candle]:
    validated = validate_candles(candles)
    if style.replace("-", "_").lower() in {"heikin_ashi", "heikinashi", "ha"}:
        return heikin_ashi(validated)
    return validated


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    series = [values[0]]
    for value in values[1:]:
        series.append((value - series[-1]) * multiplier + series[-1])
    return series


def macd(values: list[float], fast_period: int, slow_period: int, signal_period: int) -> tuple[list[float], list[float], list[float]]:
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    line = [fast_value - slow_value for fast_value,
            slow_value in zip(fast, slow)]
    signal = ema(line, signal_period)
    histogram = [line_value - signal_value for line_value,
                 signal_value in zip(line, signal)]
    return line, signal, histogram


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0] * len(values)

    gains: list[float] = [0.0]
    losses: list[float] = [0.0]
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    result = [50.0] * len(values)
    seed_end = min(period + 1, len(values))
    avg_gain = sum(gains[1:seed_end]) / max(seed_end - 1, 1)
    avg_loss = sum(losses[1:seed_end]) / max(seed_end - 1, 1)

    for index in range(period, len(values)):
        if index > period:
            avg_gain = (avg_gain * (period - 1) + gains[index]) / period
            avg_loss = (avg_loss * (period - 1) + losses[index]) / period
        if avg_loss == 0:
            result[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[index] = 100 - (100 / (1 + rs))
    return result


def bollinger(values: list[float], period: int, std_dev: float) -> tuple[list[float], list[float], list[float]]:
    middles: list[float] = []
    uppers: list[float] = []
    lowers: list[float] = []
    for index in range(len(values)):
        window = values[max(0, index - period + 1): index + 1]
        mean = sum(window) / len(window)
        variance = sum((item - mean) ** 2 for item in window) / len(window)
        deviation = math.sqrt(variance)
        middles.append(mean)
        uppers.append(mean + deviation * std_dev)
        lowers.append(mean - deviation * std_dev)
    return middles, uppers, lowers


def adx(candles: list[Candle], period: int = 14) -> tuple[list[float], list[float], list[float]]:
    if len(candles) < period + 2:
        empty = [0.0] * len(candles)
        return empty, empty, empty

    high = highs(candles)
    low = lows(candles)
    close = closes(candles)

    true_ranges: list[float] = [0.0]
    plus_dm: list[float] = [0.0]
    minus_dm: list[float] = [0.0]

    for index in range(1, len(candles)):
        up_move = high[index] - high[index - 1]
        down_move = low[index - 1] - low[index]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move >
                        up_move and down_move > 0 else 0.0)
        true_ranges.append(max(
            high[index] - low[index],
            abs(high[index] - close[index - 1]),
            abs(low[index] - close[index - 1]),
        ))

    atr = [0.0] * len(candles)
    smooth_plus = [0.0] * len(candles)
    smooth_minus = [0.0] * len(candles)
    atr[period - 1] = sum(true_ranges[1:period]) / period
    smooth_plus[period - 1] = sum(plus_dm[1:period]) / period
    smooth_minus[period - 1] = sum(minus_dm[1:period]) / period

    for index in range(period, len(candles)):
        atr[index] = ((atr[index - 1] * (period - 1)) +
                      true_ranges[index]) / period
        smooth_plus[index] = (
            (smooth_plus[index - 1] * (period - 1)) + plus_dm[index]) / period
        smooth_minus[index] = (
            (smooth_minus[index - 1] * (period - 1)) + minus_dm[index]) / period

    plus_di = [0.0] * len(candles)
    minus_di = [0.0] * len(candles)
    dx = [0.0] * len(candles)
    for index in range(period - 1, len(candles)):
        if atr[index] == 0:
            continue
        plus_di[index] = 100 * (smooth_plus[index] / atr[index])
        minus_di[index] = 100 * (smooth_minus[index] / atr[index])
        denominator = plus_di[index] + minus_di[index]
        if denominator != 0:
            dx[index] = 100 * abs(plus_di[index] -
                                  minus_di[index]) / denominator

    adx_line = [0.0] * len(candles)
    adx_start = period * 2 - 2
    if adx_start < len(candles):
        adx_line[adx_start] = sum(dx[period - 1:period * 2 - 1]) / period
        for index in range(adx_start + 1, len(candles)):
            adx_line[index] = (
                (adx_line[index - 1] * (period - 1)) + dx[index]) / period
    return adx_line, plus_di, minus_di


@dataclass(slots=True)
class EmaCrossStrategy(Strategy):
    name: str = "ema_cross"
    fast_period: int = 9
    slow_period: int = 21
    candle_style: str = "raw"

    def generate(self, candles: list[Candle], symbol: str) -> Signal:
        frame = prepare_candles(candles, self.candle_style)
        close_values = closes(frame)
        fast = ema(close_values, self.fast_period)
        slow = ema(close_values, self.slow_period)
        prev_fast, prev_slow = fast[-2], slow[-2]
        curr_fast, curr_slow = fast[-1], slow[-1]
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            return Signal(symbol, self.name, 1.0, f"EMA{self.fast_period} crossed above EMA{self.slow_period}")
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            return Signal(symbol, self.name, -1.0, f"EMA{self.fast_period} crossed below EMA{self.slow_period}")
        diff = (curr_fast - curr_slow) / close_values[-1]
        score = max(min(diff * 100, 1.0), -1.0)
        return Signal(symbol, self.name, score, f"EMA spread score {score:.2f}")


@dataclass(slots=True)
class MacdStrategy(Strategy):
    name: str = "macd"
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    candle_style: str = "raw"

    def generate(self, candles: list[Candle], symbol: str) -> Signal:
        frame = prepare_candles(candles, self.candle_style)
        _, _, histogram = macd(
            closes(frame), self.fast_period, self.slow_period, self.signal_period)
        hist = histogram[-1]
        prev_hist = histogram[-2]
        if prev_hist <= 0 < hist:
            return Signal(symbol, self.name, 1.0, "MACD histogram crossed positive")
        if prev_hist >= 0 > hist:
            return Signal(symbol, self.name, -1.0, "MACD histogram crossed negative")
        score = max(min(hist / frame[-1]["close"] * 100, 1.0), -1.0)
        return Signal(symbol, self.name, score, f"MACD histogram score {score:.2f}")


@dataclass(slots=True)
class RsiReversionStrategy(Strategy):
    name: str = "rsi"
    period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    candle_style: str = "raw"

    def generate(self, candles: list[Candle], symbol: str) -> Signal:
        frame = prepare_candles(candles, self.candle_style)
        value = rsi(closes(frame), self.period)[-1]
        if value <= self.oversold:
            return Signal(symbol, self.name, 0.85, f"RSI oversold at {value:.1f}")
        if value >= self.overbought:
            return Signal(symbol, self.name, -0.85, f"RSI overbought at {value:.1f}")
        midpoint = 50.0
        score = max(min((midpoint - value) / 25, 1.0), -1.0)
        return Signal(symbol, self.name, score, f"RSI score {score:.2f}")


@dataclass(slots=True)
class BollingerStrategy(Strategy):
    name: str = "bollinger"
    period: int = 20
    std_dev: float = 2.0
    candle_style: str = "raw"

    def generate(self, candles: list[Candle], symbol: str) -> Signal:
        frame = prepare_candles(candles, self.candle_style)
        middle, upper, lower = bollinger(
            closes(frame), self.period, self.std_dev)
        price = frame[-1]["close"]
        if price < lower[-1]:
            return Signal(symbol, self.name, 0.9, "Price broke below lower Bollinger band")
        if price > upper[-1]:
            return Signal(symbol, self.name, -0.9, "Price broke above upper Bollinger band")
        width = (upper[-1] - lower[-1]) / price
        score = max(min((middle[-1] - price) / price * 4, 1.0), -1.0)
        return Signal(symbol, self.name, score * max(min(width * 10, 1.0), 0.2), f"Bollinger score {score:.2f}")


@dataclass(slots=True)
class AdxStrategy(Strategy):
    name: str = "adx"
    period: int = 14
    threshold: float = 22.0
    candle_style: str = "raw"

    def generate(self, candles: list[Candle], symbol: str) -> Signal:
        frame = prepare_candles(candles, self.candle_style)
        adx_line, plus_di, minus_di = adx(frame, self.period)
        latest_adx = adx_line[-1]
        if latest_adx < self.threshold:
            return Signal(symbol, self.name, 0.0, f"ADX below threshold at {latest_adx:.1f}")
        if plus_di[-1] > minus_di[-1]:
            return Signal(symbol, self.name, 0.75, f"Strong uptrend, ADX {latest_adx:.1f}")
        return Signal(symbol, self.name, -0.75, f"Strong downtrend, ADX {latest_adx:.1f}")


STRATEGY_REGISTRY = {
    "ema_cross": EmaCrossStrategy,
    "macd": MacdStrategy,
    "rsi": RsiReversionStrategy,
    "bollinger": BollingerStrategy,
    "adx": AdxStrategy,
}


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in STRATEGY_REGISTRY:
        raise KeyError(f"Unknown strategy: {name}")
    return STRATEGY_REGISTRY[name](**(params or {}))
