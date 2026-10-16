from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from futures_bot.backtesting import BacktestConfiguration, BacktestResult, run_backtest
from futures_bot.exchange import FundingRate, Kline, SymbolRules
from futures_bot.exchange.leverage import LeverageBracket, LeverageSchedule
from futures_bot.risk import RiskPolicy
from futures_bot.risk.configuration import RiskConfiguration, SymbolConfiguration
from futures_bot.strategy import StrategyConfiguration, evaluate_pullback

START = datetime(2026, 10, 16, 12, tzinfo=UTC)
D = Decimal


def bar(time: datetime, minutes: int, close: int) -> Kline:
    price = D(close)
    return Kline(
        open_time=time,
        close_time=time + timedelta(minutes=minutes, milliseconds=-1),
        open=price - 1,
        high=price + 2,
        low=price - 2,
        close=price,
        volume=D(1),
        quote_volume=price,
        trade_count=1,
        taker_buy_volume=D(0),
        taker_buy_quote_volume=D(0),
    )


@pytest.fixture
def rules() -> SymbolRules:
    return SymbolRules(
        symbol="BTCUSDC",
        quote_asset="USDC",
        margin_asset="USDC",
        price_tick=D("0.1"),
        min_price=D("0.1"),
        max_price=D("1000000"),
        quantity_step=D("0.1"),
        min_quantity=D("0.1"),
        max_quantity=D("10000"),
        market_quantity_step=None,
        market_min_quantity=None,
        market_max_quantity=None,
        min_notional=D("5"),
    )


@pytest.fixture
def risk() -> RiskConfiguration:
    return RiskConfiguration(
        policy=RiskPolicy(
            basis="equity",
            risk_fraction=D("0.01"),
            margin_reserve_fraction=D("0.1"),
            maximum_position_notional=D("100000"),
            maximum_total_exposure=D("100000"),
            maximum_aggregate_risk=D("1000"),
            maximum_daily_loss=D("300"),
            maximum_drawdown_fraction=D("0.2"),
            maximum_stop_distance_fraction=D("0.1"),
            minimum_reward_to_risk=D("1"),
            maximum_open_positions=1,
        ),
        symbols=(SymbolConfiguration(symbol="BTCUSDC"),),
    )


@pytest.fixture
def history() -> tuple[tuple[Kline, ...], tuple[Kline, ...]]:
    hourly = tuple(bar(START - timedelta(hours=220 - i), 60, 200 + i) for i in range(220))
    warmup = tuple(bar(START - timedelta(minutes=15 * (30 - i)), 15, 400 + i) for i in range(30))
    warmup = (*warmup[:-1], replace(warmup[-1], low=D(415)))
    execution = tuple(
        replace(bar(START + timedelta(minutes=15 * i), 15, 433), low=D(432), open=D(433))
        for i in range(4)
    )
    return hourly, warmup + execution


def simulate(history, rules, risk, *, funding=(), configuration=None, strategy=None):
    return run_backtest(
        "BTCUSDC",
        *history,
        funding,
        rules,
        strategy or StrategyConfiguration(),
        risk,
        configuration
        or BacktestConfiguration(starting_balance="10000", fee_fraction="0", slippage_fraction="0"),
        start=START,
        end=START + timedelta(hours=1),
    )


def test_reproducible_decimal_accounting_and_serialization(history, rules, risk) -> None:
    result = simulate(history, rules, risk)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "end_of_data"
    assert trade.entry_time == START
    assert trade.entry_price == D(433)
    assert trade.gross_pnl == 0
    assert result.net_pnl == result.final_equity - result.starting_balance == 0
    assert result.quote_asset == "USDC"
    assert len(result.dataset_checksum) == 64
    assert len(result.equity_curve) == 5
    assert result == simulate(history, rules, risk)
    assert BacktestResult.model_validate_json(result.model_dump_json()) == result


def test_fees_slippage_and_funding_reduce_equity_exactly(history, rules, risk) -> None:
    configuration = BacktestConfiguration(
        starting_balance="10000", fee_fraction="0.001", slippage_fraction="0.001"
    )
    funding = (FundingRate("BTCUSDC", START + timedelta(minutes=15), D("0.001"), D(433)),)
    result = simulate(history, rules, risk, funding=funding, configuration=configuration)
    trade = result.trades[0]
    assert trade.entry_price == D("433.5")
    assert trade.exit_price == D("432.5")
    assert trade.funding_pnl == -trade.quantity * D("433") * D("0.001")
    assert trade.fees == trade.quantity * (trade.entry_price + trade.exit_price) * D("0.001")
    assert trade.net_pnl == trade.gross_pnl - trade.fees + trade.funding_pnl
    assert result.net_pnl == trade.net_pnl
    assert result.final_equity == D(10000) + trade.net_pnl
    assert result.maximum_drawdown_fraction > 0
    assert result.profit_factor == 0


