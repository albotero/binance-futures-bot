from __future__ import annotations

from dataclasses import dataclass

from .accounting import calculate_trade_accounting
from .execution import BaseExecution
from .models import Position, Side, TradeStatus


@dataclass(slots=True)
class PaperExecution(BaseExecution):
    def open_position(self, position: Position) -> Position:
        existing = self.positions.get(position.symbol)
        if existing:
            return existing
        entry_fee = position.entry_price * position.quantity * self.trading_fee_pct / 100
        position.trading_fees = entry_fee
        self.realized_pnl -= entry_fee
        self.balance -= entry_fee
        self.positions[position.symbol] = position
        return position

    def close_position(self, symbol: str, price: float, reason: str = "") -> Position | None:
        position = self.positions.pop(symbol, None)
        if not position:
            return None
        position.mark(price)
        exit_fee = price * position.quantity * self.trading_fee_pct / 100
        accounting = calculate_trade_accounting(
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=price,
            entry_fee=position.trading_fees,
            exit_fee=exit_fee,
            funding_pnl=position.funding_pnl,
        )
        position.gross_pnl = accounting.gross_pnl
        position.trading_fees = accounting.trading_fees
        position.realized_pnl = accounting.net_pnl
        position.status = TradeStatus(reason or TradeStatus.CLOSED.value)
        position.close_reason = reason or TradeStatus.CLOSED.value
        close_cash_flow = accounting.gross_pnl - exit_fee
        self.realized_pnl += close_cash_flow
        self.balance += close_cash_flow
        return position
