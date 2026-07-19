from __future__ import annotations

from dataclasses import dataclass

from .execution import BaseExecution
from .models import Position, Side, TradeStatus


@dataclass(slots=True)
class PaperExecution(BaseExecution):
    def open_position(self, position: Position) -> Position:
        existing = self.positions.get(position.symbol)
        if existing:
            return existing
        self.positions[position.symbol] = position
        return position

    def close_position(self, symbol: str, price: float, reason: str = "") -> Position | None:
        position = self.positions.pop(symbol, None)
        if not position:
            return None
        position.mark(price)
        if position.side == Side.LONG:
            pnl = (price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - price) * position.quantity
        position.realized_pnl = pnl
        position.status = TradeStatus(reason or TradeStatus.CLOSED.value)
        position.close_reason = reason or TradeStatus.CLOSED.value
        self.realized_pnl += pnl
        self.balance += pnl
        return position
