from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from futures_bot.exchange import Kline
from futures_bot.strategy import EntrySetup, StrategyConfiguration
from futures_bot.strategy.lifecycle import evaluate_entry, evaluate_exit

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def setup(short: bool = False) -> EntrySetup:
    config = StrategyConfiguration()
    return EntrySetup(
        id="fixture",
        symbol="BTCUSDT",
        direction="short" if short else "long",
        strategy_version="ema-pullback-v1",
        configuration_checksum=config.checksum,
        configuration_json=config.model_dump_json(),
        signal_time=NOW - timedelta(milliseconds=1),
        eligible_from=NOW,
        expires_at=NOW + timedelta(minutes=45),
        entry_price=Decimal("100"),
        stop_price=Decimal("102") if short else Decimal("98"),
        target_price=Decimal("96") if short else Decimal("104"),
        time_stop_bars=8,
    )


def candle(
    open_price: str = "100", high: str = "101", low: str = "99", close: str = "100"
) -> Kline:
    return Kline(
        open_time=NOW,
        close_time=NOW + timedelta(minutes=15, milliseconds=-1),
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
        quote_volume=Decimal("100"),
        trade_count=1,
        taker_buy_volume=Decimal("0"),
        taker_buy_quote_volume=Decimal("0"),
    )


@pytest.mark.parametrize("short", [False, True])
def test_entry_valid_for_three_bars_and_never_on_signal_bar(short: bool) -> None:
    signal = setup(short)
    for minutes in (0, 15, 30):
        current = replace(candle(), open_time=NOW + timedelta(minutes=minutes))
        assert evaluate_entry(signal, current).status == "triggered"
    assert (
        evaluate_entry(signal, replace(candle(), open_time=NOW + timedelta(minutes=45))).status
        == "expired"
    )
    assert (
        evaluate_entry(signal, replace(candle(), open_time=NOW - timedelta(minutes=15))).status
        == "pending"
    )


def test_adverse_gap_prices_are_used_for_entries_and_stops() -> None:
    assert evaluate_entry(setup(), candle("101", "103", "100", "102")).fill_price == Decimal("101")
    assert evaluate_entry(setup(True), candle("99", "100", "97", "98")).fill_price == Decimal("99")
    long_exit = evaluate_exit(setup(), candle("96", "99", "95", "97"), bars_held=2)
    short_exit = evaluate_exit(setup(True), candle("104", "105", "101", "103"), bars_held=2)
    assert long_exit is not None and long_exit.price == Decimal("96")
    assert short_exit is not None and short_exit.price == Decimal("104")


@pytest.mark.parametrize("short", [False, True])
def test_ambiguous_bar_always_resolves_stop_first(short: bool) -> None:
    current = candle("100", "106", "94", "100")
    assert evaluate_entry(setup(short), current).status == "triggered"
    result = evaluate_exit(setup(short), current, bars_held=1, entry_bar=True)
    assert result is not None
    assert result.reason == "stop"
    assert result.price == setup(short).stop_price


def test_stop_before_breakout_invalidates_pending_setup() -> None:
    assert evaluate_entry(setup(), candle("99", "99.5", "97", "98")).status == "invalidated"
    assert evaluate_entry(setup(True), candle("101", "103", "100.5", "102")).status == "invalidated"


def test_time_stop_on_eighth_bar_and_protection_takes_priority() -> None:
    assert evaluate_exit(setup(), candle(), bars_held=7) is None
    timed = evaluate_exit(setup(), candle(), bars_held=8)
    assert timed is not None and timed.reason == "time" and timed.price == Decimal("100")
    stopped = evaluate_exit(setup(), candle(low="97"), bars_held=8)
    assert stopped is not None and stopped.reason == "stop"
    target = evaluate_exit(setup(), candle(high="105"), bars_held=8)
    assert target is not None and target.reason == "target"