def test_stop_first_ambiguity_and_adverse_open_gap(history, rules, risk) -> None:
    hourly, entries = history
    setup = evaluate_pullback(
        "BTCUSDC", hourly, entries, rules, StrategyConfiguration(), as_of=START
    ).setup
    assert setup is not None
    second = replace(
        entries[-3],
        open=setup.stop_price - 1,
        low=setup.stop_price - 2,
        high=setup.target_price + 1,
        close=setup.stop_price,
    )
    result = simulate((hourly, (*entries[:-3], second, *entries[-2:])), rules, risk)
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.exit_price == setup.stop_price - 1
    assert trade.net_pnl < 0
    assert result.win_rate == 0


def test_time_stop_uses_shared_lifecycle(history, rules, risk) -> None:
    result = simulate(history, rules, risk, strategy=StrategyConfiguration(time_stop_bars=2))
    assert result.trades[0].exit_reason == "time"
    assert result.trades[0].exit_time == START + timedelta(minutes=30, milliseconds=-1)


def test_future_bars_change_provenance_not_prior_fills(history, rules, risk) -> None:
    hourly, entries = history
    baseline = simulate(history, rules, risk, strategy=StrategyConfiguration(time_stop_bars=1))
    future = replace(entries[-1], high=D(999), close=D(998))
    changed = simulate(
        (hourly, (*entries[:-1], future)),
        rules,
        risk,
        strategy=StrategyConfiguration(time_stop_bars=1),
    )
    assert baseline.trades[0] == changed.trades[0]
    assert baseline.dataset_checksum != changed.dataset_checksum


def test_short_funding_sign_is_reversed(history, rules, risk) -> None:
    def mirror(candle: Kline) -> Kline:
        return replace(
            candle,
            open=1000 - candle.open,
            high=1000 - candle.low,
            low=1000 - candle.high,
            close=1000 - candle.close,
        )

    mirrored = tuple(tuple(map(mirror, candles)) for candles in history)
    rate = FundingRate("BTCUSDC", START + timedelta(minutes=15), D("0.001"), D(567))
    result = simulate(mirrored, rules, risk, funding=(rate,))
    trade = result.trades[0]
    assert trade.direction == "short"
    assert trade.funding_pnl == trade.quantity * D(567) * D("0.001")
    assert result.net_pnl == trade.net_pnl > 0


def test_quote_and_funding_mismatches_fail_closed(history, rules, risk) -> None:
    with pytest.raises(ValueError, match="quote"):
        simulate(history, replace(rules, margin_asset="USDT"), risk)
    with pytest.raises(ValueError, match="funding symbol"):
        simulate(history, rules, risk, funding=(FundingRate("BTCUSDT", START, D(0), D(433)),))
    with pytest.raises(ValueError, match="strictly ordered"):
        rate = FundingRate("BTCUSDC", START, D(0), D(433))
        simulate(history, rules, risk, funding=(rate, rate))


def test_missing_coverage_and_warmup_are_errors_not_empty_success(history, rules, risk) -> None:
    hourly, entries = history
    with pytest.raises(ValueError, match="warm-up"):
        simulate((hourly[-10:], entries), rules, risk)
    with pytest.raises(ValueError, match="complete"):
        simulate((hourly, entries[:-1]), rules, risk)
    with pytest.raises(ValueError, match="missing candles"):
        simulate((hourly, (*entries[:-3], *entries[-2:])), rules, risk)
    with pytest.raises(ValueError, match="stale"):
        simulate((hourly[:-1], entries), rules, risk)


def test_risk_rejections_are_exposed_and_do_not_create_trades(history, rules, risk) -> None:
    risk = RiskConfiguration(
        policy=replace(risk.policy, maximum_stop_distance_fraction=D("0.001")), symbols=risk.symbols
    )
    result = simulate(history, rules, risk)
    assert not result.trades
    assert result.risk_rejections
    assert result.net_pnl == 0


def test_simulation_supports_50x_without_enlarging_position_risk(history, rules, risk):
    baseline = simulate(history, rules, risk)
    high_risk = RiskConfiguration(
        policy=replace(risk.policy, maximum_leverage=50),
        symbols=(SymbolConfiguration(symbol="BTCUSDC", leverage=50),),
    )
    high = simulate(history, rules, high_risk)
    assert high.trades and baseline.trades
    assert high.trades[0].quantity == baseline.trades[0].quantity
    assert not any("leverage" in str(item).lower() for item in high.risk_rejections)


def test_simulation_applies_verified_125x_notional_bracket(history, rules, risk):
    schedule = LeverageSchedule(
        symbol="BTCUSDC",
        fetched_at=START,
        brackets=(
            LeverageBracket(
                initial_leverage=125,
                notional_floor="0",
                notional_cap="1000",
            ),
        ),
    )
    configured = RiskConfiguration(
        policy=replace(risk.policy, maximum_leverage=5),
        symbols=(
            SymbolConfiguration(
                symbol="BTCUSDC",
                leverage=125,
                leverage_schedule=schedule,
            ),
        ),
    )
    result = simulate(history, rules, configured)
    assert result.trades
    assert result.trades[0].entry_price * result.trades[0].quantity <= D("1000")
    assert (
        result.configuration_snapshot["risk"]["symbols"][0]["leverage_schedule"]["brackets"][0][
            "notional_cap"
        ]
        == "1000"
    )
    unverified = RiskConfiguration(
        policy=risk.policy,
        symbols=(SymbolConfiguration(symbol="BTCUSDC", leverage=125),),
    )
    blocked = simulate(history, rules, unverified)
    assert not blocked.trades
    assert any("leverage" in str(item).lower() for item in blocked.risk_rejections)


