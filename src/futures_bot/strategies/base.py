from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Signal

Candle = dict[str, float]


class Strategy(ABC):
    name: str

    @abstractmethod
    def generate(self, candles: list[Candle], symbol: str) -> Signal:
        raise NotImplementedError


def validate_candles(candles: list[Candle]) -> list[Candle]:
    required = {"open", "high", "low", "close", "volume"}
    if not candles:
        raise ValueError("No candles available")
    missing = required.difference(candles[0].keys())
    if missing:
        raise ValueError(f"Missing candle columns: {sorted(missing)}")
    if len(candles) < 30:
        raise ValueError("Not enough candles to evaluate strategies")
    return candles
