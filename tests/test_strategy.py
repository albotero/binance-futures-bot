from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from futures_bot.exchange import Kline, SymbolRules
from futures_bot.strategy import StrategyConfiguration, evaluate_pullback
from futures_bot.strategy.pullback import average_true_range, exponential_moving_average

AS_OF = datetime(2026, 10, 16, 12, tzinfo=UTC)


@pytest.fixture
def rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDT",
        quote_asset="USDT",
        margin_asset="USDT",
        price_tick=Decimal("0.1"),
        min_price=Decimal("0.1"),
        max_price=Decimal("1000000"),
        quantity_step=Decimal("0.1"),
        min_quantity=Decimal("0.1"),
        max_quantity=Decimal("1000"),
        market_quantity_step=None,
        market_min_quantity=None,
        market_max_quantity=None,
        min_notional=Decimal("5"),
    )


def bar(time: datetime, interval: int, close: int) -> Kline:
    price = Decimal(close)
    return Kline(
        open_time=time,
        close_time=time + timedelta(minutes=interval, milliseconds=-1),
        open=price - 1,
        high=price + 2,
        low=price - 2,
        close=price,
        volume=Decimal("1"),
        quote_volume=price,
        trade_count=1,
        taker_buy_volume=Decimal("0"),
        taker_buy_quote_volume=Decimal("0"),
    )


def histories(short: bool = False) -> tuple[tuple[Kline, ...], tuple[Kline, ...]]:
    hourly = tuple(
        bar(AS_OF - timedelta(hours=220 - index), 60, 200 + index) for index in range(220)
    )
    entries = tuple(
        bar(AS_OF - timedelta(minutes=15 * (30 - index)), 15, 400 + index) for index in range(30)
    )
    last = replace(entries[-1], low=Decimal("415"))
    entries = (*entries[:-1], last)
    if short:

        def mirror(candle: Kline) -> Kline:
            pivot = Decimal("1000")
            return replace(
                candle,
                open=pivot - candle.open,
                high=pivot - candle.low,
                low=pivot - candle.high,
                close=pivot - candle.close,
            )

        return tuple(map(mirror, hourly)), tuple(map(mirror, entries))
    return hourly, entries


def test_ema_and_wilder_atr_match_known_values() -> None:
    assert exponential_moving_average(tuple(map(Decimal, ("1", "2", "3", "4", "5"))), 3) == (
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
    )
    candles = tuple(bar(AS_OF + timedelta(minutes=15 * i), 15, 100 + i) for i in range(5))
    assert average_true_range(candles, 3) == Decimal("4")
    assert average_true_range(candles[:3], 3) is None


@pytest.mark.parametrize("short", [False, True])
def test_closed_pullback_produces_versioned_tick_aligned_setup(
    rules: SymbolRules, short: bool
) -> None:
    hourly, entries = histories(short)
    config = StrategyConfiguration()
    result = evaluate_pullback("BTCUSDT", hourly, entries, rules, config, as_of=AS_OF)
    setup = result.setup
    assert setup is not None
    assert setup.direction == ("short" if short else "long")
    assert setup.eligible_from == AS_OF
    assert setup.expires_at == AS_OF + timedelta(minutes=45)
    assert setup.time_stop_bars == 8
    assert setup.signal_time < setup.eligible_from
    assert setup.configuration_checksum == config.checksum
    assert StrategyConfiguration.model_validate_json(setup.configuration_json) == config
    assert all(
        price % rules.price_tick == 0
        for price in (setup.entry_price, setup.stop_price, setup.target_price)
    )
    assert abs(setup.target_price - setup.entry_price) >= (
        abs(setup.stop_price - setup.entry_price) * 2
    )
    if short:
        assert setup.entry_price < entries[-1].low
        assert setup.stop_price > max(candle.high for candle in entries[-5:])
    else:
        assert setup.entry_price > entries[-1].high
        assert setup.stop_price < min(candle.low for candle in entries[-5:])


