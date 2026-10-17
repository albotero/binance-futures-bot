"""Public-market-data polling for paper runs. No authenticated exchange client."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from futures_bot.config import Settings
from futures_bot.data.candles import fetch_historical_klines
from futures_bot.exchange import BinanceAPIError, BinancePayloadError, BinanceUSDmClient
from futures_bot.execution.paper import (
    PaperConfiguration,
    PaperRun,
    PaperState,
    fault_paper_run,
    process_paper_bar,
)
from futures_bot.execution.portfolio import process_portfolio_bar
from futures_bot.persistence.database import make_engine, make_session_factory
from futures_bot.persistence.repositories import ConfigurationConflict

logger = logging.getLogger(__name__)
MAX_RECOVERY_BARS = 96


async def poll_once(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        row = session.scalar(
            select(PaperRun).where(PaperRun.status.in_(("running", "paused", "stopping")))
        )
        if row is None:
            return
        run_id = row.id
        symbol = row.symbol
        configuration = PaperConfiguration.model_validate(row.configuration)
        state = PaperState.model_validate(row.state)
    async with BinanceUSDmClient(configuration.environment) as client:
        now = await client.server_time()
        hourly_profile = configuration.strategy.profile == "hourly_market"
        duration = timedelta(hours=1) if hourly_profile else timedelta(minutes=15)
        end = now.replace(
            minute=0 if hourly_profile else now.minute // 15 * 15, second=0, microsecond=0
        )
        next_open = state.last_bar + duration
        if next_open >= end:
            return
        if end - next_open > timedelta(minutes=15 * MAX_RECOVERY_BARS):
            with factory() as session:
                fault_paper_run(
                    session,
                    run_id,
                    "paper outage exceeds 24-hour recovery bound; manual ledger review required",
                )
                session.commit()
            return
        strategy = configuration.strategy
        if configuration.symbols:
            await _poll_portfolio(factory, client, run_id, configuration, next_open, end, now)
            return
        market = ()
        if hourly_profile:
            market_data = await fetch_historical_klines(
                client,
                f"BTC{configuration.risk.quote_asset}",
                "1h",
                next_open - timedelta(hours=250),
                end - timedelta(milliseconds=1),
            )
            if market_data.gaps:
                raise ValueError("BTC market history has gaps")
            market = market_data.candles
        hourly, entries, funding, metadata = await asyncio.gather(
            fetch_historical_klines(
                client,
                symbol,
                "1h",
                next_open.replace(minute=0) - timedelta(hours=strategy.hourly_history_bars + 50),
                end - timedelta(milliseconds=1),
            ),
            fetch_historical_klines(
                client,
                symbol,
                strategy.timeframes[0],
                next_open
                - duration * (max(strategy.entry_history_bars, 200 if hourly_profile else 0) + 50),
                end - timedelta(milliseconds=1),
            ),
            client.funding_rate_history(
                symbol,
                start_time=int(next_open.timestamp() * 1000),
                end_time=int(end.timestamp() * 1000) - 1,
                limit=1000,
            ),
            client.exchange_information(),
        )
    contract = next(
        (
            item
            for item in metadata.tradable_symbols(configuration.risk.quote_asset)
            if item.symbol == symbol
        ),
        None,
    )
    if contract is None:
        raise ValueError("saved paper contract is no longer eligible for trading")
    if hourly.gaps or entries.gaps:
        raise ValueError("paper market data has missing historical candles")
    for index, candle in enumerate(entries.candles):
        if candle.open_time < next_open:
            continue
        with factory() as session:
            row = session.get(PaperRun, run_id)
            if row is None or row.status not in ("running", "paused", "stopping"):
                return
            process_paper_bar(
                session,
                run_id,
                hourly.candles,
                entries.candles[: index + 1],
                funding,
                contract.rules(),
                now=now,
                market_candles=market,
            )
            session.commit()


async def _poll_portfolio(factory, client, run_id, configuration, next_open, end, now):
    market = await fetch_historical_klines(
        client,
        f"BTC{configuration.risk.quote_asset}",
        "1h",
        next_open - timedelta(hours=250),
        end - timedelta(milliseconds=1),
    )
    metadata = await client.exchange_information()
    contracts = {c.symbol: c for c in metadata.tradable_symbols(configuration.risk.quote_asset)}
    histories, funding, rules = {}, {}, {}
    for symbol in configuration.symbols:
        if symbol not in contracts:
            raise ValueError(f"portfolio contract is unavailable: {symbol}")
        history = await fetch_historical_klines(
            client, symbol, "1h", next_open - timedelta(hours=250), end - timedelta(milliseconds=1)
        )
        if history.gaps or market.gaps:
            raise ValueError("portfolio market history has missing candles")
        histories[symbol] = history.candles
        funding[symbol] = await client.funding_rate_history(
            symbol,
            start_time=int(next_open.timestamp() * 1000),
            end_time=int(end.timestamp() * 1000) - 1,
            limit=1000,
        )
        if len(funding[symbol]) == 1000:
            raise ValueError("portfolio funding exceeded recovery bound")
        rules[symbol] = contracts[symbol].rules()
    cursor = next_open
    while cursor < end:
        batch = {
            s: tuple(c for c in history if c.open_time <= cursor)
            for s, history in histories.items()
        }
        if any(not history or history[-1].open_time != cursor for history in batch.values()):
            raise ValueError("portfolio batch lacks synchronized candles")
        with factory() as session:
            row = session.get(PaperRun, run_id)
            if row is None or row.status not in ("running", "paused", "stopping"):
                return
            process_portfolio_bar(session, run_id, batch, funding, rules, market.candles, now=now)
            session.commit()
        cursor += timedelta(hours=1)


async def serve(factory: sessionmaker[Session]) -> None:
    while True:
        try:
            await poll_once(factory)
        except ConfigurationConflict:
            logger.info("Paper state changed concurrently; retrying the next poll")
        except (BinanceAPIError, BinancePayloadError, ValueError):
            logger.exception("Paper polling failed; blocking new entries pending operator recovery")
            with factory() as session:
                row = session.scalar(
                    select(PaperRun).where(PaperRun.status.in_(("running", "paused", "stopping")))
                )
                if row is not None:
                    fault_paper_run(
                        session,
                        row.id,
                        "market-data polling failed; inspect logs then acknowledge recovery",
                    )
                    session.commit()
        await asyncio.sleep(15)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    engine = make_engine(settings.database_url)
    try:
        logger.info(
            "Paper-only worker starting at %s; live execution is unavailable", datetime.now(UTC)
        )
        asyncio.run(serve(make_session_factory(engine)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
