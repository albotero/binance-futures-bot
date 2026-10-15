from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from futures_bot.exchange.binance_usdm import BinanceUSDmClient, Kline

_INTERVAL_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}
_MILLISECOND = timedelta(milliseconds=1)
_MAX_HISTORY_PAGES = 10_000


class CandleClient(Protocol):
    async def server_time(self) -> datetime: ...

    async def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 500,
    ) -> tuple[Kline, ...]: ...


@dataclass(frozen=True, slots=True)
class CandleGap:
    previous_open: datetime
    next_open: datetime
    missing_candles: int


@dataclass(frozen=True, slots=True)
class HistoricalCandles:
    symbol: str
    interval: str
    requested_start: datetime
    requested_end: datetime
    fetched_at: datetime
    candles: tuple[Kline, ...]
    gaps: tuple[CandleGap, ...]


async def fetch_historical_klines(
    client: BinanceUSDmClient | CandleClient,
    symbol: str,
    interval: str,
    start: datetime,
    end: datetime,
    *,
    limit: int = 1500,
    max_pages: int = _MAX_HISTORY_PAGES,
) -> HistoricalCandles:
    _validate_range(start, end)
    if type(limit) is not int or not 1 <= limit <= 1500:
        raise ValueError("limit must be between 1 and 1500")
    if type(max_pages) is not int or not 1 <= max_pages <= _MAX_HISTORY_PAGES:
        raise ValueError(f"max_pages must be between 1 and {_MAX_HISTORY_PAGES}")
    if interval not in _INTERVAL_SECONDS and interval != "1M":
        raise ValueError("unsupported candle interval")

    fetched_at = _require_aware_utc(await client.server_time(), "exchange server time")
    start_ms = _to_milliseconds(start)
    end_ms = _to_milliseconds(end)
    cursor = start_ms
    rows: list[Kline] = []
    last_open: datetime | None = None

    for _ in range(max_pages):
        page = await client.klines(
            symbol,
            interval,
            start_time=cursor,
            end_time=end_ms,
            limit=limit,
        )
        if not page:
            break

        for candle in page:
            open_time = _require_aware_utc(candle.open_time, "candle open time")
            close_time = _require_aware_utc(candle.close_time, "candle close time")
            if not start <= open_time <= end:
                raise ValueError("exchange returned a candle outside the requested time range")
            if last_open is not None and open_time <= last_open:
                raise ValueError("exchange returned duplicate or unordered candles across pages")
            expected_close = next_candle_open(open_time, interval) - _MILLISECOND
            if close_time != expected_close:
                raise ValueError("candle close time does not match its interval")
            last_open = open_time
            if close_time < fetched_at:
                rows.append(candle)

        last_page_open = _require_aware_utc(page[-1].open_time, "candle open time")
        cursor = _to_milliseconds(next_candle_open(last_page_open, interval))
        if cursor > end_ms or len(page) < limit:
            break
    else:
        raise RuntimeError("historical candle request exceeded the configured page limit")

    candles = tuple(rows)
    return HistoricalCandles(
        symbol=symbol,
        interval=interval,
        requested_start=start.astimezone(UTC),
        requested_end=end.astimezone(UTC),
        fetched_at=fetched_at,
        candles=candles,
        gaps=find_candle_gaps(candles, interval),
    )


def find_candle_gaps(candles: Sequence[Kline], interval: str) -> tuple[CandleGap, ...]:
    if interval not in _INTERVAL_SECONDS and interval != "1M":
        raise ValueError("unsupported candle interval")
    gaps: list[CandleGap] = []
    previous: Kline | None = None
    for candle in candles:
        open_time = _require_aware_utc(candle.open_time, "candle open time")
        expected_close = next_candle_open(open_time, interval) - _MILLISECOND
        if candle.close_time != expected_close:
            raise ValueError("candle close time does not match its interval")
        if previous is not None:
            previous_open = _require_aware_utc(previous.open_time, "candle open time")
            delta = _periods_between(previous_open, open_time, interval)
            if delta <= 0:
                raise ValueError("candles must be strictly ordered and unique")
            if delta > 1:
                gaps.append(
                    CandleGap(
                        previous_open=previous_open,
                        next_open=open_time,
                        missing_candles=delta - 1,
                    )
                )
        previous = candle
    return tuple(gaps)


def next_candle_open(open_time: datetime, interval: str) -> datetime:
    timestamp = _require_aware_utc(open_time, "candle open time").astimezone(UTC)
    if interval == "1M":
        if timestamp.day != 1 or any(
            (timestamp.hour, timestamp.minute, timestamp.second, timestamp.microsecond)
        ):
            raise ValueError("monthly candle open must be at midnight on the first day")
        next_year = timestamp.year + (timestamp.month == 12)
        next_month = 1 if timestamp.month == 12 else timestamp.month + 1
        return timestamp.replace(year=next_year, month=next_month, day=1)
    seconds = _INTERVAL_SECONDS.get(interval)
    if seconds is None:
        raise ValueError("unsupported candle interval")
    return timestamp + timedelta(seconds=seconds)


def _periods_between(previous: datetime, current: datetime, interval: str) -> int:
    if interval == "1M":
        if any(
            timestamp.day != 1
            or timestamp.hour
            or timestamp.minute
            or timestamp.second
            or timestamp.microsecond
            for timestamp in (previous, current)
        ):
            raise ValueError("monthly candles must open at midnight on the first day")
        return (current.year - previous.year) * 12 + current.month - previous.month
    seconds = _INTERVAL_SECONDS.get(interval)
    if seconds is None:
        raise ValueError("unsupported candle interval")
    elapsed = (current - previous).total_seconds()
    periods, remainder = divmod(elapsed, seconds)
    if remainder:
        raise ValueError("candle timestamps are not aligned to the interval")
    return int(periods)


def _validate_range(start: datetime, end: datetime) -> None:
    _require_aware_utc(start, "start")
    _require_aware_utc(end, "end")
    if end < start:
        raise ValueError("end cannot precede start")


def _require_aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _to_milliseconds(value: datetime) -> int:
    elapsed = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return elapsed.days * 86_400_000 + elapsed.seconds * 1000 + elapsed.microseconds // 1000
