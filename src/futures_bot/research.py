"""Bounded public-data research using saved operator configuration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.orm import Session

from futures_bot.backtesting import BacktestConfiguration, BacktestResult, run_backtest
from futures_bot.backtesting.results import save_backtest_result
from futures_bot.data.candles import fetch_historical_klines
from futures_bot.exchange import BinanceUSDmClient
from futures_bot.risk.configuration import RiskConfiguration, load_risk_configuration
from futures_bot.strategy.configuration import load_strategy_configuration


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    start: datetime
    end: datetime
    simulation: BacktestConfiguration

    @field_validator("start", "end")
    @classmethod
    def aligned(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("research dates must be timezone-aware")
        value = value.astimezone(UTC)
        if value.minute % 15 or value.second or value.microsecond:
            raise ValueError("research dates must align to 15-minute opens")
        return value

    @model_validator(mode="after")
    def bounded(self) -> ResearchRequest:
        if not timedelta(minutes=15) <= self.end - self.start <= timedelta(days=30):
            raise ValueError("research range must span 15 minutes through 30 days")
        return self


async def execute_research(
    session: Session,
    request: ResearchRequest,
    *,
    actor: str,
    risk_override: RiskConfiguration | None = None,
    expected_risk_version: int | None = None,
) -> tuple[str, BacktestResult]:
    """Public production prices only, no keys or account calls. Caller commits."""
    risk = load_risk_configuration(session)
    if risk_override is not None:
        if expected_risk_version != risk.version:
            raise ValueError("risk settings changed while verifying leverage; reload")
        risk = type(risk)(version=risk.version, configuration=risk_override)
    strategy = load_strategy_configuration(session)
    if not any(
        item.symbol == request.symbol and item.enabled for item in risk.configuration.symbols
    ):
        raise ValueError("research symbol must be enabled in saved risk settings")
    parameters = strategy.configuration
    async with BinanceUSDmClient("production") as client:
        server_time = await client.server_time()
        if request.end > server_time:
            raise ValueError("research cannot include future or unclosed bars")
        metadata = await client.exchange_information()
        contract = next(
            (
                item
                for item in metadata.tradable_symbols(risk.configuration.quote_asset)
                if item.symbol == request.symbol
            ),
            None,
        )
        if contract is None:
            raise ValueError("research symbol is unavailable for the selected quote")
        hourly, entries = await asyncio.gather(
            fetch_historical_klines(
                client,
                request.symbol,
                "1h",
                request.start.replace(minute=0)
                - timedelta(hours=parameters.hourly_history_bars + 50),
                request.end - timedelta(milliseconds=1),
                max_pages=10,
            ),
            fetch_historical_klines(
                client,
                request.symbol,
                "15m",
                request.start - timedelta(minutes=15 * (parameters.entry_history_bars + 50)),
                request.end - timedelta(milliseconds=1),
                max_pages=10,
            ),
        )
        # Funding intervals are not assumed to be fixed at eight hours.
        funding = []
        cursor = int(request.start.timestamp() * 1000)
        end_ms = int(request.end.timestamp() * 1000) - 1
        for _ in range(5):
            page = await client.funding_rate_history(
                request.symbol,
                start_time=cursor,
                end_time=end_ms,
                limit=1000,
            )
            if not page:
                break
            if int(page[0].funding_time.timestamp() * 1000) < cursor:
                raise ValueError("funding pagination did not advance")
            funding.extend(page)
            cursor = int(page[-1].funding_time.timestamp() * 1000) + 1
            if len(page) < 1000 or cursor > end_ms:
                break
        else:
            raise ValueError("research funding exceeded the bounded pagination budget")
    if not funding and request.end - request.start >= timedelta(days=1):
        raise ValueError("no funding observations returned; cannot assume zero for a full day")
    result = await asyncio.to_thread(
        run_backtest,
        request.symbol,
        hourly.candles,
        entries.candles,
        tuple(funding),
        contract.rules(),
        parameters,
        risk.configuration,
        request.simulation,
        start=request.start,
        end=request.end,
    )
    row = save_backtest_result(session, result, actor=actor)
    return row.id, result
