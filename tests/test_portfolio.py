from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_backtesting import START, bar

from futures_bot.backtesting import BacktestConfiguration
from futures_bot.backtesting.engine import _size_entry, size_entry_with_leverage
from futures_bot.exchange.leverage import LeverageBracket, LeverageSchedule
from futures_bot.execution.paper import (
    PaperConfiguration,
    PaperEvent,
    PaperRun,
    PaperState,
    control_paper_run,
    create_paper_run,
)
from futures_bot.execution.portfolio import process_portfolio_bar
from futures_bot.persistence.database import (
    create_schema_for_tests,
    make_engine,
    make_session_factory,
)
from futures_bot.persistence.models import OrderRecord, Trade
from futures_bot.risk.configuration import (
    RiskConfiguration,
    SymbolConfiguration,
    save_risk_configuration,
)
from futures_bot.strategy import EntrySetup, StrategyConfiguration
from futures_bot.strategy.configuration import save_strategy_configuration
from futures_bot.strategy.pullback import StrategyEvaluation, market_direction

pytest_plugins = ["test_backtesting"]
SYMBOLS = ("AAAUSDC", "BBBUSDC", "CCCUSDC")


def strategy():
    return StrategyConfiguration(
        profile="hourly_market", trend_fast_period=20, volume_filter="disabled"
    )


def market():
    return tuple(bar(START - timedelta(hours=220 - i), 60, 200 + i) for i in range(220))


def setup(symbol):
    return EntrySetup(
        id=str(uuid4()),
        symbol=symbol,
        direction="long",
        strategy_version="hourly-market-sr-v1",
        configuration_checksum=strategy().checksum,
        configuration_json=strategy().model_dump_json(),
        signal_time=START - timedelta(hours=1),
        eligible_from=START,
        expires_at=START + timedelta(hours=3),
        entry_price=D(100),
        stop_price=D(90),
        target_price=D(120),
        time_stop_bars=None,
    )


@pytest.fixture
def portfolio(tmp_path, risk, rules, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'portfolio.sqlite3'}")
    create_schema_for_tests(engine)

    def schedule(symbol):
        return LeverageSchedule(
            symbol=symbol,
            fetched_at=START,
            brackets=(
                LeverageBracket(initial_leverage=50, notional_floor="0", notional_cap="50000"),
            ),
        )

    configured = RiskConfiguration(
        policy=replace(
            risk.policy,
            basis="available_balance",
            risk_fraction=D(".15"),
            maximum_daily_loss=D(50),
            maximum_drawdown_fraction=D(".1"),
            maximum_stop_distance_fraction=D(".2"),
            maximum_open_positions=3,
        ),
        symbols=tuple(
            SymbolConfiguration(symbol=s, leverage=50, leverage_schedule=schedule(s))
            for s in SYMBOLS
        ),
        daily_loss_fraction="0.30",
        leverage_mode="bracket_aware",
    )
    with Session(engine) as session:
        version = save_risk_configuration(session, configured, actor="test", expected_version=0)
        save_strategy_configuration(session, strategy(), actor="test", expected_version=version)
        row = create_paper_run(
            session,
            SYMBOLS[0],
            BacktestConfiguration(starting_balance="1000", fee_fraction="0", slippage_fraction="0"),
            environment="production",
            start=START,
            actor="test",
            symbols=SYMBOLS,
        )
        control_paper_run(session, row.id, "resume", expected_version=row.version, actor="test")
        run_id = row.id
        session.commit()
    histories = {
        s: (replace(bar(START, 60, 100), open=D(100), low=D(99), high=D(101)),) for s in SYMBOLS
    }
    contracts = {
        s: replace(
            rules, symbol=s, price_tick=D(".01"), quantity_step=D(".001"), min_quantity=D(".001")
        )
        for s in SYMBOLS
    }
    monkeypatch.setattr(
        "futures_bot.execution.portfolio.evaluate_pullback",
        lambda symbol, *args, **kwargs: StrategyEvaluation(setup(symbol), "test"),
    )
    yield engine, run_id, histories, contracts, configured
    engine.dispose()


