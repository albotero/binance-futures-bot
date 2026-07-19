from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import DashboardMetrics, Position, Side


class ExecutionAdapter(Protocol):
    def open_position(self, position: Position) -> Position: ...

    def close_position(self, symbol: str, price: float,
                       reason: str = "") -> Position | None: ...

    def get_position(self, symbol: str) -> Position | None: ...

    def list_positions(self) -> list[Position]: ...

    def mark_price(self, symbol: str, price: float) -> None: ...

    def snapshot(self) -> DashboardMetrics: ...


@dataclass(slots=True)
class BaseExecution:
    initial_equity: float
    balance: float = field(init=False)
    realized_pnl: float = field(default=0.0, init=False)
    positions: dict[str, Position] = field(default_factory=dict, init=False)
    price_marks: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.balance = self.initial_equity

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def list_positions(self) -> list[Position]:
        return sorted(self.positions.values(), key=lambda item: item.opened_at)

    def mark_price(self, symbol: str, price: float) -> None:
        self.price_marks[symbol] = price
        position = self.positions.get(symbol)
        if position:
            position.mark(price)
            position.update_trailing_stop(self._trailing_stop_pct(position))

    def snapshot(self) -> DashboardMetrics:
        unrealized = sum(
            position.unrealized_pnl for position in self.positions.values())
        margin_in_use = sum((position.entry_price * position.quantity) /
                            max(position.leverage, 1) for position in self.positions.values())
        liquidation_risk = self._liquidation_risk()
        daily_pnl = self.realized_pnl + unrealized
        return DashboardMetrics(
            equity=self.balance + unrealized,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            margin_in_use=margin_in_use,
            liquidation_risk=liquidation_risk,
            open_positions=len(self.positions),
            daily_pnl=daily_pnl,
        )

    def _liquidation_risk(self) -> float:
        if not self.positions:
            return 0.0
        risks: list[float] = []
        for position in self.positions.values():
            if position.side == Side.LONG:
                gap = max(position.current_price -
                          position.stop_loss_price, 0.0)
            else:
                gap = max(position.stop_loss_price -
                          position.current_price, 0.0)
            denom = max(position.current_price, 1e-9)
            risks.append(max(min(100 - (gap / denom) * 100, 100.0), 0.0))
        return sum(risks) / len(risks)

    def _trailing_stop_pct(self, position: Position) -> float:
        if position.side == Side.LONG:
            if position.current_price <= 0:
                return 0.0
            return max((position.current_price - position.trailing_stop_price) / position.current_price * 100, 0.0)
        if position.current_price <= 0:
            return 0.0
        return max((position.trailing_stop_price - position.current_price) / position.current_price * 100, 0.0)