def test_decimal_context_does_not_change_research_results(history, rules, risk) -> None:
    baseline = simulate(history, rules, risk)
    with localcontext() as context:
        context.prec = 8
        assert simulate(history, rules, risk) == baseline


def test_liquidation_stress_halts_and_accounts_for_extra_fees(history, rules, risk) -> None:
    hourly, entries = history
    risk = RiskConfiguration(
        policy=replace(
            risk.policy,
            risk_fraction=D("0.2"),
            maximum_aggregate_risk=D("100000"),
            maximum_daily_loss=D("100000"),
        ),
        symbols=(SymbolConfiguration(symbol="BTCUSDC", leverage=20),),
    )
    adverse = replace(entries[-3], low=D("0.1"), close=D("0.1"))
    result = simulate((hourly, (*entries[:-3], adverse, *entries[-2:])), rules, risk)
    trade = result.trades[0]
    assert result.halted
    assert len(result.trades) == 1
    assert trade.exit_reason == "conservative_liquidation"
    assert trade.exit_price == D("0.1")
    assert trade.fees == trade.quantity * trade.exit_price * D("0.005")
    assert result.final_equity == result.starting_balance + trade.net_pnl


def test_funding_at_entry_boundary_is_not_charged_to_new_position(history, rules, risk) -> None:
    funding = (FundingRate("BTCUSDC", START, D("0.01"), D(433)),)
    result = simulate(history, rules, risk, funding=funding)
    assert result.trades[0].funding_pnl == 0


@pytest.mark.parametrize(
    ("offset", "rate", "expected_rate"),
    [
        (timedelta(milliseconds=1), "0.001", "-0.001"),
        (timedelta(milliseconds=1), "-0.001", "0"),
        (timedelta(minutes=15, milliseconds=1), "-0.001", "0.001"),
    ],
)
def test_exact_intrabar_funding_timestamps_use_conservative_attribution(
    history, rules, risk, offset, rate, expected_rate
) -> None:
    funding = (FundingRate("BTCUSDC", START + offset, D(rate), D(433)),)
    result = simulate(history, rules, risk, funding=funding)
    trade = result.trades[0]
    assert trade.funding_pnl == trade.quantity * D(433) * D(expected_rate)
    assert result.net_pnl == trade.net_pnl


def test_entry_fees_cannot_overdraw_margin_reserve(history, rules, risk) -> None:
    risk = RiskConfiguration(
        policy=replace(
            risk.policy,
            risk_fraction=D("0.5"),
            maximum_aggregate_risk=D("100000"),
            maximum_daily_loss=D("100000"),
        ),
        symbols=risk.symbols,
    )
    result = simulate(
        history,
        rules,
        risk,
        configuration=BacktestConfiguration(
            starting_balance="10000", fee_fraction="0.01", slippage_fraction="0"
        ),
    )
    assert not result.trades
    assert any("entry fee" in reason for _, reasons in result.risk_rejections for reason in reasons)


def test_saved_result_and_csv_round_trip(history, rules, risk, tmp_path) -> None:
    import csv
    import io

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from futures_bot.backtesting.results import export_trades_csv, save_backtest_result
    from futures_bot.persistence.database import create_schema_for_tests, make_engine
    from futures_bot.persistence.models import AuditEvent, BacktestRun

    result = simulate(history, rules, risk)
    engine = make_engine(f"sqlite:///{tmp_path / 'research.sqlite3'}")
    create_schema_for_tests(engine)
    try:
        with Session(engine) as session:
            row = save_backtest_result(session, result, actor="research-operator")
            run_id = row.id
            session.commit()
        with Session(engine) as session:
            loaded = session.get(BacktestRun, run_id)
            assert loaded is not None
            assert loaded.status == "completed"
            assert BacktestResult.model_validate(loaded.metrics) == result
            assert loaded.dataset_metadata["quote_asset"] == "USDC"
            assert len(session.scalars(select(AuditEvent)).all()) == 2
        csv_rows = list(csv.DictReader(io.StringIO(export_trades_csv(result))))
        assert len(csv_rows) == 1
        assert D(csv_rows[0]["quantity"]) == result.trades[0].quantity
        assert csv_rows[0]["symbol"] == "BTCUSDC"
        with Session(engine) as session:
            save_backtest_result(session, result, actor="research-operator")
            session.rollback()
            assert len(session.scalars(select(BacktestRun)).all()) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"starting_balance": 1000},
        {"starting_balance": "NaN"},
        {"starting_balance": "1000", "fee_fraction": True},
        {"starting_balance": "1000", "reporting_timezone": "not/a/timezone"},
        {"starting_balance": "1000", "slippage_fraction": "0.5"},
    ],
)
def test_invalid_simulation_assumptions_are_rejected(values) -> None:
    with pytest.raises(ValidationError):
        BacktestConfiguration.model_validate(values)