def process(portfolio, *, histories=None, now=None, market_data=None, funding=None):
    engine, run_id, initial, rules, _ = portfolio
    histories = histories or initial
    with Session(engine) as session:
        row = process_portfolio_bar(
            session,
            run_id,
            histories,
            funding or {s: () for s in SYMBOLS},
            rules,
            market() if market_data is None else market_data,
            now=now or histories[SYMBOLS[0]][-1].close_time + timedelta(seconds=1),
        )
        session.commit()
        return PaperState.model_validate(row.state), row.version


def test_shared_cash_daily_reservation_and_restart_idempotency(portfolio):
    state, version = process(portfolio)
    # First positions reserve15% each; the last cannot exceed the remaining30%dailybudget.
    assert len(state.positions) == 3
    total_risk = sum(p.entry_sizing.estimated_stop_loss for p in state.positions.values())
    assert D(299) < total_risk <= D(300)
    assert state.cash == 1000 and state.equity == 1000
    assert state.positions[SYMBOLS[0]].entry_sizing.estimated_stop_loss == 150
    assert state.positions[SYMBOLS[2]].entry_sizing.estimated_stop_loss < 10
    repeated, repeated_version = process(portfolio)
    assert repeated == state and repeated_version == version
    with Session(portfolio[0]) as session:
        assert len(session.scalars(select(PaperEvent)).all()) == 1


def test_shared_open_position_limit(portfolio):
    engine, run_id, _, _, _ = portfolio
    with Session(engine) as session:
        row = session.get(PaperRun, run_id)
        config = PaperConfiguration.model_validate(row.configuration)
        config = config.model_copy(
            update={
                "risk": config.risk.model_copy(
                    update={"policy": replace(config.risk.policy, maximum_open_positions=1)}
                )
            }
        )
        row.configuration = config.model_dump(mode="json")
        session.commit()
    state, _ = process(portfolio)
    assert len(state.positions) == 1
    with Session(engine) as session:
        event = session.scalar(select(PaperEvent))
        assert any("open" in r for r in event.details["symbols"][SYMBOLS[1]]["risk_reasons"])


def test_stop_preserves_positions_until_original_sl_tp(portfolio):
    state, _ = process(portfolio)
    original = {s: p.setup.stop_price for s, p in state.positions.items()}
    engine, run_id, initial, _, _ = portfolio
    with Session(engine) as session:
        row = session.get(PaperRun, run_id)
        control_paper_run(session, run_id, "stop", expected_version=row.version, actor="test")
        assert row.status == "stopping"
        session.commit()
    histories = {
        s: (
            *history,
            replace(
                history[-1],
                open_time=START + timedelta(hours=1),
                close_time=START + timedelta(hours=2, milliseconds=-1),
                low=D(89),
            ),
        )
        for s, history in initial.items()
    }
    state, _ = process(portfolio, histories=histories, market_data=(*market(), bar(START, 60, 420)))
    assert not state.positions and state.cash < 1000
    with Session(engine) as session:
        assert session.get(PaperRun, run_id).status == "stopped"
        trades = session.scalars(select(Trade)).all()
        assert len(trades) == 3 and all(t.exit_reason == "stop" for t in trades)
    assert set(original.values()) == {D(90)}


def test_market_flip_cancels_pending(portfolio):
    engine, run_id, _, _, _ = portfolio
    with Session(engine) as session:
        row = session.get(PaperRun, run_id)
        state = PaperState.model_validate(row.state)
        row.state = state.model_copy(
            update={"pendings": {SYMBOLS[0]: setup(SYMBOLS[0])}}
        ).model_dump(mode="json")
        session.commit()
    falling = tuple(
        replace(
            c,
            open=D(1000) - c.open,
            high=D(1000) - c.low,
            low=D(1000) - c.high,
            close=D(1000) - c.close,
        )
        for c in market()
    )
    state, _ = process(portfolio, market_data=falling)
    assert not state.positions and not state.pendings


