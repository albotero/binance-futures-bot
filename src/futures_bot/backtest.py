from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import default_strategy_profile, resolve_path
from .execution_paper import PaperExecution
from .market_data import BinanceMarketData
from .models import BotConfig, Position, Side, StrategyProfile, StrategyRule, TradeStatus
from .strategies.engine import StrategyEvaluation, evaluate_profile


def utcstamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class BacktestTrade:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float
    opened_at: str
    closed_at: str
    reason: str


@dataclass(slots=True)
class BacktestSymbolReport:
    symbol: str
    trades: list[BacktestTrade] = field(default_factory=list)
    final_equity: float = 0.0
    net_pnl: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    counted: bool = True
    skip_reason: str = ""


@dataclass(slots=True)
class BacktestReport:
    profile_name: str
    created_at: str
    start_equity: float
    final_equity: float
    net_pnl: float
    win_rate: float
    max_drawdown: float
    requested_symbols: int = 0
    counted_symbols: int = 0
    skipped_symbols: list[str] = field(default_factory=list)
    symbol_reports: list[BacktestSymbolReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["symbol_reports"] = [
            {
                **asdict(symbol_report),
                "trades": [asdict(trade) for trade in symbol_report.trades],
            }
            for symbol_report in self.symbol_reports
        ]
        return payload


@dataclass(slots=True)
class BacktestSuiteResult:
    created_at: str
    reports: list[BacktestReport]

    def to_dict(self) -> dict[str, object]:
        return {
            "created_at": self.created_at,
            "reports": [report.to_dict() for report in self.reports],
        }


def run_backtest_suite(
    config: BotConfig,
    profiles: list[StrategyProfile],
    symbols: list[str] | None = None,
    all_symbols: bool = False,
) -> BacktestSuiteResult:
    market_data = BinanceMarketData(
        config.api_key, config.api_secret, config.binance_base_url)
    selected_symbols = resolve_backtest_symbols(
        config, market_data, symbols=symbols, all_symbols=all_symbols)
    created_at = utcstamp()
    reports = [run_backtest(config, market_data, profile,
                            selected_symbols) for profile in profiles]
    return BacktestSuiteResult(created_at=created_at, reports=reports)


def resolve_backtest_symbols(
    config: BotConfig,
    market_data: BinanceMarketData,
    symbols: list[str] | None = None,
    all_symbols: bool = False,
) -> list[str]:
    if all_symbols:
        fetched = market_data.list_symbols(config.quote_asset)
        normalized = [symbol.strip().upper()
                      for symbol in fetched if symbol and symbol.strip()]
        if not normalized:
            raise ValueError(
                f"No trading symbols found for quote asset {config.quote_asset}")
        return sorted(set(normalized))

    selected = symbols or config.symbols
    normalized = [str(symbol).strip().upper()
                  for symbol in selected if str(symbol).strip()]
    if normalized:
        return normalized
    return [str(symbol).upper() for symbol in config.symbols]


def run_backtest(
    config: BotConfig,
    market_data: BinanceMarketData,
    profile: StrategyProfile,
    symbols: list[str],
) -> BacktestReport:
    if not symbols:
        symbols = list(config.symbols)

    allocation = max(config.initial_equity / max(len(symbols), 1), 1.0)
    symbol_reports: list[BacktestSymbolReport] = []
    skipped_symbols: list[str] = []
    total_final_equity = 0.0
    counted_symbols = 0

    for symbol in symbols:
        try:
            candles = market_data.fetch_candles(
                symbol, config.interval, config.candles_limit)
            if not candles:
                raise ValueError("No candles returned")
            result = _run_symbol_backtest(
                config, profile, symbol, candles, allocation)
            symbol_reports.append(result)
            total_final_equity += result.final_equity
            counted_symbols += 1
        except Exception as exc:  # noqa: BLE001
            skipped_symbols.append(symbol)
            symbol_reports.append(
                BacktestSymbolReport(
                    symbol=symbol,
                    final_equity=0.0,
                    net_pnl=0.0,
                    win_rate=0.0,
                    max_drawdown=0.0,
                    counted=False,
                    skip_reason=str(exc),
                )
            )

    total_start_equity = allocation * counted_symbols

    all_trades = [
        trade for report in symbol_reports for trade in report.trades]
    win_rate = _win_rate(all_trades)
    max_drawdown = max(
        (report.max_drawdown for report in symbol_reports), default=0.0)
    return BacktestReport(
        profile_name=profile.name,
        created_at=utcstamp(),
        start_equity=total_start_equity,
        final_equity=total_final_equity,
        net_pnl=total_final_equity - total_start_equity,
        win_rate=win_rate,
        max_drawdown=max_drawdown,
        requested_symbols=len(symbols),
        counted_symbols=counted_symbols,
        skipped_symbols=skipped_symbols,
        symbol_reports=symbol_reports,
    )


def save_backtest_report(report: BacktestSuiteResult | BacktestReport, data_dir: str | Path) -> Path:
    report_dir = resolve_path(Path(data_dir) / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / \
        f"backtest-{utcstamp().replace(':', '').replace('.', '-')}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return path


def compare_profiles(
    config: BotConfig,
    profile_names: list[str],
    symbols: list[str] | None = None,
    all_symbols: bool = False,
) -> BacktestSuiteResult:
    profiles = [_single_strategy_profile(
        name, config.candle_style) for name in profile_names]
    return run_backtest_suite(config, profiles, symbols, all_symbols=all_symbols)


def _single_strategy_profile(strategy_name: str, candle_style: str) -> StrategyProfile:
    return StrategyProfile(
        name=strategy_name,
        threshold=0.35,
        description=f"Single-strategy comparison profile for {strategy_name}",
        rules=[
            StrategyRule(
                name=strategy_name,
                enabled=True,
                weight=1.0,
                params={"candle_style": candle_style},
            )
        ],
    )


def _run_symbol_backtest(
    config: BotConfig,
    profile: StrategyProfile,
    symbol: str,
    candles: list[dict[str, float]],
    start_equity: float,
) -> BacktestSymbolReport:
    execution = PaperExecution(
        initial_equity=start_equity,
        trailing_stop_pct=config.trailing_stop_pct,
    )
    trades: list[BacktestTrade] = []
    equity_curve: list[float] = [start_equity]
    peak_equity = start_equity
    max_drawdown = 0.0
    warmup = min(max(30, _profile_warmup(profile)), max(len(candles) - 1, 0))

    for index in range(warmup, len(candles)):
        window = candles[: index + 1]
        current_price = float(window[-1]["close"])
        execution.mark_price(symbol, current_price)
        evaluation = evaluate_profile(
            profile, window, symbol, config.risk_reward_ratio)
        _sync_symbol_position(config, execution, profile,
                              symbol, current_price, evaluation, trades)
        equity = execution.snapshot().equity
        equity_curve.append(equity)
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            drawdown = (peak_equity - equity) / peak_equity * 100
            max_drawdown = max(max_drawdown, drawdown)

    closing_price = float(candles[-1]["close"])
    position = execution.get_position(symbol)
    if position:
        closed = execution.close_position(
            symbol, closing_price, TradeStatus.CLOSED.value)
        if closed:
            trades.append(_trade_from_position(
                closed, TradeStatus.CLOSED.value))

    final_equity = execution.snapshot().equity
    return BacktestSymbolReport(
        symbol=symbol,
        trades=trades,
        final_equity=final_equity,
        net_pnl=final_equity - start_equity,
        win_rate=_win_rate(trades),
        max_drawdown=max_drawdown,
    )


def _sync_symbol_position(
    config: BotConfig,
    execution: PaperExecution,
    profile: StrategyProfile,
    symbol: str,
    current_price: float,
    evaluation: StrategyEvaluation,
    trades: list[BacktestTrade],
) -> None:
    existing = execution.get_position(symbol)
    if existing:
        existing.mark(current_price)
        existing.update_trailing_stop(config.trailing_stop_pct)
        should_close, close_reason = existing.should_close()
        if should_close:
            closed = execution.close_position(
                symbol, current_price, close_reason)
            if closed:
                trades.append(_trade_from_position(closed, close_reason))
            return
        if evaluation.action == "hold":
            return
        if (existing.side == Side.LONG and evaluation.action == "short") or (existing.side == Side.SHORT and evaluation.action == "long"):
            closed = execution.close_position(
                symbol, current_price, TradeStatus.REVERSED.value)
            if closed:
                trades.append(_trade_from_position(
                    closed, TradeStatus.REVERSED.value))
            _open_backtest_position(
                config, execution, profile, symbol, current_price, evaluation.action, evaluation)
        return

    _open_backtest_position(config, execution, profile,
                            symbol, current_price, evaluation.action, evaluation)


def _open_backtest_position(
    config: BotConfig,
    execution: PaperExecution,
    profile: StrategyProfile,
    symbol: str,
    current_price: float,
    action: str,
    evaluation: StrategyEvaluation | None = None,
) -> None:
    if action == "hold":
        return
    if action == "short" and not config.allow_short:
        return
    if len(execution.list_positions()) >= config.max_open_positions:
        return

    equity = max(execution.snapshot().equity, 1.0)
    risk_amount = equity * (config.risk_per_trade_pct / 100)
    exit_plan = evaluation.exit_plan if evaluation else None
    stop_distance = abs(
        current_price - exit_plan.stop_loss_price) if exit_plan else current_price * (config.stop_loss_pct / 100)
    if stop_distance <= 0:
        return
    quantity = max(risk_amount / stop_distance, 0.0)
    notional = quantity * current_price
    leverage = min(config.leverage, config.max_leverage)
    max_notional = equity * (config.max_position_pct / 100) * leverage
    if max_notional > 0:
        quantity = min(quantity, max_notional / max(current_price, 1e-9))
    if quantity <= 0:
        return

    side = Side.LONG if action == "long" else Side.SHORT
    stop_loss_price = exit_plan.stop_loss_price if exit_plan else (
        current_price * (1 - config.stop_loss_pct /
                         100) if side == Side.LONG else current_price * (1 + config.stop_loss_pct / 100)
    )
    take_profit_price = exit_plan.take_profit_price if exit_plan else (
        current_price + stop_distance * config.risk_reward_ratio if side == Side.LONG else current_price -
        stop_distance * config.risk_reward_ratio
    )
    trailing_stop_price = exit_plan.trailing_stop_price if exit_plan else (
        current_price * (1 - config.trailing_stop_pct /
                         100) if side == Side.LONG else current_price * (1 + config.trailing_stop_pct / 100)
    )
    position = Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=current_price,
        current_price=current_price,
        leverage=leverage,
        strategy=profile.name,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        trailing_stop_price=trailing_stop_price,
    )
    execution.open_position(position)


def _trade_from_position(position: Position, reason: str) -> BacktestTrade:
    return BacktestTrade(
        symbol=position.symbol,
        side=position.side.value,
        entry_price=position.entry_price,
        exit_price=position.current_price,
        quantity=position.quantity,
        realized_pnl=position.realized_pnl,
        opened_at=position.opened_at,
        closed_at=position.updated_at,
        reason=reason,
    )


def _win_rate(trades: list[BacktestTrade]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for trade in trades if trade.realized_pnl > 0)
    return wins / len(trades) * 100


def _profile_warmup(profile: StrategyProfile) -> int:
    return max((rule.params.get("warmup", 30) for rule in profile.rules), default=30)
