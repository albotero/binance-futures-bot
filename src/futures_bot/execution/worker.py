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
        end = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0)
        next_open = state.last_bar + timedelta(minutes=15)
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
        hourly, entries, funding, metadata = await asyncio.gather(
            fetch_historical_klines(
                client,
                symbol,
                "1h",
                next_open.replace(minute=0) - timedelta(hours=strategy.trend_slow_period + 50),
                end - timedelta(milliseconds=1),
            ),
            fetch_historical_klines(
                client,
                symbol,
                "15m",
                next_open
                - timedelta(
                    minutes=15
                    * (
                        max(
                            strategy.pullback_period, strategy.atr_period + 1, strategy.swing_period
                        )
                        + 30
                    )
                ),
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
            )
            session.commit()


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
