import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from futures_bot.data.candles import (
    fetch_historical_klines,
    find_candle_gaps,
    next_candle_open,
)
from futures_bot.data.repositories import cache_candles, load_cached_candles
from futures_bot.exchange.binance_usdm import Kline
from futures_bot.persistence.database import create_schema_for_tests, make_engine


def candle(open_time: datetime, interval: str = "1m") -> Kline:
    close_time = next_candle_open(open_time, interval) - timedelta(milliseconds=1)
    return Kline(
        open_time=open_time,
        close_time=close_time,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("2"),
        quote_volume=Decimal("201"),
        trade_count=4,
        taker_buy_volume=Decimal("1"),
        taker_buy_quote_volume=Decimal("101"),
    )


def test_gap_detection_reports_missing_candles_without_filling_them() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = (candle(start), candle(start + timedelta(minutes=3)))

    gaps = find_candle_gaps(candles, "1m")

    assert len(candles) == 2
    assert len(gaps) == 1
    assert gaps[0].missing_candles == 2
    assert gaps[0].previous_open == start
    assert gaps[0].next_open == start + timedelta(minutes=3)


def test_gap_detection_rejects_duplicate_and_interval_misaligned_rows() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="strictly ordered"):
        find_candle_gaps((candle(start), candle(start)), "1m")

    with pytest.raises(ValueError, match="aligned"):
        find_candle_gaps(
            (
                candle(start),
                candle(start + timedelta(seconds=90)),
            ),
            "1m",
        )


def test_monthly_interval_advances_across_leap_february() -> None:
    january = datetime(2024, 1, 1, tzinfo=UTC)

    assert next_candle_open(january, "1M") == datetime(2024, 2, 1, tzinfo=UTC)
    assert next_candle_open(datetime(2024, 2, 1, tzinfo=UTC), "1M") == datetime(
        2024, 3, 1, tzinfo=UTC
    )


class PagedClient:
    def __init__(self, rows: tuple[Kline, ...], fetched_at: datetime) -> None:
        self.rows = rows
        self.fetched_at = fetched_at
        self.requests: list[tuple[int | None, int | None, int]] = []

    async def server_time(self) -> datetime:
        return self.fetched_at

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> tuple[Kline, ...]:
        assert symbol == "BTCUSDT"
        assert interval == "1m"
        self.requests.append((start_time, end_time, limit))
        selected = tuple(
            row
            for row in self.rows
            if (start_time is None or int(row.open_time.timestamp() * 1000) >= start_time)
            and (end_time is None or int(row.open_time.timestamp() * 1000) <= end_time)
        )
        return selected[:limit]


def test_historical_fetch_pages_without_overlap_and_drops_open_candle() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = tuple(candle(start + timedelta(minutes=minute)) for minute in range(5))
    client = PagedClient(
        rows,
        fetched_at=start + timedelta(minutes=4, seconds=30),
    )

    dataset = asyncio.run(
        fetch_historical_klines(
            client,
            "BTCUSDT",
            "1m",
            start,
            start + timedelta(minutes=4),
            limit=2,
        )
    )

    assert [item.open_time for item in dataset.candles] == [
        start,
        start + timedelta(minutes=1),
        start + timedelta(minutes=2),
        start + timedelta(minutes=3),
    ]
    assert [request[0] for request in client.requests] == [
        int(start.timestamp() * 1000),
        int((start + timedelta(minutes=2)).timestamp() * 1000),
        int((start + timedelta(minutes=4)).timestamp() * 1000),
    ]
    assert dataset.gaps == ()


def test_historical_fetch_rejects_naive_ranges() -> None:
    start = datetime(2026, 1, 1)
    client = PagedClient((), datetime(2026, 1, 2, tzinfo=UTC))

    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(
            fetch_historical_klines(
                client,
                "BTCUSDT",
                "1m",
                start,
                start + timedelta(minutes=1),
            )
        )


def test_candle_cache_upserts_and_reads_utc_rows(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'candles.sqlite3'}")
    create_schema_for_tests(engine)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    first = candle(start)
    revised = Kline(
        open_time=first.open_time,
        close_time=first.close_time,
        open=first.open,
        high=first.high,
        low=first.low,
        close=Decimal("100.5"),
        volume=first.volume,
        quote_volume=first.quote_volume,
        trade_count=first.trade_count,
        taker_buy_volume=first.taker_buy_volume,
        taker_buy_quote_volume=first.taker_buy_quote_volume,
    )

    with Session(engine) as session:
        assert cache_candles(session, "BTCUSDT", "1m", (first,)) == 1
        session.commit()

    with Session(engine) as session:
        assert cache_candles(session, "BTCUSDT", "1m", (revised,)) == 1
        session.commit()
        cached = load_cached_candles(
            session,
            "BTCUSDT",
            "1m",
            start=start,
            end=start + timedelta(minutes=1),
        )

    assert len(cached) == 1
    assert cached[0].close == Decimal("100.5")
    assert cached[0].open_time.tzinfo == UTC
    engine.dispose()
