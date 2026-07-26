from __future__ import annotations

from dataclasses import dataclass

from .models import Side


@dataclass(frozen=True, slots=True)
class TradeAccounting:
    gross_pnl: float
    trading_fees: float
    funding_pnl: float
    net_pnl: float


def calculate_trade_accounting(
    *,
    side: Side | str,
    quantity: float,
    entry_price: float,
    exit_price: float,
    entry_fee_rate_pct: float = 0.0,
    exit_fee_rate_pct: float = 0.0,
    entry_fee: float | None = None,
    exit_fee: float | None = None,
    funding_pnl: float = 0.0,
) -> TradeAccounting:
    normalized_side = side.value if isinstance(side, Side) else side.upper()
    if normalized_side == Side.SHORT.value:
        gross_pnl = (entry_price - exit_price) * quantity
    else:
        gross_pnl = (exit_price - entry_price) * quantity

    resolved_entry_fee = (
        entry_price * quantity * max(entry_fee_rate_pct, 0.0) / 100
        if entry_fee is None else max(entry_fee, 0.0)
    )
    resolved_exit_fee = (
        exit_price * quantity * max(exit_fee_rate_pct, 0.0) / 100
        if exit_fee is None else max(exit_fee, 0.0)
    )
    trading_fees = resolved_entry_fee + resolved_exit_fee
    net_pnl = gross_pnl - trading_fees + funding_pnl
    return TradeAccounting(
        gross_pnl=gross_pnl,
        trading_fees=trading_fees,
        funding_pnl=funding_pnl,
        net_pnl=net_pnl,
    )
