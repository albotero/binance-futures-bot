from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from futures_bot.data.candles import find_candle_gaps, next_candle_open
from futures_bot.exchange import Kline, SymbolRules

STRATEGY_VERSION = "ema-pullback-sr-v4"
Direction = Literal["long", "short"]
StructureTimeframe = Literal["5m", "15m", "30m", "1h", "2h", "4h"]
TimeframePair = tuple[StructureTimeframe, StructureTimeframe]
TIMEFRAME_PAIRS: tuple[TimeframePair, ...] = (
    ("5m", "30m"),
    ("15m", "1h"),
    ("30m", "2h"),
    ("1h", "4h"),
)
DEFAULT_TIMEFRAMES: TimeframePair = ("15m", "1h")


class StrategyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    trend_fast_period: StrictInt = Field(default=50, ge=2, le=500)
    trend_slow_period: StrictInt = Field(default=200, ge=3, le=1000)
    pullback_period: StrictInt = Field(default=20, ge=2, le=500)
    atr_period: StrictInt = Field(default=14, ge=2, le=500)
    swing_period: StrictInt = Field(default=5, ge=2, le=100)
    atr_multiplier: Decimal = Field(default=Decimal("1.5"), gt=0, le=10)
    reward_multiple: Decimal = Field(default=Decimal("2"), ge=1, le=10)
    entry_expiry_bars: StrictInt = Field(default=3, ge=1, le=20)
    time_stop_bars: StrictInt = Field(default=8, ge=1, le=100)
    exit_policy: Literal["protective_only", "legacy_time_limit"] = "protective_only"
    cost_filter: Literal["net_reward_risk", "gross_only"] = "gross_only"
    protection_mode: Literal["support_resistance", "legacy_atr"] = "support_resistance"
    level_lookback_bars: StrictInt = Field(default=96, ge=10, le=500)
    pivot_confirmation_bars: StrictInt = Field(default=2, ge=1, le=10)
    volume_filter: Literal["signal_above_prior_mean", "disabled"] = "signal_above_prior_mean"

    @field_validator("atr_multiplier", "reward_multiple", mode="before")
    @classmethod
    def exact_decimal(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("strategy decimal values must be exact strings")
        return value

    @model_validator(mode="after")
    def ordered_periods(self) -> StrategyConfiguration:
        if self.trend_fast_period >= self.trend_slow_period:
            raise ValueError("trend_fast_period must be below trend_slow_period")
        if self.level_lookback_bars < 2 * self.pivot_confirmation_bars + 1:
            raise ValueError("level lookback must cover both pivot confirmation sides")
        return self

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("ascii")).hexdigest()

    @property
    def implementation(self) -> str:
        if self.volume_filter != "disabled":
            return (
                "ema-pullback-atr-volume-v4"
                if self.protection_mode == "legacy_atr"
                else STRATEGY_VERSION
            )
        if self.protection_mode == "legacy_atr":
            return (
                "ema-pullback-v1"
                if self.exit_policy == "legacy_time_limit"
                else "ema-pullback-atr-v3"
            )
        if self.exit_policy == "legacy_time_limit" and self.cost_filter == "gross_only":
            return "ema-pullback-sr-v2"
        return "ema-pullback-sr-v3"

    @property
    def effective_time_stop_bars(self) -> int | None:
        return self.time_stop_bars if self.exit_policy == "legacy_time_limit" else None

    @property
    def entry_history_bars(self) -> int:
        volume_bars = 21 if self.volume_filter != "disabled" else 0
        if self.protection_mode == "support_resistance":
            return max(self.pullback_period, self.level_lookback_bars, volume_bars)
        return max(
            self.pullback_period,
            self.atr_period + 1,
            self.swing_period,
            volume_bars,
        )

    @property
    def hourly_history_bars(self) -> int:
        return max(
            self.trend_slow_period,
            self.level_lookback_bars if self.protection_mode == "support_resistance" else 0,
        )


