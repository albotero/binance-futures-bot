from __future__ import annotations

from dataclasses import dataclass, field

from .execution import BaseExecution
from .market_data import BinanceFuturesRESTClient
from .models import Position, Side, TradeStatus


@dataclass(slots=True)
class BinanceFuturesExecution(BaseExecution):
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://fapi.binance.com"
    client: BinanceFuturesRESTClient = field(init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.client = BinanceFuturesRESTClient(
            self.api_key, self.api_secret, self.base_url)

    def open_position(self, position: Position) -> Position:
        side = "BUY" if position.side == Side.LONG else "SELL"
        self.client.futures_change_leverage(
            symbol=position.symbol, leverage=position.leverage)
        self.client.futures_create_order(
            symbol=position.symbol,
            side=side,
            type="MARKET",
            quantity=round(position.quantity, 5),
        )
        self.positions[position.symbol] = position
        return position

    def close_position(self, symbol: str, price: float, reason: str = "") -> Position | None:
        position = self.positions.pop(symbol, None)
        if not position:
            return None
        close_side = "SELL" if position.side == Side.LONG else "BUY"
        self.client.futures_create_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=round(position.quantity, 5),
            reduceOnly=True,
        )
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
