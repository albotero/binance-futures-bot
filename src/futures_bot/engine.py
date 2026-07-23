from __future__ import annotations

import json
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
    started_at: str = ""
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
            "started_at": self.started_at,
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
        self._restore_account_metrics()
        self.profile = load_strategy_profile(config, config.strategy_profile)
        self.thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def _create_execution(self, config: BotConfig) -> BaseExecution:
        if config.mode.lower() == "live":
            return BinanceFuturesExecution(
                initial_equity=config.initial_equity,
                trailing_stop_pct=config.trailing_stop_pct,
                trailing_stage_enabled=config.trailing_stage_enabled,
                trailing_break_even_r=config.trailing_break_even_r,
                trailing_activation_r=config.trailing_activation_r,
                trailing_fee_buffer_pct=config.trailing_fee_buffer_pct,
                api_key=config.api_key,
                api_secret=config.api_secret,
                base_url=config.binance_base_url,
                live_protection_mode=config.live_protection_mode,
            )
        return PaperExecution(
            initial_equity=config.initial_equity,
            trailing_stop_pct=config.trailing_stop_pct,
            trailing_stage_enabled=config.trailing_stage_enabled,
            trailing_break_even_r=config.trailing_break_even_r,
            trailing_activation_r=config.trailing_activation_r,
            trailing_fee_buffer_pct=config.trailing_fee_buffer_pct,
        )

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
            self.state.running = True
            return
        self._stop_event.clear()
        self.state.running = True
        if not self.state.started_at:
            self.state.started_at = utcnow().isoformat()
        self.thread = threading.Thread(
            target=self._run_loop, name="trading-engine", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.state.running = False
        self.state.started_at = ""

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
        with self._lock:
            position = self.execution.get_position(symbol)
            if not position:
                return None
            current_price = self.data.latest_price(symbol)
            closed = self.execution.close_position(
                symbol, current_price, TradeStatus.MANUAL_CLOSE.value)
            if closed:
                self.storage.record_close(closed)
                self.state.halted_symbols.add(symbol)
                self._record_snapshot(utcnow().isoformat())
            return closed

    def status(self) -> dict:
        thread_alive = bool(self.thread and self.thread.is_alive())
        self.state.running = thread_alive
        if not thread_alive:
            self.state.started_at = ""
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

    def sync_exchange_history(self) -> dict[str, int]:
        if self.config.mode.lower() != "live":
            return {"trades": 0, "updated": 0}
        if not isinstance(self.execution, BinanceFuturesExecution):
            return {"trades": 0, "updated": 0}

        updated = 0
        trades = self.storage.list_all_trades()
        grouped: dict[str, list[dict]] = {}
        for trade in trades:
            grouped.setdefault(
                str(trade.get("symbol", "")).upper(), []).append(trade)

        for symbol, symbol_trades in grouped.items():
            if not symbol:
                continue
            first_opened = str(symbol_trades[0].get("opened_at") or "")
            if not first_opened:
                continue
            fills = self.execution.exchange_fill_history(
                symbol,
                start_time_ms=_iso_to_ms(first_opened),
            )
            used_order_ids: set[int] = set()
            for trade in symbol_trades:
                metadata = _parse_trade_metadata(trade.get("metadata"))
                entry_fill, exit_fill = _match_trade_fills_for_history(
                    fills,
                    trade_side=str(trade.get("side", "")),
                    quantity=float(trade.get("quantity") or 0.0),
                    opened_at=str(trade.get("opened_at") or ""),
                    used_order_ids=used_order_ids,
                    entry_order_id=metadata.get("entry_order_id"),
                    exit_order_id=metadata.get("exit_order_id"),
                )
                updates: dict[str, object] = {}
                metadata_updates: dict[str, object] = {}
                if entry_fill is not None:
                    updates["entry_price"] = entry_fill.average_price
                    updates["opened_at"] = entry_fill.fill_time
                    metadata_updates["entry_order_id"] = entry_fill.order_id
                    used_order_ids.add(entry_fill.order_id)
                if exit_fill is not None:
                    updates["exit_price"] = exit_fill.average_price
                    updates["closed_at"] = exit_fill.fill_time
                    updates["realized_pnl"] = _estimate_realized_pnl(
                        side=str(trade.get("side", "")),
                        quantity=float(trade.get("quantity") or 0.0),
                        entry_price=float(updates.get(
                            "entry_price", trade.get("entry_price") or 0.0)),
                        exit_price=exit_fill.average_price,
                    )
                    metadata_updates["exit_order_id"] = exit_fill.order_id
                    used_order_ids.add(exit_fill.order_id)
                if updates or metadata_updates:
                    self.storage.update_trade_from_exchange(
                        int(trade["id"]),
                        entry_price=updates.get(
                            "entry_price") if "entry_price" in updates else None,
                        exit_price=updates.get(
                            "exit_price") if "exit_price" in updates else None,
                        realized_pnl=updates.get(
                            "realized_pnl") if "realized_pnl" in updates else None,
                        opened_at=updates.get(
                            "opened_at") if "opened_at" in updates else None,
                        closed_at=updates.get(
                            "closed_at") if "closed_at" in updates else None,
                        metadata_updates=metadata_updates,
                    )
                    updated += 1

        self._restore_account_metrics()
        return {"trades": len(trades), "updated": updated}

    def run_once(self) -> None:
        with self._lock:
            symbols = self.list_symbols()
            self.state.active_symbols = symbols
            self.state.last_run_at = utcnow().isoformat()
            self.state.latest_actions.clear()
            self.state.latest_scores.clear()
            self.state.latest_reasons.clear()

            self._reconcile_exchange_positions(symbols)

            for symbol in symbols:
                if symbol in self.state.halted_symbols:
                    continue
                try:
                    candles = self.data.fetch_candles(
                        symbol, self.config.interval, self.config.candles_limit)
                    current_price = float(candles[-1]["close"])
                    self.execution.mark_price(symbol, current_price)
                    evaluation = evaluate_profile(
                        self.profile, candles, symbol, self.config.risk_reward_ratio)
                    self.state.latest_actions[symbol] = evaluation.action
                    self.state.latest_scores[symbol] = evaluation.score
                    self.state.latest_reasons[symbol] = evaluation.reasons
                    self._sync_position(symbol, current_price, evaluation)
                except Exception as exc:  # noqa: BLE001
                    self.state.last_error = f"{symbol}: {exc}"

            self._record_snapshot(self.state.last_run_at)

    def _reconcile_exchange_positions(self, active_symbols: list[str]) -> None:
        if self.config.mode.lower() != "live":
            return
        if not isinstance(self.execution, BinanceFuturesExecution):
            return

        open_trades = self.storage.list_open_trades()
        if not open_trades:
            return

        snapshots: dict[str, tuple[float, float]] = {}
        for trade in open_trades:
            symbol = str(trade.get("symbol", "")).upper()
            if not symbol:
                continue
            if symbol not in snapshots:
                snapshots[symbol] = self.execution.exchange_position_snapshot(
                    symbol)
            position_qty, exchange_mark_price = snapshots[symbol]
            if abs(position_qty) > 1e-10:
                continue

            entry_fill, exit_fill = self.execution.find_exchange_trade_fills(
                symbol,
                trade_side=str(trade.get("side", "")),
                quantity=float(trade.get("quantity") or 0.0),
                opened_at=str(trade.get("opened_at") or utcnow().isoformat()),
            )

            exit_price = exit_fill.average_price if exit_fill else exchange_mark_price
            if exit_price <= 0:
                exit_price = float(trade.get("entry_price") or 0.0)

            local_position = self.execution.get_position(symbol)
            if local_position:
                closed = self.execution.close_position_from_exchange(
                    symbol,
                    exit_price,
                    TradeStatus.EXTERNAL_CLOSE.value,
                    fill_time=exit_fill.fill_time if exit_fill else None,
                    order_id=exit_fill.order_id if exit_fill else None,
                )
                if closed:
                    self.storage.record_close(closed)
                    continue

            realized_pnl = _estimate_realized_pnl(
                side=str(trade.get("side", "")).upper(),
                quantity=float(trade.get("quantity") or 0.0),
                entry_price=float(trade.get("entry_price") or exit_price),
                exit_price=exit_price,
            )
            self.storage.close_trade_by_id(
                int(trade["id"]),
                exit_price=exit_price,
                realized_pnl=realized_pnl,
                status=TradeStatus.EXTERNAL_CLOSE.value,
                close_reason=TradeStatus.EXTERNAL_CLOSE.value,
                closed_at=exit_fill.fill_time if exit_fill else utcnow().isoformat(),
            )
            self.execution.realized_pnl += realized_pnl
            self.execution.balance += realized_pnl

    def _restore_account_metrics(self) -> None:
        realized = self.storage.total_realized_pnl()
        self.execution.realized_pnl = realized
        self.execution.balance = self.execution.initial_equity + realized

    def _record_snapshot(self, created_at: str) -> None:
        self.storage.store_snapshot({
            "created_at": created_at,
            "metrics": asdict(self.execution.snapshot()),
            "state": self.state.to_dict(),
        })

    def _sync_position(self, symbol: str, current_price: float, evaluation: StrategyEvaluation) -> None:
        existing = self.execution.get_position(symbol)
        if existing:
            self.execution.mark_price(symbol, current_price)
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
                    symbol, current_price, evaluation.action, evaluation)
            return

        self._open_from_signal(symbol, current_price,
                               evaluation.action, evaluation)

    def _open_from_signal(self, symbol: str, current_price: float, action: str, evaluation: StrategyEvaluation | None = None) -> None:
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
        exit_plan = evaluation.exit_plan if evaluation else None
        stop_distance = abs(
            current_price - exit_plan.stop_loss_price) if exit_plan else current_price * (self.config.stop_loss_pct / 100)
        if stop_distance <= 0:
            return
        quantity = max((risk_amount / stop_distance), 0.0)
        max_notional = equity * (self.config.max_position_pct / 100) * leverage
        if max_notional > 0:
            quantity = min(quantity, max_notional / max(current_price, 1e-9))
        if quantity <= 0:
            return
        side = Side.LONG if action == "long" else Side.SHORT
        stop_loss_price = exit_plan.stop_loss_price if exit_plan else (
            current_price * (1 - self.config.stop_loss_pct /
                             100) if side == Side.LONG else current_price * (1 + self.config.stop_loss_pct / 100)
        )
        take_profit_price = exit_plan.take_profit_price if exit_plan else (
            current_price + stop_distance * self.config.risk_reward_ratio if side == Side.LONG else current_price -
            stop_distance * self.config.risk_reward_ratio
        )
        trailing_stop_price = exit_plan.trailing_stop_price if exit_plan else (
            current_price * (1 - self.config.trailing_stop_pct /
                             100) if side == Side.LONG else current_price * (1 + self.config.trailing_stop_pct / 100)
        )
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
            trailing_stage_enabled=self.config.trailing_stage_enabled,
            trailing_break_even_r=self.config.trailing_break_even_r,
            trailing_activation_r=self.config.trailing_activation_r,
            trailing_fee_buffer_pct=self.config.trailing_fee_buffer_pct,
            initial_stop_loss_price=stop_loss_price,
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


def _estimate_realized_pnl(side: str, quantity: float, entry_price: float, exit_price: float) -> float:
    if side == Side.SHORT.value:
        return (entry_price - exit_price) * quantity
    return (exit_price - entry_price) * quantity


def _parse_trade_metadata(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _match_trade_fills_for_history(
    fills: list,
    *,
    trade_side: str,
    quantity: float,
    opened_at: str,
    used_order_ids: set[int],
    entry_order_id: object = None,
    exit_order_id: object = None,
):
    from .execution_live import ExchangeFillSummary, _match_trade_to_fills

    available = [fill for fill in fills if fill.order_id not in used_order_ids]
    entry_fill, close_fill = _match_trade_to_fills(
        available,
        trade_side=trade_side,
        quantity=quantity,
        opened_at_ms=_iso_to_ms(opened_at),
    )
    if entry_order_id is not None:
        entry_fill = next(
            (fill for fill in fills if fill.order_id == int(entry_order_id)), entry_fill)
    if exit_order_id is not None:
        close_fill = next(
            (fill for fill in fills if fill.order_id == int(exit_order_id)), close_fill)
    return entry_fill, close_fill


def _iso_to_ms(raw: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(raw).timestamp() * 1000)