@dataclass(frozen=True, slots=True)
class StructureLevel:
    price: Decimal
    kind: Literal["support", "resistance"]
    timeframe: StructureTimeframe
    pivot_time: datetime
    confirmed_at: datetime


def confirmed_levels(
    candles: Sequence[Kline],
    timeframe: StructureTimeframe,
    lookback: int,
    span: int,
) -> tuple[StructureLevel, ...]:
    """Only strict pivots with closed right-hand confirmation and no later close break."""
    window = candles[-lookback:]
    levels = []
    for index in range(span, len(window) - span):
        pivot = window[index]
        neighbors = (*window[index - span : index], *window[index + 1 : index + span + 1])
        candidates: tuple[tuple[Literal["support", "resistance"], Decimal, bool, bool], ...] = (
            (
                "support",
                pivot.low,
                all(pivot.low < item.low for item in neighbors),
                all(item.close >= pivot.low for item in window[index + span + 1 :]),
            ),
            (
                "resistance",
                pivot.high,
                all(pivot.high > item.high for item in neighbors),
                all(item.close <= pivot.high for item in window[index + span + 1 :]),
            ),
        )
        for kind, price, is_pivot, unbroken in candidates:
            if is_pivot and unbroken:
                levels.append(
                    StructureLevel(
                        price,
                        kind,
                        timeframe,
                        pivot.open_time,
                        window[index + span].close_time + timedelta(milliseconds=1),
                    )
                )
    return tuple(levels)


def adverse_fill_price(price: Decimal, buy: bool, fraction: Decimal, tick: Decimal) -> Decimal:
    adjusted = price * (1 + fraction if buy else 1 - fraction)
    return _round_price(adjusted, tick, ROUND_CEILING if buy else ROUND_FLOOR)


def net_reward_to_risk_passes(
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    buy: bool,
    fee_fraction: Decimal,
    exit_slippage: Decimal,
    tick: Decimal,
    minimum_ratio: Decimal,
) -> bool:
    stop_fill = adverse_fill_price(stop, not buy, exit_slippage, tick)
    target_fill = adverse_fill_price(target, not buy, exit_slippage, tick)
    reward = target_fill - entry if buy else entry - target_fill
    loss = entry - stop_fill if buy else stop_fill - entry
    net_reward = reward - (entry + target_fill) * fee_fraction
    net_loss = loss + (entry + stop_fill) * fee_fraction
    return net_reward > 0 and net_loss > 0 and net_reward >= net_loss * minimum_ratio


@dataclass(frozen=True, slots=True)
class EntrySetup:
    id: str
    symbol: str
    direction: Direction
    strategy_version: str
    configuration_checksum: str
    configuration_json: str
    signal_time: datetime
    eligible_from: datetime
    expires_at: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    time_stop_bars: int | None
    stop_level: StructureLevel | None = None
    target_level: StructureLevel | None = None
    expected_entry_price: Decimal | None = None
    minimum_reward_to_risk: Decimal | None = None
    expected_fee_fraction: Decimal | None = None
    expected_exit_slippage: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    setup: EntrySetup | None
    reason: str


def exponential_moving_average(values: Sequence[Decimal], period: int) -> tuple[Decimal, ...]:
    if type(period) is not int or period < 2:
        raise ValueError("EMA period must be an integer of at least two")
    _validate_numbers(values)
    if len(values) < period:
        return ()
    with localcontext() as context:
        context.prec = 40
        previous = sum(values[:period], Decimal(0)) / period
        output = [previous]
        alpha = Decimal(2) / (period + 1)
        for value in values[period:]:
            previous += alpha * (value - previous)
            output.append(previous)
    return tuple(output)


def average_true_range(candles: Sequence[Kline], period: int) -> Decimal | None:
    if type(period) is not int or period < 2:
        raise ValueError("ATR period must be an integer of at least two")
    if len(candles) <= period:
        return None
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(candles, candles[1:], strict=False)
    ]
    with localcontext() as context:
        context.prec = 40
        atr = sum(ranges[:period], Decimal(0)) / period
        for value in ranges[period:]:
            atr = (atr * (period - 1) + value) / period
    return atr


