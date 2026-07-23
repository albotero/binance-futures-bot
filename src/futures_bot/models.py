from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    EXTERNAL_CLOSE = "EXTERNAL_CLOSE"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    REVERSED = "REVERSED"


@dataclass(slots=True)
class StrategyRule:
    name: str
    enabled: bool = True
    weight: float = 1.0
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StrategyProfile:
    name: str
    threshold: float = 0.35
    rules: list[StrategyRule] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rules"] = [asdict(rule) for rule in self.rules]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StrategyProfile":
        rules_payload = payload.get("rules", []) or []
        rules: list[StrategyRule] = []
        for rule in rules_payload:
            if isinstance(rule, StrategyRule):
                rules.append(rule)
            else:
                rules.append(StrategyRule(**rule))
        return cls(
            name=payload.get("name", "default"),
            threshold=float(payload.get("threshold", 0.35)),
            rules=rules,
            description=payload.get("description", ""),
        )


@dataclass(slots=True)
class BotConfig:
    mode: str = "paper"
    symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    trade_all_symbols: bool = False
    allow_short: bool = True
    live_protection_mode: str = "local_and_exchange"
    backtest_duration: str = "12w"
    candle_style: str = "raw"
    interval: str = "5m"
    candles_limit: int = 200
    leverage: int = 3
    max_leverage: int = 3
    risk_per_trade_pct: float = 1.0
    risk_reward_ratio: float = 2.0
    max_open_positions: int = 5
    max_position_pct: float = 25.0
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0
    trailing_stop_pct: float = 1.0
    trailing_stage_enabled: bool = False
    trailing_break_even_r: float = 0.8
    trailing_activation_r: float = 1.2
    trailing_fee_buffer_pct: float = 0.04
    max_daily_loss_pct: float = 5.0
    min_margin_buffer_pct: float = 25.0
    quote_asset: str = "USDT"
    initial_equity: float = 1000.0
    poll_seconds: int = 20
    strategy_profile: str = "default"
    data_dir: str = "data"
    db_path: str = "data/bot.db"
    binance_base_url: str = "https://fapi.binance.com"
    testnet: bool = False
    live_trading_confirmed: bool = False
    api_key: str = ""
    api_secret: str = ""
    backtest_max_candles: int = 0
    backtest_eval_window: int = 320
    backtest_cache_enabled: bool = True
    backtest_cache_ttl_hours: float = 0.0
    backtest_cache_dir: str = "data/cache/backtest_candles"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BotConfig":
        base = cls()
        merged = base.to_dict()
        merged.update(payload)
        if isinstance(merged.get("symbols"), str):
            merged["symbols"] = [item.strip().upper()
                                 for item in merged["symbols"].split(",") if item.strip()]
        merged["symbols"] = [symbol.upper()
                             for symbol in merged.get("symbols", [])]
        return cls(**merged)


@dataclass(slots=True)
class Signal:
    symbol: str
    strategy: str
    score: float
    reason: str
    timestamp: str = field(default_factory=iso)

    @property
    def side(self) -> Side | None:
        if self.score > 0:
            return Side.LONG
        if self.score < 0:
            return Side.SHORT
        return None