def test_incomplete_batch_is_atomic(portfolio):
    engine, run_id, histories, rules, _ = portfolio
    with Session(engine) as session:
        before = session.get(PaperRun, run_id).state
        with pytest.raises(ValueError, match="every configured"):
            process_portfolio_bar(
                session,
                run_id,
                {SYMBOLS[0]: histories[SYMBOLS[0]]},
                {s: () for s in SYMBOLS},
                rules,
                market(),
                now=START,
            )
        assert session.get(PaperRun, run_id).state == before


def test_stale_market_blocks_before_entries(portfolio):
    with pytest.raises(ValueError, match="stale"):
        process(portfolio, market_data=market()[:-2])


def test_late_recovery_cannot_open_positions(portfolio):
    state, _ = process(portfolio, now=START + timedelta(hours=2))
    assert not state.positions


def test_dynamic_daily_fraction_in_shared_backtest_sizer(portfolio):
    _, _, _, contracts, configured = portfolio
    selected = configured.model_copy(update={"leverage_mode": "fixed"})
    decision = _size_entry(
        setup(SYMBOLS[0]),
        D(100),
        D(1000),
        D(1000),
        D(0),
        START,
        D(1000),
        D(1000),
        contracts[SYMBOLS[0]],
        selected,
        50,
    )
    assert decision.accepted and decision.estimated_stop_loss == 150
    assert configured.policy.maximum_daily_loss == 50


def test_market_gate_uses_only_closed_history_and_fails_missing():
    history = market()
    assert market_direction(history, as_of=START) == "long"
    assert (
        market_direction(
            (
                *history,
                replace(
                    history[-1],
                    open_time=START,
                    close_time=START + timedelta(hours=1, milliseconds=-1),
                    close=D(1),
                ),
            ),
            as_of=START,
        )
        == "long"
    )
    with pytest.raises(ValueError, match="200"):
        market_direction(history[:100], as_of=START)


def test_hourly_profile_refuses_inconsistent_parameters():
    with pytest.raises(ValueError, match="requires"):
        StrategyConfiguration(profile="hourly_market")


def test_bracket_aware_sizing_uses_actual_lower_leverage(portfolio):
    _, _, _, contracts, configured = portfolio
    symbols = list(configured.symbols)
    symbols[0] = symbols[0].model_copy(
        update={
            "leverage_schedule": LeverageSchedule(
                symbol=SYMBOLS[0],
                fetched_at=START,
                brackets=(
                    LeverageBracket(initial_leverage=50, notional_floor="0", notional_cap="100"),
                    LeverageBracket(
                        initial_leverage=25, notional_floor="100", notional_cap="50000"
                    ),
                ),
            )
        }
    )
    configured = configured.model_copy(update={"symbols": tuple(symbols)})
    decision, leverage = size_entry_with_leverage(
        setup(SYMBOLS[0]),
        D(100),
        D(1000),
        D(1000),
        D(0),
        START,
        D(1000),
        D(1000),
        contracts[SYMBOLS[0]],
        configured,
        50,
    )
    assert decision.accepted and leverage == 25
    assert decision.estimated_stop_loss == 150
    assert decision.margin_required == 60


def test_failed_funding_batch_rolls_back_entries_and_checkpoint(portfolio):
    from futures_bot.exchange import FundingRate

    engine, run_id, histories, rules, _ = portfolio
    invalid = FundingRate(symbol="OTHERUSDC", funding_time=START, rate=D(".01"), mark_price=D(100))
    with pytest.raises(ValueError, match="funding"), Session(engine) as session:
        process_portfolio_bar(
            session,
            run_id,
            histories,
            {s: (invalid,) for s in SYMBOLS},
            rules,
            market(),
            now=START + timedelta(hours=1, seconds=1),
        )
        session.commit()
    with Session(engine) as session:
        assert not session.scalars(select(OrderRecord)).all()
        assert not session.scalars(select(PaperEvent)).all()
        state = PaperState.model_validate(session.get(PaperRun, run_id).state)
        assert state.last_bar == START - timedelta(hours=1) and not state.positions