def evaluate_pullback(
    symbol: str,
    hourly_candles: Sequence[Kline],
    entry_candles: Sequence[Kline],
    rules: SymbolRules,
    configuration: StrategyConfiguration,
    *,
    as_of: datetime,
    expected_entry_slippage: Decimal = Decimal(0),
    minimum_reward_to_risk: Decimal = Decimal(0),
    expected_fee_fraction: Decimal = Decimal(0),
    timeframes: TimeframePair = DEFAULT_TIMEFRAMES,
) -> StrategyEvaluation:
    if timeframes not in TIMEFRAME_PAIRS:
        raise ValueError("unsupported entry/trend timeframe pair")
    entry_interval, trend_interval = timeframes
    if rules.symbol != symbol:
        raise ValueError("exchange rules must match strategy symbol")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(UTC)
    if not rules.price_tick.is_finite() or rules.price_tick <= 0:
        raise ValueError("exchange price tick must be positive and finite")
    if (
        not expected_entry_slippage.is_finite()
        or not 0 <= expected_entry_slippage <= 1
        or not minimum_reward_to_risk.is_finite()
        or minimum_reward_to_risk < 0
        or not expected_fee_fraction.is_finite()
        or not 0 <= expected_fee_fraction <= 1
    ):
        raise ValueError(
            "slippage, fees and minimum reward-to-risk must be finite and non-negative"
        )
    hourly = tuple(candle for candle in hourly_candles if candle.close_time < as_of)
    entries = tuple(candle for candle in entry_candles if candle.close_time < as_of)
    _validate_candles(hourly, trend_interval)
    _validate_candles(entries, entry_interval)
    warmup = (
        configuration.pullback_period
        if configuration.protection_mode == "support_resistance"
        else configuration.entry_history_bars
    )
    if configuration.volume_filter != "disabled":
        warmup = max(warmup, 21)
    if len(hourly) < configuration.trend_slow_period or len(entries) < warmup:
        return StrategyEvaluation(None, "indicator warm-up is incomplete")
    last = entries[-1]
    eligible_from = next_candle_open(last.open_time, entry_interval)
    if eligible_from != as_of:
        return StrategyEvaluation(None, "latest closed entry candle is stale")
    entry_duration = next_candle_open(as_of, entry_interval) - as_of
    trend_duration = next_candle_open(as_of, trend_interval) - as_of
    if next_candle_open(hourly[-1].open_time, trend_interval) < (
        as_of - trend_duration + entry_duration
    ):
        return StrategyEvaluation(None, "latest closed trend candle is stale")

    with localcontext() as context:
        context.prec = 40
        closes = [candle.close for candle in hourly]
        fast = exponential_moving_average(closes, configuration.trend_fast_period)[-1]
        slow = exponential_moving_average(closes, configuration.trend_slow_period)[-1]
        pullback = exponential_moving_average(
            [candle.close for candle in entries], configuration.pullback_period
        )[-1]
        direction: Direction
        if fast > slow and last.low <= pullback < last.close and last.close > last.open:
            direction = "long"
        elif fast < slow and last.high >= pullback > last.close and last.close < last.open:
            direction = "short"
        else:
            return StrategyEvaluation(None, "no trend-aligned closed-candle pullback")
        if configuration.volume_filter != "disabled":
            reference = sum((candle.volume for candle in entries[-21:-1]), Decimal(0)) / 20
            if last.volume < reference:
                return StrategyEvaluation(None, "signal volume below prior 20-bar mean")
        stop_level = target_level = None
        expected_entry = None
        required_ratio = max(configuration.reward_multiple, minimum_reward_to_risk)
        if configuration.protection_mode == "support_resistance":
            tick = rules.price_tick
            buy = direction == "long"
            entry = (
                _round_price(last.high, tick, ROUND_FLOOR) + tick
                if buy
                else _round_price(last.low, tick, ROUND_CEILING) - tick
            )
            expected_entry = adverse_fill_price(entry, buy, expected_entry_slippage, tick)
            levels = (
                *confirmed_levels(
                    entries,
                    entry_interval,
                    configuration.level_lookback_bars,
                    configuration.pivot_confirmation_bars,
                ),
                *confirmed_levels(
                    hourly,
                    trend_interval,
                    configuration.level_lookback_bars,
                    configuration.pivot_confirmation_bars,
                ),
            )
            protective = (
                sorted(
                    (
                        level
                        for level in levels
                        if level.kind == "support" and level.price < last.low
                    ),
                    key=lambda level: level.price,
                    reverse=True,
                )
                if buy
                else sorted(
                    (
                        level
                        for level in levels
                        if level.kind == "resistance" and level.price > last.high
                    ),
                    key=lambda level: level.price,
                )
            )
            if not protective:
                return StrategyEvaluation(None, "no confirmed protective support/resistance level")
            stop_level = protective[0]
            stop = (
                _round_price(stop_level.price, tick, ROUND_FLOOR) - tick
                if buy
                else _round_price(stop_level.price, tick, ROUND_CEILING) + tick
            )
            opposing = sorted(
                (
                    level
                    for level in levels
                    if level.kind == ("resistance" if buy else "support")
                    and (level.price > expected_entry if buy else level.price < expected_entry)
                ),
                key=lambda level: level.price,
                reverse=not buy,
            )
            target = Decimal(0)
            for level in opposing:
                candidate = (
                    _round_price(level.price, tick, ROUND_FLOOR) - tick
                    if buy
                    else _round_price(level.price, tick, ROUND_CEILING) + tick
                )
                reward = candidate - expected_entry if buy else expected_entry - candidate
                distance = abs(expected_entry - stop)
                cost_passes = (
                    configuration.cost_filter == "gross_only"
                    or net_reward_to_risk_passes(
                        expected_entry,
                        stop,
                        candidate,
                        buy,
                        expected_fee_fraction,
                        expected_entry_slippage,
                        tick,
                        required_ratio,
                    )
                )
                if distance > 0 and reward >= distance * required_ratio and cost_passes:
                    target_level, target = level, candidate
                    break
            if target_level is None:
                return StrategyEvaluation(
                    None,
                    "no confirmed target level meets cost-adjusted minimum reward-to-risk"
                    if configuration.cost_filter == "net_reward_risk"
                    else "no confirmed target level meets slippage-adjusted minimum reward-to-risk",
                )
        else:
            return _legacy_setup(
                symbol,
                hourly,
                entries,
                rules,
                configuration,
                as_of=as_of,
                entry_duration=entry_duration,
            )
        if min(entry, stop, target) <= 0:
            return StrategyEvaluation(None, "setup produces a non-positive protective price")
        if any(not rules.min_price <= price <= rules.max_price for price in (entry, stop, target)):
            return StrategyEvaluation(None, "setup prices exceed exchange limits")

    checksum = configuration.checksum
    identity = (
        f"{configuration.implementation}:{checksum}:{symbol}:{direction}:"
        f"{last.close_time.isoformat()}:{expected_entry}:{required_ratio}:{stop}:{target}:"
        f"{expected_fee_fraction}:{expected_entry_slippage}:{entry_interval}:{trend_interval}"
    )
    return StrategyEvaluation(
        EntrySetup(
            id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            symbol=symbol,
            direction=direction,
            strategy_version=configuration.implementation,
            configuration_checksum=checksum,
            configuration_json=configuration.model_dump_json(),
            signal_time=last.close_time,
            eligible_from=eligible_from,
            expires_at=eligible_from + entry_duration * configuration.entry_expiry_bars,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            time_stop_bars=configuration.effective_time_stop_bars,
            stop_level=stop_level,
            target_level=target_level,
            expected_entry_price=expected_entry,
            minimum_reward_to_risk=required_ratio,
            expected_fee_fraction=expected_fee_fraction
            if configuration.cost_filter == "net_reward_risk"
            else None,
            expected_exit_slippage=expected_entry_slippage,
        ),
        "confirmed support/resistance pullback setup",
    )