@dataclass(slots=True)
class ExitPlan:
    stop_loss_price: float
    take_profit_price: float
    trailing_stop_price: float
    support_price: float = 0.0
    resistance_price: float = 0.0
    rationale: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Position:
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    current_price: float
    leverage: int
    strategy: str
    stop_loss_price: float
    take_profit_price: float
    trailing_stop_price: float
    entry_order_id: int | None = None
    exit_order_id: int | None = None
    trailing_stage_enabled: bool = False
    trailing_break_even_r: float = 0.8
    trailing_activation_r: float = 1.2
    trailing_fee_buffer_pct: float = 0.04
    initial_stop_loss_price: float = 0.0
    trailing_armed: bool = False
    break_even_applied: bool = False
    opened_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    status: TradeStatus = TradeStatus.OPEN
    close_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["side"] = self.side.value
        payload["status"] = self.status.value
        return payload

    def mark(self, price: float) -> None:
        self.current_price = price
        if self.side == Side.LONG:
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.quantity
        self.updated_at = iso()

    def update_trailing_stop(self, trailing_pct: float) -> None:
        if trailing_pct <= 0:
            return
        if self.initial_stop_loss_price <= 0:
            self.initial_stop_loss_price = self.stop_loss_price

        if not self.trailing_stage_enabled:
            self._update_trailing_legacy(trailing_pct)
            return

        risk_per_unit = abs(self.entry_price - self.initial_stop_loss_price)
        if risk_per_unit <= 0:
            self._update_trailing_legacy(trailing_pct)
            return

        if self.side == Side.LONG:
            favorable_move = self.current_price - self.entry_price
            r_multiple = favorable_move / risk_per_unit
            if (not self.break_even_applied) and r_multiple >= self.trailing_break_even_r:
                break_even_price = self.entry_price * \
                    (1 + self.trailing_fee_buffer_pct / 100)
                self.stop_loss_price = max(
                    self.stop_loss_price, break_even_price)
                self.break_even_applied = True

            if r_multiple >= self.trailing_activation_r:
                self.trailing_armed = True
                candidate = self.current_price * (1 - trailing_pct / 100)
                floor = self.stop_loss_price
                if self.trailing_stop_price <= 0:
                    self.trailing_stop_price = max(candidate, floor)
                else:
                    self.trailing_stop_price = max(
                        self.trailing_stop_price,
                        candidate,
                        floor,
                    )
            return

        favorable_move = self.entry_price - self.current_price
        r_multiple = favorable_move / risk_per_unit
        if (not self.break_even_applied) and r_multiple >= self.trailing_break_even_r:
            break_even_price = self.entry_price * \
                (1 - self.trailing_fee_buffer_pct / 100)
            self.stop_loss_price = min(self.stop_loss_price, break_even_price)
            self.break_even_applied = True

        if r_multiple >= self.trailing_activation_r:
            self.trailing_armed = True
            candidate = self.current_price * (1 + trailing_pct / 100)
            ceiling = self.stop_loss_price
            if self.trailing_stop_price <= 0:
                self.trailing_stop_price = min(candidate, ceiling)
            else:
                self.trailing_stop_price = min(
                    self.trailing_stop_price,
                    candidate,
                    ceiling,
                )

    def _update_trailing_legacy(self, trailing_pct: float) -> None:
        if self.side == Side.LONG:
            candidate = self.current_price * (1 - trailing_pct / 100)
            self.trailing_stop_price = max(self.trailing_stop_price, candidate)
        else:
            candidate = self.current_price * (1 + trailing_pct / 100)
            if self.trailing_stop_price == 0:
                self.trailing_stop_price = candidate
            else:
                self.trailing_stop_price = min(
                    self.trailing_stop_price, candidate)

    def should_close(self) -> tuple[bool, str]:
        trailing_active = self.trailing_stop_price and (
            not self.trailing_stage_enabled or self.trailing_armed
        )
        if self.side == Side.LONG:
            if self.current_price <= self.stop_loss_price:
                return True, TradeStatus.STOP_LOSS.value
            if self.current_price >= self.take_profit_price:
                return True, TradeStatus.TAKE_PROFIT.value
            if trailing_active and self.current_price <= self.trailing_stop_price:
                return True, TradeStatus.TRAILING_STOP.value
        else:
            if self.current_price >= self.stop_loss_price:
                return True, TradeStatus.STOP_LOSS.value
            if self.current_price <= self.take_profit_price:
                return True, TradeStatus.TAKE_PROFIT.value
            if trailing_active and self.current_price >= self.trailing_stop_price:
                return True, TradeStatus.TRAILING_STOP.value
        return False, ""


@dataclass(slots=True)
class DashboardMetrics:
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    margin_in_use: float
    liquidation_risk: float
    open_positions: int
    daily_pnl: float
