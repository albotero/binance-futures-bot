import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_backtesting import START

from futures_bot.backtesting import BacktestConfiguration
from futures_bot.persistence.models import BacktestRun
from futures_bot.research import ResearchRequest, execute_research

pytest_plugins = ["test_paper"]


def test_bounded_saved_configuration_research_is_persisted_without_credentials(
    factory, history, rules, monkeypatch
):
    hourly, entries = history
    calls = []

    class PublicClient:
        def __init__(self, environment):
            calls.append(environment)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def server_time(self):
            return START + timedelta(days=1)

        async def funding_rate_history(self, *args, **kwargs):
            return ()

        async def exchange_information(self):
            return SimpleNamespace(
                tradable_symbols=lambda quote: (
                    SimpleNamespace(symbol="BTCUSDC", rules=lambda: rules),
                )
            )

    async def historical(client, symbol, interval, start, end, **kwargs):
        assert kwargs["max_pages"] == 10
        return SimpleNamespace(candles=hourly if interval == "1h" else entries, gaps=())

    monkeypatch.setattr("futures_bot.research.BinanceUSDmClient", PublicClient)
    monkeypatch.setattr("futures_bot.research.fetch_historical_klines", historical)
    request = ResearchRequest(
        symbol="BTCUSDC",
        start=START,
        end=START + timedelta(hours=1),
        simulation=BacktestConfiguration(starting_balance="10000"),
    )
    with factory() as session:
        run_id, result = asyncio.run(execute_research(session, request, actor="operator"))
        assert result.quote_asset == "USDC"
        session.commit()
    with factory() as session:
        assert session.get(BacktestRun, run_id).status == "completed"
    assert calls == ["production"]


@pytest.mark.parametrize("span", [timedelta(0), timedelta(days=31)])
def test_research_resource_bounds_are_validated_before_exchange_access(span):
    with pytest.raises(ValueError, match="range"):
        ResearchRequest(
            symbol="BTCUSDC",
            start=START,
            end=START + span,
            simulation=BacktestConfiguration(starting_balance="10000"),
        )


def test_research_rejects_disabled_symbol_without_public_request(factory, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid symbol must not start an HTTP client")

    monkeypatch.setattr("futures_bot.research.BinanceUSDmClient", forbidden)
    with factory() as session:
        with pytest.raises(ValueError, match="enabled"):
            asyncio.run(
                execute_research(
                    session,
                    ResearchRequest(
                        symbol="ETHUSDC",
                        start=START,
                        end=START + timedelta(hours=1),
                        simulation=BacktestConfiguration(starting_balance="10000"),
                    ),
                    actor="operator",
                )
            )
        assert not session.scalars(select(BacktestRun)).all()
