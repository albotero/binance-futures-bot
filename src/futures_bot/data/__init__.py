"""Validated historical market-data services."""

from futures_bot.data.candles import (
    CandleGap,
    HistoricalCandles,
    fetch_historical_klines,
    find_candle_gaps,
    next_candle_open,
)

__all__ = [
    "CandleGap",
    "HistoricalCandles",
    "fetch_historical_klines",
    "find_candle_gaps",
    "next_candle_open",
]
