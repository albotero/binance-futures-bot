from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from futures_bot.data.candles import find_candle_gaps
from futures_bot.exchange.binance_usdm import Kline
from futures_bot.persistence.models import CandleRecord


def cache_candles(
    session: Session,
    symbol: str,
    interval: str,
    candles: Sequence[Kline],
    *,
    source: str = "binance_usdm",
) -> int:
    if not symbol or symbol != symbol.upper():
        raise ValueError("symbol must be uppercase and non-empty")
    if not source or len(source) > 32:
        raise ValueError("source must contain between 1 and 32 characters")
    if candles:
        find_candle_gaps(candles, interval)

    cached_at = datetime.now(UTC)
    for candle in candles:
        session.merge(
            CandleRecord(
                symbol=symbol,
                interval=interval,
                open_time=candle.open_time,
                close_time=candle.close_time,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                quote_volume=candle.quote_volume,
                trade_count=candle.trade_count,
                taker_buy_volume=candle.taker_buy_volume,
                taker_buy_quote_volume=candle.taker_buy_quote_volume,
                source=source,
                cached_at=cached_at,
            )
        )
    session.flush()
    return len(candles)


def load_cached_candles(
    session: Session,
    symbol: str,
    interval: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[Kline, ...]:
    if not symbol or symbol != symbol.upper():
        raise ValueError("symbol must be uppercase and non-empty")
    if start is not None and start.tzinfo is None:
        raise ValueError("start must be timezone-aware")
    if end is not None and end.tzinfo is None:
        raise ValueError("end must be timezone-aware")
    if start is not None and end is not None and end < start:
        raise ValueError("end cannot precede start")

    statement = select(CandleRecord).where(
        CandleRecord.symbol == symbol,
        CandleRecord.interval == interval,
    )
    if start is not None:
        statement = statement.where(CandleRecord.open_time >= start)
    if end is not None:
        statement = statement.where(CandleRecord.open_time <= end)
    records = session.scalars(statement.order_by(CandleRecord.open_time)).all()
    return tuple(
        Kline(
            open_time=record.open_time,
            close_time=record.close_time,
            open=record.open,
            high=record.high,
            low=record.low,
            close=record.close,
            volume=record.volume,
            quote_volume=record.quote_volume,
            trade_count=record.trade_count,
            taker_buy_volume=record.taker_buy_volume,
            taker_buy_quote_volume=record.taker_buy_quote_volume,
        )
        for record in records
    )
