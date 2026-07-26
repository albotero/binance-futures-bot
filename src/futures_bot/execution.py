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
    trailing_stop_pct: float = 0.0
    trailing_stage_enabled: bool = False
    hybrid_trailing_enabled: bool = False
    trailing_break_even_r: float = 0.8
    trailing_activation_r: float = 1.2
    trailing_fee_buffer_pct: float = 0.04
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
            if self.trailing_stop_pct > 0:
                position.update_trailing_stop(self.trailing_stop_pct)

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
            entry_price = max(position.entry_price, 1e-9)
            liquidation_distance = entry_price / max(position.leverage, 1)
            if position.side == Side.LONG:
                adverse_move = max(entry_price - position.current_price, 0.0)
            else:
                adverse_move = max(position.current_price - entry_price, 0.0)
            risks.append(max(min(
                adverse_move / max(liquidation_distance, 1e-9) * 100,
                100.0,
            ), 0.0))
        return max(risks)
