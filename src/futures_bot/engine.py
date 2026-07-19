from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import load_strategy_profile, save_strategy_profile
from .execution import BaseExecution
from .execution_live import BinanceFuturesExecution
from .execution_paper import PaperExecution
from .market_data import BinanceMarketData
from .models import BotConfig, DashboardMetrics, Position, Side, StrategyProfile, TradeStatus, utcnow
from .storage import SQLiteStore
from .strategies.engine import StrategyEvaluation, evaluate_profile


@dataclass(slots=True)
class BotState:
    running: bool = False
    paused: bool = False
    last_run_at: str = ""
    last_error: str = ""
    active_symbols: list[str] = field(default_factory=list)
    selected_profile: str = "default"
    latest_actions: dict[str, str] = field(default_factory=dict)
    latest_scores: dict[str, float] = field(default_factory=dict)
    latest_reasons: dict[str, list[str]] = field(default_factory=dict)
    halted_symbols: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "paused": self.paused,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "active_symbols": list(self.active_symbols),
            "selected_profile": self.selected_profile,
            "latest_actions": dict(self.latest_actions),
            "latest_scores": dict(self.latest_scores),
            "latest_reasons": {symbol: list(reasons) for symbol, reasons in self.latest_reasons.items()},
            "halted_symbols": sorted(self.halted_symbols),
        }


