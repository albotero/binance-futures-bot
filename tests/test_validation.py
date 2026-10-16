from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from futures_bot.backtesting import BacktestConfiguration, BacktestResult
from futures_bot.backtesting.engine import EquityPoint, SimulatedTrade
from futures_bot.backtesting.results import save_validation_result
from futures_bot.backtesting.validation import (
    ResearchWindow,
    SearchPolicy,
    ValidationPlan,
    ValidationResult,
    WalkForwardFold,
    run_walk_forward_search,
)
from futures_bot.exchange import FundingRate, Kline, SymbolRules
from futures_bot.persistence.database import create_schema_for_tests, make_engine
from futures_bot.persistence.models import ApplicationConfiguration, AuditEvent, BacktestRun
from futures_bot.risk import RiskPolicy
from futures_bot.risk.configuration import RiskConfiguration, SymbolConfiguration
from futures_bot.strategy import StrategyConfiguration

D = Decimal
START = datetime(2026, 10, 1, tzinfo=UTC)


def window(start: int, end: int) -> ResearchWindow:
    return ResearchWindow(start=START + timedelta(hours=start), end=START + timedelta(hours=end))


@pytest.fixture
def plan() -> ValidationPlan:
    return ValidationPlan(
        folds=(
            WalkForwardFold(training=window(0, 24), validation=window(27, 48)),
            WalkForwardFold(training=window(0, 48), validation=window(51, 72)),
        ),
        holdout=window(75, 96),
        purge_bars=12,
    )


@pytest.fixture
def risk() -> RiskConfiguration:
    return RiskConfiguration(
        policy=RiskPolicy(
            basis="equity",
            risk_fraction=D("0.01"),
            margin_reserve_fraction=D("0.1"),
            maximum_position_notional=D("10000"),
            maximum_total_exposure=D("10000"),
            maximum_aggregate_risk=D("100"),
            maximum_daily_loss=D("300"),
            maximum_drawdown_fraction=D("0.2"),
            maximum_stop_distance_fraction=D("0.1"),
            minimum_reward_to_risk=D("1"),
            maximum_open_positions=1,
        ),
        symbols=(SymbolConfiguration(symbol="BTCUSDC"),),
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
        max_quantity=D("1000"),
        market_quantity_step=None,
        market_min_quantity=None,
        market_max_quantity=None,
        min_notional=D(5),
    )