def _legacy_setup(
    symbol: str,
    hourly: Sequence[Kline],
    entries: Sequence[Kline],
    rules: SymbolRules,
    configuration: StrategyConfiguration,
    *,
    as_of: datetime,
    entry_duration: timedelta = timedelta(minutes=15),
) -> StrategyEvaluation:
    # Existing paper snapshots keep their original protection rules, not v2 levels.
    with localcontext() as context:
        context.prec = 40
        last = entries[-1]
        direction: Direction = (
            "long"
            if exponential_moving_average(
                [c.close for c in hourly], configuration.trend_fast_period
            )[-1]
            > exponential_moving_average(
                [c.close for c in hourly], configuration.trend_slow_period
            )[-1]
            else "short"
        )
        eligible_from = as_of
        atr = average_true_range(entries, configuration.atr_period)
        assert atr is not None
        swing = entries[-configuration.swing_period :]
        tick = rules.price_tick
        if direction == "long":
            entry = _round_price(last.high, tick, ROUND_FLOOR) + tick
            stop = _round_price(
                min(
                    min(candle.low for candle in swing) - tick,
                    entry - atr * configuration.atr_multiplier,
                ),
                tick,
                ROUND_FLOOR,
            )
            target = _round_price(
                entry + (entry - stop) * configuration.reward_multiple, tick, ROUND_CEILING
            )
        else:
            entry = _round_price(last.low, tick, ROUND_CEILING) - tick
            stop = _round_price(
                max(
                    max(candle.high for candle in swing) + tick,
                    entry + atr * configuration.atr_multiplier,
                ),
                tick,
                ROUND_CEILING,
            )
            target = _round_price(
                entry - (stop - entry) * configuration.reward_multiple, tick, ROUND_FLOOR
            )
        if min(entry, stop, target) <= 0:
            return StrategyEvaluation(None, "setup produces a non-positive protective price")
        if any(not rules.min_price <= price <= rules.max_price for price in (entry, stop, target)):
            return StrategyEvaluation(None, "setup prices exceed exchange limits")

    checksum = configuration.checksum
    identity = (
        f"{configuration.implementation}:{checksum}:{symbol}:{direction}:"
        f"{last.close_time.isoformat()}:{entry_duration}"
    )
    return StrategyEvaluation(
        setup=EntrySetup(
            id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            symbol=symbol,
            direction=direction,
            strategy_version=configuration.implementation,
            configuration_checksum=checksum,
            configuration_json=configuration.model_dump_json(),
            signal_time=last.close_time,
            eligible_from=eligible_from,
            expires_at=eligible_from + entry_duration * configuration.entry_expiry_bars,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            time_stop_bars=configuration.effective_time_stop_bars,
        ),
        reason="trend-aligned closed-candle pullback",
    )