class TradingEngine:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self._validate_mode()
        self.state = BotState(selected_profile=config.strategy_profile)
        self.data = BinanceMarketData(
            config.api_key, config.api_secret, config.binance_base_url)
        self.execution: BaseExecution = self._create_execution(config)
        self.storage = SQLiteStore(config.db_path)
        self.profile = load_strategy_profile(config, config.strategy_profile)
        self.thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def _create_execution(self, config: BotConfig) -> BaseExecution:
        if config.mode.lower() == "live":
            return BinanceFuturesExecution(initial_equity=config.initial_equity, api_key=config.api_key, api_secret=config.api_secret, base_url=config.binance_base_url)
        return PaperExecution(initial_equity=config.initial_equity)

    def _validate_mode(self) -> None:
        if self.config.mode.lower() != "live":
            return
        if self.config.testnet:
            return
        if not self.config.live_trading_confirmed:
            raise ValueError(
                "Live trading requires BOT_LIVE_TRADING_CONFIRMED=true or BINANCE_TESTNET=true"
            )

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop_event.clear()
        self.state.running = True
        self.thread = threading.Thread(
            target=self._run_loop, name="trading-engine", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.state.running = False

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False

    def reload_profile(self, name: str | None = None) -> StrategyProfile:
        self.profile = load_strategy_profile(
            self.config, name or self.config.strategy_profile)
        self.state.selected_profile = self.profile.name
        return self.profile

    def save_profile(self, profile: StrategyProfile) -> Path:
        path = save_strategy_profile(self.config, profile)
        self.profile = profile
        self.state.selected_profile = profile.name
        return path

    def list_symbols(self) -> list[str]:
        if self.config.trade_all_symbols:
            return self.data.list_symbols(self.config.quote_asset)
        return self.config.symbols

    def manually_close(self, symbol: str) -> Position | None:
        position = self.execution.get_position(symbol)
        if not position:
            return None
        current_price = self.data.latest_price(symbol)
        closed = self.execution.close_position(
            symbol, current_price, TradeStatus.MANUAL_CLOSE.value)
        if closed:
            self.storage.record_close(closed)
            self.state.halted_symbols.add(symbol)
        return closed

    def status(self) -> dict:
        metrics = self.execution.snapshot()
        positions = [position.to_dict()
                     for position in self.execution.list_positions()]
        return {
            "state": self.state.to_dict(),
            "metrics": asdict(metrics),
            "positions": positions,
            "profile": self.profile.to_dict(),
            "config": self.config.to_dict(),
        }

    def snapshot(self) -> DashboardMetrics:
        return self.execution.snapshot()

    def run_once(self) -> None:
        with self._lock:
            symbols = self.list_symbols()
            self.state.active_symbols = symbols
            self.state.last_run_at = utcnow().isoformat()
            self.state.latest_actions.clear()
            self.state.latest_scores.clear()
            self.state.latest_reasons.clear()

            for symbol in symbols:
                if symbol in self.state.halted_symbols:
                    continue
                try:
                    candles = self.data.fetch_candles(
                        symbol, self.config.interval, self.config.candles_limit)
                    current_price = float(candles[-1]["close"])
                    self.execution.mark_price(symbol, current_price)
                    evaluation = evaluate_profile(
                        self.profile, candles, symbol)
                    self.state.latest_actions[symbol] = evaluation.action
                    self.state.latest_scores[symbol] = evaluation.score
                    self.state.latest_reasons[symbol] = evaluation.reasons
                    self._sync_position(symbol, current_price, evaluation)
                except Exception as exc:  # noqa: BLE001
                    self.state.last_error = f"{symbol}: {exc}"

            self.storage.store_snapshot({
                "created_at": self.state.last_run_at,
                "metrics": asdict(self.execution.snapshot()),
                "state": self.state.to_dict(),
            })

    def _sync_position(self, symbol: str, current_price: float, evaluation: StrategyEvaluation) -> None:
        existing = self.execution.get_position(symbol)
        if existing:
            existing.mark(current_price)
            should_close, close_reason = existing.should_close()
            if should_close:
                closed = self.execution.close_position(
                    symbol, current_price, close_reason)
                if closed:
                    self.storage.record_close(closed)
                return
            if evaluation.action == "hold":
                return
            if (existing.side == Side.LONG and evaluation.action == "short") or (existing.side == Side.SHORT and evaluation.action == "long"):
                closed = self.execution.close_position(
                    symbol, current_price, TradeStatus.REVERSED.value)
                if closed:
                    self.storage.record_close(closed)
                self._open_from_signal(
                    symbol, current_price, evaluation.action)
            return

        self._open_from_signal(symbol, current_price, evaluation.action)

    def _open_from_signal(self, symbol: str, current_price: float, action: str) -> None:
        if self.config.mode.lower() == "live" and not self.config.testnet and not self.config.live_trading_confirmed:
            return
        if len(self.execution.list_positions()) >= self.config.max_open_positions:
            return
        if action == "hold":
            return
        if action == "short" and not self.config.allow_short:
            return

        equity = max(self.execution.snapshot().equity, 1.0)
        if self.execution.snapshot().daily_pnl <= -(self.config.initial_equity * self.config.max_daily_loss_pct / 100):
            self.state.paused = True
            self.state.last_error = "Daily loss limit reached"
            return
        if self.execution.snapshot().margin_in_use > equity * (1 - self.config.min_margin_buffer_pct / 100):
            return
        leverage = min(self.config.leverage, self.config.max_leverage)
        if leverage <= 0:
            return
        risk_amount = equity * (self.config.risk_per_trade_pct / 100)
        stop_distance = current_price * (self.config.stop_loss_pct / 100)
        if stop_distance <= 0:
            return
        quantity = max((risk_amount / stop_distance), 0.0)
        max_notional = equity * (self.config.max_position_pct / 100)
        if max_notional > 0:
            quantity = min(quantity, max_notional / max(current_price, 1e-9))
        if quantity <= 0:
            return
        side = Side.LONG if action == "long" else Side.SHORT
        stop_loss_price = current_price * \
            (1 - self.config.stop_loss_pct / 100) if side == Side.LONG else current_price * \
            (1 + self.config.stop_loss_pct / 100)
        take_profit_price = current_price * \
            (1 + self.config.take_profit_pct / 100) if side == Side.LONG else current_price * \
            (1 - self.config.take_profit_pct / 100)
        trailing_stop_price = current_price * \
            (1 - self.config.trailing_stop_pct / 100) if side == Side.LONG else current_price * \
            (1 + self.config.trailing_stop_pct / 100)
        position = Position(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=current_price,
            current_price=current_price,
            leverage=leverage,
            strategy=self.profile.name,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            trailing_stop_price=trailing_stop_price,
        )
        opened = self.execution.open_position(position)
        self.storage.record_open(
            opened, {"action": action, "profile": self.profile.name})

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.state.paused:
                self.run_once()
            time.sleep(self.config.poll_seconds)


def load_engine(config: BotConfig | None = None) -> TradingEngine:
    return TradingEngine(config or BotConfig())