def candles(minutes: int, warmup: int) -> tuple[Kline, ...]:
    return tuple(
        Kline(
            open_time=START + timedelta(minutes=minutes * index),
            close_time=START + timedelta(minutes=minutes * (index + 1), milliseconds=-1),
            open=D(429),
            high=D(432),
            low=D(415),
            close=D(430),
            volume=D(1),
            quote_volume=D(430),
            trade_count=1,
            taker_buy_volume=D(0),
            taker_buy_quote_volume=D(0),
        )
        for index in range(-warmup, 96 * 60 // minutes)
    )


@pytest.fixture
def inputs(risk, rules, plan):
    return {
        "symbol": "BTCUSDC",
        "hourly_candles": candles(60, 220),
        "entry_candles": candles(15, 40),
        "funding_rates": tuple(
            FundingRate("BTCUSDC", START + timedelta(hours=i), D("0.001"), D(430))
            for i in range(0, 96, 8)
        ),
        "rules": rules,
        "baseline": StrategyConfiguration(),
        "candidates": (StrategyConfiguration(reward_multiple="3"),),
        "risk": risk,
        "simulation": BacktestConfiguration(starting_balance="10000"),
        "plan": plan,
        "policy": SearchPolicy(minimum_training_trades=1, minimum_validation_trades=1),
    }


def fake_result(symbol, hourly, entries, funding, rules, strategy, risk, simulation, *, start, end):
    assert all(candle.close_time < end for candle in (*hourly, *entries))
    assert all(start <= rate.funding_time < end for rate in funding)
    pnl = strategy.reward_multiple * 100
    trade = SimulatedTrade(
        setup_id=strategy.checksum,
        symbol=symbol,
        direction="long",
        entry_time=start,
        exit_time=end - timedelta(milliseconds=1),
        entry_price=D(100),
        exit_price=D(101),
        quantity=pnl,
        leverage=1,
        gross_pnl=pnl,
        fees=D(0),
        funding_pnl=D(0),
        net_pnl=pnl,
        exit_reason="end_of_data",
    )
    return BacktestResult(
        symbol=symbol,
        quote_asset="USDC",
        start=start,
        end=end,
        dataset_checksum=strategy.checksum,
        configuration_snapshot={"strategy": strategy.model_dump(mode="json")},
        trades=(trade,),
        equity_curve=(
            EquityPoint(time=start, equity=simulation.starting_balance, drawdown_fraction=D(0)),
            EquityPoint(
                time=end,
                equity=simulation.starting_balance + pnl,
                drawdown_fraction=D("0.01"),
            ),
        ),
        risk_rejections=(),
        starting_balance=simulation.starting_balance,
        final_equity=simulation.starting_balance + pnl,
        net_pnl=pnl,
        total_fees=D(0),
        funding_pnl=D(0),
        maximum_drawdown_fraction=D("0.01"),
        win_rate=D(1),
        profit_factor=None,
        expectancy=pnl,
        halted=False,
        limitations=(),
    )


@pytest.fixture
def runner(monkeypatch):
    calls = []

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return fake_result(*args, **kwargs)

    monkeypatch.setattr("futures_bot.backtesting.validation.run_backtest", tracked)
    return calls


def test_selection_is_reproducible_bounded_and_uses_one_fixed_risk_policy(inputs, runner):
    report = run_walk_forward_search(**inputs)
    assert report.selected_checksum == inputs["candidates"][0].checksum
    assert report.selected_holdout is not None
    assert report.selected_holdout.start == inputs["plan"].holdout.start
    assert report.backtests_run == len(runner) == 10
    assert all(call[0][6] is inputs["risk"] for call in runner)
    assert [kwargs["start"] for _, kwargs in runner[-2:]] == [inputs["plan"].holdout.start] * 2
    assert ValidationResult.model_validate_json(report.model_dump_json()) == report
    with localcontext() as context:
        context.prec = 6
        assert run_walk_forward_search(**inputs) == report


def test_holdout_losses_cannot_change_the_frozen_selection(inputs, monkeypatch):
    def losing_holdout(*args, **kwargs):
        result = fake_result(*args, **kwargs)
        if kwargs["start"] == inputs["plan"].holdout.start:
            return result.model_copy(update={"net_pnl": D("-9999"), "halted": True})
        return result

    monkeypatch.setattr("futures_bot.backtesting.validation.run_backtest", losing_holdout)
    report = run_walk_forward_search(**inputs)
    assert report.selected_checksum == inputs["candidates"][0].checksum
    assert report.selected_holdout.halted
    assert report.selected_holdout.net_pnl == D("-9999")


def test_worst_fold_score_beats_a_high_average_with_an_unstable_fold(inputs, monkeypatch):
    def unstable(*args, **kwargs):
        result = fake_result(*args, **kwargs)
        if (
            args[5].reward_multiple == 3
            and kwargs["start"] == inputs["plan"].folds[1].validation.start
        ):
            return result.model_copy(update={"net_pnl": D(1)})
        return result

    monkeypatch.setattr("futures_bot.backtesting.validation.run_backtest", unstable)
    report = run_walk_forward_search(**inputs)
    assert report.selected_checksum == inputs["baseline"].checksum
    assert report.selected_holdout is report.baseline_holdout
    assert report.backtests_run == 9


@pytest.mark.parametrize(
    "update, reason",
    [
        ({"trades": ()}, "insufficient trades"),
        ({"halted": True}, "simulation halted"),
        ({"maximum_drawdown_fraction": D("0.3")}, "drawdown"),
        ({"net_pnl": D(0)}, "return"),
    ],
)
def test_ineligible_trials_remain_in_report_without_a_selected_strategy(
    inputs, monkeypatch, update, reason
):
    def rejected(*args, **kwargs):
        return fake_result(*args, **kwargs).model_copy(update=update)

    monkeypatch.setattr("futures_bot.backtesting.validation.run_backtest", rejected)
    report = run_walk_forward_search(**inputs)
    assert report.selected_checksum is None
    assert report.selected_holdout is None
    assert len(report.candidates) == 2
    assert all(not trial.eligible for trial in report.candidates)
    assert any(reason in item for item in report.candidates[0].rejection_reasons)
    assert report.backtests_run == 9


def test_exact_score_ties_are_independent_of_candidate_order(inputs, monkeypatch):
    def tied(*args, **kwargs):
        return fake_result(*args, **kwargs).model_copy(update={"net_pnl": D(100)})

    monkeypatch.setattr("futures_bot.backtesting.validation.run_backtest", tied)
    candidates = (*inputs["candidates"], StrategyConfiguration(reward_multiple="4"))
    first = run_walk_forward_search(**{**inputs, "candidates": candidates})
    second = run_walk_forward_search(**{**inputs, "candidates": candidates[::-1]})
    assert first.selected_checksum == second.selected_checksum
    assert first.selected_checksum == min(candidate.checksum for candidate in first.candidates)


def test_duplicate_candidates_horizon_and_budget_fail_before_running(inputs, runner):
    for updates, message in (
        ({"candidates": (inputs["baseline"],)}, "unique"),
        ({"candidates": (StrategyConfiguration(time_stop_bars=20),)}, "horizon"),
        ({"policy": SearchPolicy(maximum_backtests=4)}, "budget"),
        (
            {"candidates": tuple(StrategyConfiguration(time_stop_bars=i) for i in range(1, 17))},
            "at most 16",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            run_walk_forward_search(**{**inputs, **updates})
    assert not runner


def test_invalid_plan_order_overlap_and_purge_are_rejected(plan):
    with pytest.raises(ValidationError, match="gap"):
        ValidationPlan.model_validate({**plan.model_dump(), "purge_bars": 13})
    with pytest.raises(ValidationError, match="non-overlapping"):
        ValidationPlan(
            folds=(
                plan.folds[0],
                WalkForwardFold(training=window(0, 25), validation=window(28, 60)),
            ),
            holdout=plan.holdout,
            purge_bars=12,
        )
    with pytest.raises(ValidationError, match="holdout"):
        ValidationPlan(folds=plan.folds, holdout=window(73, 96), purge_bars=12)
    with pytest.raises(ValidationError, match="advance"):
        ValidationPlan(folds=(plan.folds[0], plan.folds[0]), holdout=plan.holdout, purge_bars=12)


@pytest.mark.parametrize(
    "start,end",
    [
        (START.replace(tzinfo=None), START + timedelta(hours=1)),
        (START + timedelta(seconds=1), START + timedelta(hours=1)),
        (START, START),
    ],
)
def test_windows_require_aware_aligned_forward_boundaries(start, end):
    with pytest.raises(ValidationError):
        ResearchWindow(start=start, end=end)


def test_real_backtester_flat_trend_produces_explicit_no_winner(inputs):
    report = run_walk_forward_search(**inputs)
    assert report.selected_checksum is None
    assert not report.baseline_holdout.trades
    assert report.baseline_holdout.net_pnl == 0
    assert all(not candidate.eligible for candidate in report.candidates)


def test_future_holdout_data_cannot_change_development_results(inputs):
    first = run_walk_forward_search(**inputs)
    altered = tuple(
        replace(candle, high=D(999), close=D(999))
        if candle.open_time >= inputs["plan"].holdout.start
        else candle
        for candle in inputs["entry_candles"]
    )
    second = run_walk_forward_search(**{**inputs, "entry_candles": altered})
    assert first.candidates == second.candidates
    assert first.selected_checksum == second.selected_checksum
    assert first.baseline_holdout.dataset_checksum != second.baseline_holdout.dataset_checksum


def test_validation_persists_all_trials_and_audit_without_changing_runtime(
    inputs, runner, tmp_path
):
    result = run_walk_forward_search(**inputs)
    engine = make_engine(f"sqlite:///{tmp_path / 'validation.sqlite3'}")
    create_schema_for_tests(engine)
    try:
        with Session(engine) as session:
            row = save_validation_result(session, result, actor="researcher")
            run_id = row.id
            session.commit()
        with Session(engine) as session:
            saved = session.get(BacktestRun, run_id)
            assert saved.status == "completed"
            assert saved.dataset_metadata["kind"] == "walk_forward_search"
            assert ValidationResult.model_validate(saved.metrics) == result
            assert session.get(ApplicationConfiguration, "runtime") is None
            assert len(session.scalars(select(AuditEvent)).all()) == 2
            save_validation_result(session, result, actor="researcher")
            session.rollback()
            assert len(session.scalars(select(BacktestRun)).all()) == 1
    finally:
        engine.dispose()