def test_worker_polls_synchronized_hourly_portfolio_and_is_restart_safe(portfolio, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from futures_bot.execution.worker import poll_once

    engine, run_id, histories, rules, _ = portfolio
    calls = []

    class PublicClient:
        def __init__(self, environment):
            assert environment == "production"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def server_time(self):
            return START + timedelta(hours=1, seconds=1)

        async def funding_rate_history(self, *args, **kwargs):
            return ()

        async def exchange_information(self):
            return SimpleNamespace(
                tradable_symbols=lambda quote: tuple(
                    SimpleNamespace(symbol=s, rules=lambda symbol=s: rules[symbol]) for s in SYMBOLS
                )
            )

    async def historical(client, symbol, interval, start, end):
        assert interval == "1h" and start == START - timedelta(hours=250)
        calls.append(symbol)
        return SimpleNamespace(
            candles=market() if symbol == "BTCUSDC" else histories[symbol], gaps=()
        )

    monkeypatch.setattr("futures_bot.execution.worker.BinanceUSDmClient", PublicClient)
    monkeypatch.setattr("futures_bot.execution.worker.fetch_historical_klines", historical)
    factory = make_session_factory(engine)
    asyncio.run(poll_once(factory))
    assert calls == ["BTCUSDC", *SYMBOLS]
    with factory() as session:
        state = PaperState.model_validate(session.get(PaperRun, run_id).state)
        assert state.last_bar == START and len(state.positions) == 3
    asyncio.run(poll_once(factory))
    assert calls == ["BTCUSDC", *SYMBOLS]
    with factory() as session:
        assert len(session.scalars(select(PaperEvent)).all()) == 1


def test_shared_funding_marked_equity_and_no_trailing(portfolio):
    from futures_bot.exchange import FundingRate

    before, _ = process(portfolio)
    histories = {
        s: (
            *h,
            replace(
                h[-1],
                open_time=START + timedelta(hours=1),
                close_time=START + timedelta(hours=2, milliseconds=-1),
                high=D(112),
                close=D(110),
            ),
        )
        for s, h in portfolio[2].items()
    }
    funding = {
        s: (
            FundingRate(
                symbol=s,
                funding_time=START + timedelta(hours=1, minutes=30),
                rate=D("-.001"),
                mark_price=D(100),
            ),
        )
        for s in SYMBOLS
    }
    state, _ = process(
        portfolio,
        histories=histories,
        funding=funding,
        market_data=(*market(), bar(START, 60, 420)),
    )
    quantity = sum(p.quantity for p in before.positions.values())
    assert state.cash == before.cash + quantity * D(".1")
    assert state.equity == state.cash + quantity * 10
    assert sum(p.funding_pnl for p in state.positions.values()) == quantity * D(".1")
    assert state.daily_open_equity == 1000  # floating profit does not expand today's budget
    assert {s: p.setup for s, p in state.positions.items()} == {
        s: p.setup for s, p in before.positions.items()
    }


def test_shared_daily_budget_resets_to_prior_marked_equity_at_local_midnight(portfolio):
    from datetime import UTC, datetime

    process(portfolio)
    midnight = datetime(2026, 10, 17, 5, tzinfo=UTC)  # midnight America/Bogota
    shift = midnight - START
    engine, run_id, initial, _, _ = portfolio
    with Session(engine) as session:
        row = session.get(PaperRun, run_id)
        state = PaperState.model_validate(row.state)
        row.state = state.model_copy(
            update={"last_bar": midnight - timedelta(hours=1), "cash": D(900), "equity": D(900)}
        ).model_dump(mode="json")
        session.commit()
    histories = {
        s: tuple(
            replace(c, open_time=c.open_time + shift, close_time=c.close_time + shift) for c in h
        )
        for s, h in initial.items()
    }
    market_data = tuple(
        replace(c, open_time=c.open_time + shift, close_time=c.close_time + shift) for c in market()
    )
    state, _ = process(portfolio, histories=histories, market_data=market_data)
    assert state.daily_open_equity == 900
    assert state.local_day == "2026-10-17"
