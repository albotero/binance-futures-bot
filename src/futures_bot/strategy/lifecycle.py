"""Conservative bar rules shared by backtesting and simulated execution."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from futures_bot.exchange import Kline
from futures_bot.strategy.pullback import EntrySetup


@dataclass(frozen=True, slots=True)
class EntryResult:
    status: Literal["pending", "expired", "invalidated", "triggered"]
    fill_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExitResult:
    reason: Literal["stop", "target", "time"]
    price: Decimal


def evaluate_entry(setup: EntrySetup, candle: Kline) -> EntryResult:
    if candle.open_time < setup.eligible_from:
        return EntryResult("pending")
    if candle.open_time >= setup.expires_at:
        return EntryResult("expired")
    triggered = (
        candle.high >= setup.entry_price
        if setup.direction == "long"
        else candle.low <= setup.entry_price
    )
    invalidated = (
        candle.low <= setup.stop_price
        if setup.direction == "long"
        else candle.high >= setup.stop_price
    )
    if triggered:
        price = (
            max(candle.open, setup.entry_price)
            if setup.direction == "long"
            else min(candle.open, setup.entry_price)
        )
        return EntryResult("triggered", price)
    if invalidated:
        return EntryResult("invalidated")
    return EntryResult("pending")


def evaluate_exit(
    setup: EntrySetup,
    candle: Kline,
    *,
    bars_held: int,
    entry_bar: bool = False,
) -> ExitResult | None:
    if type(bars_held) is not int or bars_held < 1:
        raise ValueError("bars_held must be a positive integer")
    stop_hit = (
        candle.low <= setup.stop_price
        if setup.direction == "long"
        else candle.high >= setup.stop_price
    )
    target_hit = (
        candle.high >= setup.target_price
        if setup.direction == "long"
        else candle.low <= setup.target_price
    )
    if stop_hit:
        # The entry bar's open preceded the trigger; it is not an exit quote.
        price = setup.stop_price
        if not entry_bar:
            price = (
                min(candle.open, price) if setup.direction == "long" else max(candle.open, price)
            )
        return ExitResult("stop", price)
    if target_hit:
        return ExitResult("target", setup.target_price)
    if bars_held >= setup.time_stop_bars:
        return ExitResult("time", candle.close)
    return None