def _validate_candles(candles: Sequence[Kline], interval: str) -> None:
    if find_candle_gaps(candles, interval):
        raise ValueError(f"{interval} strategy history contains missing candles")
    anchor = datetime(2000, 1, 1, tzinfo=UTC)
    seconds = int((next_candle_open(anchor, interval) - anchor).total_seconds())
    for candle in candles:
        stamp = candle.open_time.astimezone(UTC)
        if stamp.second or stamp.microsecond or (stamp.minute * 60 + stamp.hour * 3600) % seconds:
            raise ValueError("strategy candle opens are not aligned to the timeframe")
        _validate_numbers(
            (candle.open, candle.high, candle.low, candle.close, candle.volume), positive=False
        )
        if (
            min(candle.open, candle.high, candle.low, candle.close) <= 0
            or candle.high < max(candle.open, candle.close, candle.low)
            or candle.low > min(candle.open, candle.close, candle.high)
        ):
            raise ValueError("strategy candle OHLC values are invalid")


def _validate_numbers(values: Sequence[Decimal], *, positive: bool = True) -> None:
    for value in values:
        if (
            not isinstance(value, Decimal)
            or not value.is_finite()
            or value < 0
            or (positive and value == 0)
        ):
            raise ValueError("indicator inputs must be finite valid Decimals")


def _round_price(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick
