from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_backtesting import START

from futures_bot.backtesting import BacktestConfiguration
from futures_bot.exchange.leverage import LeverageBracket, LeverageSchedule
from futures_bot.execution.paper import (
    PaperConfiguration,
    PaperEvent,
    PaperRun,
    PaperState,
    control_paper_run,
    create_paper_run,
    process_paper_bar,
)
from futures_bot.persistence.database import create_schema_for_tests, make_engine
from futures_bot.persistence.models import Fill, OrderRecord, Position, Trade
from futures_bot.persistence.repositories import ConfigurationConflict
from futures_bot.risk.configuration import (
    RiskConfiguration,
    SymbolConfiguration,
    save_risk_configuration,
)
from futures_bot.strategy import StrategyConfiguration
from futures_bot.strategy.configuration import save_strategy_configuration

pytest_plugins = ["test_backtesting"]


@pytest.fixture
def factory(tmp_path, risk):
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.sqlite3'}")
    create_schema_for_tests(engine)
    with Session(engine) as session:
        version = save_risk_configuration(session, risk, actor="test", expected_version=0)
        save_strategy_configuration(
            session,
            StrategyConfiguration(),
            actor="test",
            expected_version=version,
        )
        session.commit()
    yield lambda: Session(engine, expire_on_commit=False)
    engine.dispose()


def start(factory):
    with factory() as session:
        row = create_paper_run(
            session,
            "BTCUSDC",
            BacktestConfiguration(
                starting_balance="10000", fee_fraction="0", slippage_fraction="0"
            ),
            environment="production",
            start=START,
            actor="operator",
        )
        assert row.status == "paused"
        control_paper_run(session, row.id, "resume", expected_version=row.version, actor="operator")
        session.commit()
        return row.id


def process(factory, run_id, history, rules, offset=0, now=None):
    hourly, entries = history
    entries = entries[: len(entries) - 3 + offset]
    with factory() as session:
        row = process_paper_bar(
            session,
            run_id,
            hourly,
            entries,
            (),
            rules,
            now=now or entries[-1].close_time + timedelta(seconds=1),
        )
        session.commit()
        return row


def test_restart_and_repeated_bar_preserve_exactly_one_entry(factory, history, rules):
    run_id = start(factory)
    row = process(factory, run_id, history, rules)
    state = PaperState.model_validate(row.state)
    assert state.position is not None
    version = row.version
    row = process(factory, run_id, history, rules)
    assert row.version == version
    with factory() as session:
        assert len(session.scalars(select(PaperEvent)).all()) == 1
        assert len(session.scalars(select(OrderRecord)).all()) == 1
        assert len(session.scalars(select(Fill)).all()) == 1
        position = session.scalar(select(Position))
        assert position.mode == "paper"
        assert position.status == "open"
    second = process(factory, run_id, history, rules, 1)
    assert PaperState.model_validate(second.state).position.bars_held == 2


def test_paper_125x_entry_respects_verified_bracket_snapshot(factory, history, rules, risk):
    schedule = LeverageSchedule(
        symbol="BTCUSDC",
        fetched_at=START,
        brackets=(LeverageBracket(initial_leverage=125, notional_floor="0", notional_cap="1000"),),
    )
    configured = RiskConfiguration(
        policy=risk.policy,
        symbols=(
            SymbolConfiguration(
                symbol="BTCUSDC",
                leverage=125,
                leverage_schedule=schedule,
            ),
        ),
    )
    with factory() as session:
        save_risk_configuration(session, configured, actor="test", expected_version=2)
        session.commit()
    run_id = start(factory)
    row = process(factory, run_id, history, rules)
    position = PaperState.model_validate(row.state).position
    assert position is not None and position.leverage == 125
    assert position.entry_price * position.quantity <= Decimal("1000")
    assert (
        row.configuration["risk"]["symbols"][0]["leverage_schedule"]["environment"] == "production"
    )


def test_old_paper_snapshot_keeps_legacy_protection_but_new_runs_use_structure(factory):
    run_id = start(factory)
    with factory() as session:
        row = session.get(PaperRun, run_id)
        snapshot = row.configuration
        assert (
            PaperConfiguration.model_validate(snapshot).strategy.protection_mode
            == "support_resistance"
        )
        old = {
            **snapshot,
            "strategy": {
                key: value
                for key, value in snapshot["strategy"].items()
                if key
                not in (
                    "protection_mode",
                    "level_lookback_bars",
                    "pivot_confirmation_bars",
                    "exit_policy",
                    "cost_filter",
                )
            },
        }
        assert PaperConfiguration.model_validate(old).strategy.protection_mode == "legacy_atr"
        assert PaperConfiguration.model_validate(old).strategy.exit_policy == "legacy_time_limit"
        prior_structural = {
            **snapshot,
            "strategy": {
                key: value
                for key, value in snapshot["strategy"].items()
                if key not in ("exit_policy", "cost_filter")
            },
        }
        previous = PaperConfiguration.model_validate(prior_structural).strategy
        assert previous.implementation == "ema-pullback-sr-v2"
        assert previous.effective_time_stop_bars == 8
        assert previous.cost_filter == "gross_only"
        current = PaperConfiguration.model_validate(snapshot).strategy
        assert current.effective_time_stop_bars is None
        assert current.cost_filter == "gross_only"
        assert (
            PaperConfiguration.model_validate(snapshot).simulation.stop_risk_model
            == "fees_and_slippage"
        )
        old_simulation = {
            **snapshot,
            "simulation": {
                k: v for k, v in snapshot["simulation"].items() if k != "stop_risk_model"
            },
        }
        assert (
            PaperConfiguration.model_validate(old_simulation).simulation.stop_risk_model
            == "price_only"
        )


def test_paper_entry_budget_includes_fees_and_slippage_in_persisted_breakdown(
    factory, history, rules
):
    with factory() as session:
        row = create_paper_run(
            session,
            "BTCUSDC",
            BacktestConfiguration(
                starting_balance="10000", fee_fraction=".0005", slippage_fraction=".0002"
            ),
            environment="production",
            start=START,
            actor="test",
        )
        control_paper_run(session, row.id, "resume", expected_version=row.version, actor="test")
        session.commit()
        run_id = row.id
    row = process(factory, run_id, history, rules)
    position = PaperState.model_validate(row.state).position
    assert position is not None
    sizing = position.entry_sizing
    assert sizing is not None
    assert sizing.estimated_stop_loss <= sizing.risk_budget
    assert sizing.estimated_round_trip_fees > 0 and sizing.estimated_stop_slippage_loss > 0
    with factory() as session:
        event = session.scalar(select(PaperEvent))
        assert Decimal(event.details["sizing"]["estimated_stop_loss"]) == sizing.estimated_stop_loss


def test_restart_after_more_than_eight_bars_keeps_protected_position(factory, history, rules):
    from dataclasses import replace

    from test_backtesting import bar

    run_id = start(factory)
    hourly, entries = history
    hourly = (*hourly, *(bar(START + timedelta(hours=i), 60, 433) for i in range(4)))
    entries = (
        *entries[:-4],
        *(
            replace(bar(START + timedelta(minutes=15 * i), 15, 433), low=Decimal(432))
            for i in range(13)
        ),
    )
    for index in range(12):
        current = entries[: 30 + index + 1]
        with factory() as session:
            row = process_paper_bar(
                session,
                run_id,
                hourly,
                current,
                (),
                rules,
                now=current[-1].close_time + timedelta(seconds=1),
            )
            session.commit()
            state = PaperState.model_validate(row.state)
            assert state.position is not None
            assert state.position.setup.time_stop_bars is None
    with factory() as session:
        assert not session.scalars(select(Trade)).all()
    stopped = replace(entries[-1], low=Decimal(400), close=Decimal(410))
    with factory() as session:
        row = process_paper_bar(
            session,
            run_id,
            hourly,
            (*entries[:-1], stopped),
            (),
            rules,
            now=stopped.close_time + timedelta(seconds=1),
        )
        session.commit()
        assert PaperState.model_validate(row.state).position is None
        assert session.scalar(select(Trade)).exit_reason == "stop"


def test_pause_blocks_entries_but_existing_protection_remains_managed(factory, history, rules):
    run_id = start(factory)
    process(factory, run_id, history, rules)
    with factory() as session:
        row = session.get(PaperRun, run_id)
        control_paper_run(session, run_id, "pause", expected_version=row.version, actor="operator")
        session.commit()
    hourly, entries = history
    from dataclasses import replace

    stop = PaperState.model_validate(row.state).position.setup.stop_price
    changed = (*entries[:-3], replace(entries[-3], low=stop - 1, close=stop), *entries[-2:])
    result = process(factory, run_id, (hourly, changed), rules, 1)
    assert result.status == "paused"
    assert PaperState.model_validate(result.state).position is None
    with factory() as session:
        trade = session.scalar(select(Trade))
        assert trade.exit_reason == "stop"
        assert trade.net_pnl < 0
        assert len(session.scalars(select(OrderRecord)).all()) == 2


def test_stop_drains_instead_of_discarding_open_position(factory, history, rules):
    run_id = start(factory)
    process(factory, run_id, history, rules)
    with factory() as session:
        row = session.get(PaperRun, run_id)
        control_paper_run(session, run_id, "stop", expected_version=row.version, actor="operator")
        assert row.status == "stopping"
        session.commit()
    hourly, entries = history
    from dataclasses import replace

    stop = PaperState.model_validate(row.state).position.setup.stop_price
    changed = (*entries[:-3], replace(entries[-3], low=stop - 1, close=stop), *entries[-2:])
    row = process(factory, run_id, (hourly, changed), rules, 1)
    assert row.status == "stopped"
    assert row.active_slot is None


def test_gap_faults_explicitly_and_late_bar_cannot_enter(factory, history, rules):
    run_id = start(factory)
    row = process(factory, run_id, history, rules, now=START + timedelta(hours=1))
    assert PaperState.model_validate(row.state).position is None
    row = process(factory, run_id, history, rules, 2)
    assert row.status == "faulted"
    assert "missing paper bars" in PaperState.model_validate(row.state).fault
    with factory() as session:
        with pytest.raises(ValueError, match="healthy paused"):
            control_paper_run(
                session, run_id, "resume", expected_version=row.version, actor="operator"
            )


def test_controls_are_optimistic_and_bar_writes_roll_back_atomically(factory, history, rules):
    run_id = start(factory)
    with factory() as session:
        with pytest.raises(ConfigurationConflict):
            control_paper_run(session, run_id, "pause", expected_version=0, actor="operator")
    hourly, entries = history
    with factory() as session:
        process_paper_bar(
            session,
            run_id,
            hourly,
            entries[:-3],
            (),
            rules,
            now=entries[-4].close_time + timedelta(seconds=1),
        )
        session.rollback()
    with factory() as session:
        assert not session.scalars(select(OrderRecord)).all()
        assert not session.scalars(select(PaperEvent)).all()
        assert PaperState.model_validate(session.get(PaperRun, run_id).state).cash == Decimal(10000)
    assert PaperState.model_validate(process(factory, run_id, history, rules).state).position


def test_only_one_active_run_and_unclosed_candle_is_rejected(factory, history, rules):
    run_id = start(factory)
    with factory() as session:
        with pytest.raises(ValueError, match="existing paper"):
            create_paper_run(
                session,
                "BTCUSDC",
                BacktestConfiguration(starting_balance="10000"),
                environment="production",
                start=START,
                actor="operator",
            )
    with pytest.raises(ValueError, match="unclosed"):
        process(factory, run_id, history, rules, now=START)
