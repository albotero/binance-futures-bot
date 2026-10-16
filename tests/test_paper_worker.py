import asyncio
from datetime import timedelta

from sqlalchemy import select
from test_backtesting import START
from test_paper import start

from futures_bot.execution.paper import PaperEvent, PaperRun, PaperState, control_paper_run
from futures_bot.execution.worker import poll_once

pytest_plugins = ["test_paper"]


def test_worker_without_active_run_never_calls_exchange(factory, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("no run must mean no market-data request")

    monkeypatch.setattr("futures_bot.execution.worker.BinanceUSDmClient", forbidden)
    asyncio.run(poll_once(factory))


def test_worker_recovers_each_contiguous_bar_and_restart_is_idempotent(
    factory, history, rules, monkeypatch
):
    from types import SimpleNamespace

    run_id = start(factory)
    hourly, entries = history
    requests = []

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
                tradable_symbols=lambda quote: (
                    SimpleNamespace(symbol="BTCUSDC", rules=lambda: rules),
                )
            )

    async def historical(client, symbol, interval, start, end):
        requests.append(interval)
        return SimpleNamespace(candles=hourly if interval == "1h" else entries, gaps=())

    monkeypatch.setattr("futures_bot.execution.worker.BinanceUSDmClient", PublicClient)
    monkeypatch.setattr("futures_bot.execution.worker.fetch_historical_klines", historical)
    asyncio.run(poll_once(factory))
    assert requests == ["1h", "15m"]
    with factory() as session:
        row = session.get(PaperRun, run_id)
        state = PaperState.model_validate(row.state)
        assert state.last_bar == START + timedelta(minutes=45)
        assert state.position is None  # earlier recovered entry bar was too late
        assert len(session.scalars(select(PaperEvent)).all()) == 4
    requests.clear()
    asyncio.run(poll_once(factory))
    assert not requests
    with factory() as session:
        assert len(session.scalars(select(PaperEvent)).all()) == 4


def test_long_outage_faults_with_explicit_recovery_acknowledgment(factory, monkeypatch):
    run_id = start(factory)

    class PublicClient:
        def __init__(self, environment):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def server_time(self):
            return START + timedelta(days=2)

    monkeypatch.setattr("futures_bot.execution.worker.BinanceUSDmClient", PublicClient)
    asyncio.run(poll_once(factory))
    with factory() as session:
        row = session.get(PaperRun, run_id)
        assert row.status == "faulted"
        assert "24-hour" in PaperState.model_validate(row.state).fault
        control_paper_run(
            session, run_id, "recover", expected_version=row.version, actor="operator"
        )
        assert row.status == "paused"
        assert PaperState.model_validate(row.state).fault is None
        session.commit()