def test_unclosed_and_future_candles_cannot_change_signal(rules: SymbolRules) -> None:
    hourly, entries = histories()
    config = StrategyConfiguration()
    baseline = evaluate_pullback("BTCUSDT", hourly, entries, rules, config, as_of=AS_OF)
    future_hourly = (*hourly, bar(AS_OF, 60, 1))
    future_entries = (*entries, bar(AS_OF, 15, 1), bar(AS_OF + timedelta(minutes=15), 15, 2))
    assert (
        evaluate_pullback("BTCUSDT", future_hourly, future_entries, rules, config, as_of=AS_OF)
        == baseline
    )
    assert baseline.setup is not None
    assert (
        evaluate_pullback(
            "BTCUSDT", hourly, entries, rules, config, as_of=entries[-1].close_time
        ).setup
        is None
    )


def test_warmup_and_stale_history_are_not_signals(rules: SymbolRules) -> None:
    hourly, entries = histories()
    config = StrategyConfiguration()
    assert (
        evaluate_pullback("BTCUSDT", hourly[:10], entries, rules, config, as_of=AS_OF).reason
        == "indicator warm-up is incomplete"
    )
    assert (
        evaluate_pullback("BTCUSDT", hourly, entries[:-1], rules, config, as_of=AS_OF).reason
        == "latest closed entry candle is stale"
    )
    assert (
        evaluate_pullback("BTCUSDT", hourly[:-1], entries, rules, config, as_of=AS_OF).reason
        == "latest closed trend candle is stale"
    )


def test_gaps_duplicates_and_invalid_ohlc_fail_explicitly(rules: SymbolRules) -> None:
    hourly, entries = histories()
    config = StrategyConfiguration()
    for history in (entries[:10] + entries[11:], entries + entries[-1:]):
        with pytest.raises(ValueError):
            evaluate_pullback("BTCUSDT", hourly, history, rules, config, as_of=AS_OF)
    with pytest.raises(ValueError, match="OHLC"):
        evaluate_pullback(
            "BTCUSDT",
            hourly,
            (*entries[:-1], replace(entries[-1], high=Decimal("1"))),
            rules,
            config,
            as_of=AS_OF,
        )


def test_flat_trend_or_failed_reclaim_produces_no_setup(rules: SymbolRules) -> None:
    hourly, entries = histories()
    flat = tuple(
        replace(candle, open=Decimal(400), high=Decimal(401), low=Decimal(399), close=Decimal(400))
        for candle in hourly
    )
    assert (
        evaluate_pullback(
            "BTCUSDT", flat, entries, rules, StrategyConfiguration(), as_of=AS_OF
        ).setup
        is None
    )
    assert (
        evaluate_pullback(
            "BTCUSDT",
            hourly,
            (*entries[:-1], replace(entries[-1], open=Decimal("430"))),
            rules,
            StrategyConfiguration(),
            as_of=AS_OF,
        ).setup
        is None
    )


def test_configuration_changes_have_distinct_setup_identity(rules: SymbolRules) -> None:
    hourly, entries = histories()
    first = evaluate_pullback(
        "BTCUSDT", hourly, entries, rules, StrategyConfiguration(), as_of=AS_OF
    )
    second = evaluate_pullback(
        "BTCUSDT",
        hourly,
        entries,
        rules,
        StrategyConfiguration(reward_multiple=Decimal("3")),
        as_of=AS_OF,
    )
    assert first.setup is not None and second.setup is not None
    assert first.setup.id != second.setup.id
    assert first.setup.configuration_checksum != second.setup.configuration_checksum


@pytest.mark.parametrize(
    "values",
    [
        {"trend_fast_period": 200},
        {"entry_expiry_bars": True},
        {"atr_multiplier": 1.5},
        {"atr_multiplier": "NaN"},
        {"reward_multiple": "0"},
        {"unknown": 1},
    ],
)
def test_strategy_configuration_is_strict(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        StrategyConfiguration.model_validate(values)
