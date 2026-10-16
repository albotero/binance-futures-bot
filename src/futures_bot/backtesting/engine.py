from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from futures_bot.data.candles import next_candle_open
from futures_bot.exchange import FundingRate, Kline, SymbolRules
from futures_bot.risk import AccountState, PortfolioState, RiskDecision, RiskRequest, size_position
from futures_bot.risk.configuration import RiskConfiguration
from futures_bot.risk.engine import UNVERIFIED_SIMULATION_MAX_LEVERAGE
from futures_bot.strategy import EntrySetup, StrategyConfiguration, evaluate_pullback
from futures_bot.strategy.lifecycle import evaluate_entry, evaluate_exit
from futures_bot.strategy.pullback import (
    DEFAULT_TIMEFRAMES,
    TIMEFRAME_PAIRS,
    StructureLevel,
    TimeframePair,
    _validate_candles,
    adverse_fill_price,
    net_reward_to_risk_passes,
)

ZERO = Decimal(0)
BACKTEST_VERSION = "single-symbol-bars-v5"


class BacktestConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    starting_balance: Decimal = Field(gt=0)
    fee_fraction: Decimal = Field(default=Decimal("0.0005"), ge=0, le=Decimal("0.01"))
    slippage_fraction: Decimal = Field(default=Decimal("0.0002"), ge=0, le=Decimal("0.01"))
    maintenance_margin_fraction: Decimal = Field(default=Decimal("0.01"), gt=0, lt=1)
    liquidation_fee_fraction: Decimal = Field(default=Decimal("0.005"), ge=0, le=Decimal("0.1"))
    reporting_timezone: str = "America/Bogota"
    stop_risk_model: Literal["fees_and_slippage", "price_only"] = "fees_and_slippage"

    @field_validator(
        "starting_balance",
        "fee_fraction",
        "slippage_fraction",
        "maintenance_margin_fraction",
        "liquidation_fee_fraction",
        mode="before",
    )
    @classmethod
    def exact_decimal(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("backtest numeric values must be exact decimal strings")
        return value

    @field_validator("reporting_timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("reporting_timezone must be an installed IANA timezone") from exc
        return value


class SimulatedTrade(BaseModel):
    model_config = ConfigDict(frozen=True)

    setup_id: str
    symbol: str
    direction: Literal["long", "short"]
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    leverage: int
    gross_pnl: Decimal
    fees: Decimal
    funding_pnl: Decimal
    net_pnl: Decimal
    exit_reason: str
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    stop_level: StructureLevel | None = None
    target_level: StructureLevel | None = None
    entry_sizing: RiskDecision | None = None


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    time: datetime
    equity: Decimal
    drawdown_fraction: Decimal


class BacktestResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    quote_asset: Literal["USDC", "USDT"]
    start: datetime
    end: datetime
    dataset_checksum: str
    configuration_snapshot: dict[str, object]
    trades: tuple[SimulatedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    risk_rejections: tuple[tuple[datetime, tuple[str, ...]], ...]
    strategy_observations: dict[str, int] = Field(default_factory=dict)
    starting_balance: Decimal
    final_equity: Decimal
    net_pnl: Decimal
    total_fees: Decimal
    funding_pnl: Decimal
    maximum_drawdown_fraction: Decimal
    win_rate: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal
    halted: bool
    limitations: tuple[str, ...]


@dataclass(slots=True)
class _OpenPosition:
    setup: EntrySetup
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    entry_fee: Decimal
    leverage: int
    entry_sizing: RiskDecision | None = None
    funding_pnl: Decimal = ZERO
    bars_held: int = 0


def run_backtest(
    symbol: str,
    hourly_candles: Sequence[Kline],
    entry_candles: Sequence[Kline],
    funding_rates: Sequence[FundingRate],
    rules: SymbolRules,
    strategy: StrategyConfiguration,
    risk: RiskConfiguration,
    configuration: BacktestConfiguration,
    *,
    start: datetime,
    end: datetime,
    timeframes: TimeframePair = DEFAULT_TIMEFRAMES,
) -> BacktestResult:
    """Run one symbol with no network access and at most one open position."""
    with localcontext(Context(prec=40)):
        return _run_backtest(
            symbol,
            hourly_candles,
            entry_candles,
            funding_rates,
            rules,
            strategy,
            risk,
            configuration,
            start=start,
            end=end,
            timeframes=timeframes,
        )


def _run_backtest(
    symbol: str,
    hourly_candles: Sequence[Kline],
    entry_candles: Sequence[Kline],
    funding_rates: Sequence[FundingRate],
    rules: SymbolRules,
    strategy: StrategyConfiguration,
    risk: RiskConfiguration,
    configuration: BacktestConfiguration,
    *,
    start: datetime,
    end: datetime,
    timeframes: TimeframePair = DEFAULT_TIMEFRAMES,
) -> BacktestResult:
    if timeframes not in TIMEFRAME_PAIRS:
        raise ValueError("unsupported entry/trend timeframe pair")
    entry_interval, trend_interval = timeframes
    _validate_range(start, end, entry_interval)
    symbol_settings = next((item for item in risk.symbols if item.symbol == symbol), None)
    if symbol_settings is None or not symbol_settings.enabled:
        raise ValueError("backtest symbol must be enabled in risk configuration")
    if (
        rules.symbol != symbol
        or rules.quote_asset != risk.quote_asset
        or rules.margin_asset != risk.quote_asset
    ):
        raise ValueError("backtest symbol quote and collateral must match risk configuration")
    hourly = tuple(hourly_candles)
    entries = tuple(entry_candles)
    _validate_candles(hourly, trend_interval)
    _validate_candles(entries, entry_interval)
    bars = tuple(candle for candle in entries if start <= candle.open_time < end)
    if (
        not bars
        or bars[0].open_time != start
        or next_candle_open(bars[-1].open_time, entry_interval) != end
    ):
        raise ValueError(
            f"backtest requires complete {entry_interval} coverage of the requested range"
        )
    # Evaluate at the first boundary to require sufficient, gap-free warm-up.
    initial = evaluate_pullback(
        symbol,
        hourly,
        entries,
        rules,
        strategy,
        as_of=start,
        expected_entry_slippage=configuration.slippage_fraction,
        minimum_reward_to_risk=risk.policy.minimum_reward_to_risk,
        expected_fee_fraction=configuration.fee_fraction,
        timeframes=timeframes,
    )
    _require_history(initial.reason)
    rates = _validate_funding(funding_rates, symbol, start, end)
    cash = configuration.starting_balance
    peak = cash
    day_open_equity = cash
    current_day = start.astimezone(ZoneInfo(configuration.reporting_timezone)).date()
    realized_pnl = ZERO
    open_position: _OpenPosition | None = None
    trades: list[SimulatedTrade] = []
    equity_curve = [EquityPoint(time=start, equity=cash, drawdown_fraction=ZERO)]
    rejections: list[tuple[datetime, tuple[str, ...]]] = []
    observations: dict[str, int] = {}
    pending: EntrySetup | None = initial.setup
    funding_index = 0
    halted = False
    timezone = ZoneInfo(configuration.reporting_timezone)
    for candle in bars:
        time = candle.open_time
        entered = False
        occupied_at_open = open_position is not None
        if time.astimezone(timezone).date() != current_day:
            current_day = time.astimezone(timezone).date()
            day_open_equity = equity_curve[-1].equity
        if open_position is not None:
            while funding_index < len(rates) and rates[funding_index].funding_time <= time:
                funding = rates[funding_index]
                if funding.funding_time > open_position.entry_time:
                    payment = _funding_payment(open_position, funding)
                    cash += payment
                    open_position.funding_pnl += payment
                funding_index += 1
        else:
            while funding_index < len(rates) and rates[funding_index].funding_time <= time:
                funding_index += 1
        unrealized = _pnl(open_position, candle.open) if open_position else ZERO
        equity = cash + unrealized
        peak = max(peak, equity)
        if open_position is None and cash <= 0:
            halted = True
            break
        if not occupied_at_open:
            evaluation = evaluate_pullback(
                symbol,
                hourly,
                entries,
                rules,
                strategy,
                as_of=time,
                expected_entry_slippage=configuration.slippage_fraction,
                minimum_reward_to_risk=risk.policy.minimum_reward_to_risk,
                expected_fee_fraction=configuration.fee_fraction,
                timeframes=timeframes,
            )
            observations[evaluation.reason] = observations.get(evaluation.reason, 0) + 1
            _require_history(evaluation.reason)
            if pending is None:
                pending = evaluation.setup
            if pending is not None:
                trigger = evaluate_entry(pending, candle)
                if trigger.status in ("expired", "invalidated"):
                    pending = None
                elif trigger.status == "triggered":
                    assert trigger.fill_price is not None
                    price = _slipped_price(
                        trigger.fill_price,
                        pending.direction == "long",
                        configuration.slippage_fraction,
                        rules.price_tick,
                    )
                    decision = _size_entry(
                        pending,
                        price,
                        cash,
                        equity,
                        realized_pnl,
                        time,
                        peak,
                        day_open_equity,
                        rules,
                        risk,
                        symbol_settings.leverage,
                        fee_fraction=configuration.fee_fraction
                        if configuration.stop_risk_model == "fees_and_slippage"
                        else ZERO,
                        stop_slippage_fraction=configuration.slippage_fraction
                        if configuration.stop_risk_model == "fees_and_slippage"
                        else ZERO,
                    )
                    if decision.accepted:
                        assert decision.quantity is not None
                        quantity = decision.quantity
                        # Fees must fit alongside the reserved margin; never overspend cash.
                        fee = price * quantity * configuration.fee_fraction
                        margin = price * quantity / symbol_settings.leverage
                        reserve = (
                            cash if risk.policy.basis == "available_balance" else equity
                        ) * risk.policy.margin_reserve_fraction
                        if margin + fee > cash - reserve:
                            rejections.append((time, ("entry fee would consume reserved margin",)))
                        else:
                            cash -= fee
                            open_position = _OpenPosition(
                                setup=pending,
                                quantity=quantity,
                                entry_price=price,
                                entry_time=time,
                                entry_fee=fee,
                                leverage=symbol_settings.leverage,
                                entry_sizing=decision,
                            )
                            entered = True
                    else:
                        rejections.append((time, decision.reasons))
                    pending = None
        if open_position is not None:
            open_position.bars_held += 1
            worst_price = candle.low if open_position.setup.direction == "long" else candle.high
            maintenance = (
                open_position.quantity * worst_price * configuration.maintenance_margin_fraction
            )
            exit_result = evaluate_exit(
                open_position.setup, candle, bars_held=open_position.bars_held, entry_bar=entered
            )
            ambiguous_funding = (
                entered
                or (exit_result is not None and exit_result.reason in ("stop", "target"))
                or cash + _pnl(open_position, worst_price) <= maintenance
            )
            while (
                funding_index < len(rates)
                and rates[funding_index].funding_time <= candle.close_time
            ):
                payment = _funding_payment(open_position, rates[funding_index])
                if ambiguous_funding:
                    payment = min(ZERO, payment)
                cash += payment
                open_position.funding_pnl += payment
                funding_index += 1
            # A bar cannot establish ordering around liquidation: use its adverse extreme.
            liquidated = cash + _pnl(open_position, worst_price) <= maintenance
            exit_price: Decimal | None = None
            reason = ""
            if liquidated:
                reason = "conservative_liquidation"
                exit_price = worst_price
            elif exit_result is not None:
                reason = exit_result.reason
                exit_price = exit_result.price
            elif candle is bars[-1]:
                reason = "end_of_data"
                exit_price = candle.close
            if exit_price is not None:
                exit_price = _slipped_price(
                    exit_price,
                    open_position.setup.direction == "short",
                    configuration.slippage_fraction,
                    rules.price_tick,
                )
                gross = _pnl(open_position, exit_price)
                fee_fraction = configuration.fee_fraction
                if liquidated:
                    fee_fraction += configuration.liquidation_fee_fraction
                exit_fee = open_position.quantity * exit_price * fee_fraction
                cash += gross - exit_fee
                fees = open_position.entry_fee + exit_fee
                realized_pnl += gross - fees + open_position.funding_pnl
                trades.append(
                    SimulatedTrade(
                        setup_id=open_position.setup.id,
                        symbol=symbol,
                        direction=open_position.setup.direction,
                        entry_time=open_position.entry_time,
                        exit_time=candle.close_time,
                        entry_price=open_position.entry_price,
                        exit_price=exit_price,
                        quantity=open_position.quantity,
                        leverage=open_position.leverage,
                        gross_pnl=gross,
                        fees=fees,
                        funding_pnl=open_position.funding_pnl,
                        net_pnl=gross - fees + open_position.funding_pnl,
                        exit_reason=reason,
                        stop_price=open_position.setup.stop_price,
                        target_price=open_position.setup.target_price,
                        stop_level=open_position.setup.stop_level,
                        target_level=open_position.setup.target_level,
                        entry_sizing=open_position.entry_sizing,
                    )
                )
                open_position = None
                if liquidated:
                    halted = True
        closing_equity = cash + (_pnl(open_position, candle.close) if open_position else ZERO)
        peak = max(peak, closing_equity)
        equity_curve.append(
            EquityPoint(
                time=candle.close_time + timedelta(milliseconds=1),
                equity=closing_equity,
                drawdown_fraction=(peak - closing_equity) / peak,
            )
        )
        if halted or cash <= 0:
            halted = True
            break
    wins = sum((trade.net_pnl for trade in trades if trade.net_pnl > 0), ZERO)
    losses = -sum((trade.net_pnl for trade in trades if trade.net_pnl < 0), ZERO)
    net = cash - configuration.starting_balance
    return BacktestResult(
        symbol=symbol,
        quote_asset=risk.quote_asset,
        start=start,
        end=end,
        dataset_checksum=_dataset_checksum(hourly, entries, rates, timeframes),
        configuration_snapshot={
            "engine_version": BACKTEST_VERSION,
            "strategy_version": strategy.implementation,
            "timeframes": {"entry": entry_interval, "trend": trend_interval},
            "strategy": strategy.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
            "simulation": configuration.model_dump(mode="json"),
            "rules": _json_values(asdict(rules)),
        },
        trades=tuple(trades),
        equity_curve=tuple(equity_curve),
        risk_rejections=tuple(rejections),
        strategy_observations=observations,
        starting_balance=configuration.starting_balance,
        final_equity=cash,
        net_pnl=net,
        total_fees=sum((trade.fees for trade in trades), ZERO),
        funding_pnl=sum((trade.funding_pnl for trade in trades), ZERO),
        maximum_drawdown_fraction=max(point.drawdown_fraction for point in equity_curve),
        win_rate=Decimal(sum(trade.net_pnl > 0 for trade in trades)) / len(trades)
        if trades
        else ZERO,
        profit_factor=wins / losses if losses else None,
        expectancy=net / len(trades) if trades else ZERO,
        halted=halted,
        limitations=(
            "Single symbol, single concurrent position; no portfolio allocation model.",
            "Trade timestamps are bar bounds, not reconstructed intrabar execution times.",
            "OHLC ambiguity is stop-first; liquidation stress uses the adverse bar extreme first.",
            "Maintenance and liquidation fees are assumptions, not exchange bracket replication.",
            "Historical funding must be supplied; an empty series explicitly models zero funding.",
            "Intrabar entry/exit funding ambiguity charges debits but omits uncertain credits.",
            "Static exchange rules and leverage limits are assumed throughout the range.",
            "Cost-inclusive stop sizing models both fees and adverse stop slippage; legacy "
            "price-only mode excludes costs. Gaps, funding or liquidation can exceed the budget.",
            "Remaining positions close at the dataset end for accounting, not a holding timer.",
            "The entry cost filter models fees/slippage, not future funding or gap losses.",
        ),
    )


def _size_entry(
    setup: EntrySetup,
    price: Decimal,
    cash: Decimal,
    equity: Decimal,
    realized_pnl: Decimal,
    time: datetime,
    peak: Decimal,
    day_open_equity: Decimal,
    rules: SymbolRules,
    risk: RiskConfiguration,
    leverage: int,
    *,
    fee_fraction: Decimal = ZERO,
    stop_slippage_fraction: Decimal = ZERO,
) -> RiskDecision:
    account = AccountState(
        wallet_balance=max(ZERO, cash),
        available_balance=max(ZERO, cash),
        equity=max(ZERO, equity),
        unrealized_pnl=ZERO,
        realized_pnl=realized_pnl,
        used_initial_margin=ZERO,
        open_exposure=ZERO,
        captured_at=time,
        quote_asset=risk.quote_asset,
    )
    portfolio = PortfolioState(
        open_position_count=0,
        aggregate_open_risk=ZERO,
        daily_loss=max(ZERO, day_open_equity - equity),
        drawdown_fraction=min(Decimal(1), max(ZERO, (peak - equity) / peak)),
        protective_orders_healthy=True,
        connectivity_healthy=True,
    )
    schedule = next(
        (item.leverage_schedule for item in risk.symbols if item.symbol == setup.symbol), None
    )
    request = RiskRequest(
        symbol=setup.symbol,
        direction=setup.direction,
        entry_price=price,
        stop_price=setup.stop_price,
        target_price=setup.target_price,
        leverage=leverage,
        exchange_max_leverage=schedule.maximum_leverage
        if schedule is not None
        else UNVERIFIED_SIMULATION_MAX_LEVERAGE,
        symbol_rules=rules,
        market_time=time,
        exchange_notional_cap=schedule.notional_limit(leverage) if schedule is not None else None,
        fee_fraction=fee_fraction,
        stop_slippage_fraction=stop_slippage_fraction,
    )
    decision = size_position(request, account, portfolio, risk.policy, now=time)
    if decision.accepted and setup.minimum_reward_to_risk is not None:
        reward = (
            setup.target_price - price if setup.direction == "long" else price - setup.target_price
        )
        if reward < abs(price - setup.stop_price) * setup.minimum_reward_to_risk:
            return RiskDecision(
                accepted=False,
                reasons=("actual fill fails the setup minimum reward-to-risk",),
                risk_budget=decision.risk_budget,
                stop_distance=decision.stop_distance,
            )
        if setup.expected_fee_fraction is not None and not net_reward_to_risk_passes(
            price,
            setup.stop_price,
            setup.target_price,
            setup.direction == "long",
            setup.expected_fee_fraction,
            setup.expected_exit_slippage,
            rules.price_tick,
            setup.minimum_reward_to_risk,
        ):
            return RiskDecision(
                accepted=False,
                reasons=("actual fill fails the setup cost-adjusted minimum reward-to-risk",),
                risk_budget=decision.risk_budget,
                stop_distance=decision.stop_distance,
            )
    return decision


def _require_history(reason: str) -> None:
    if reason in (
        "indicator warm-up is incomplete",
        "latest closed entry candle is stale",
        "latest closed trend candle is stale",
    ):
        raise ValueError(f"backtest history is not ready: {reason}")


def _pnl(position: _OpenPosition, price: Decimal) -> Decimal:
    difference = price - position.entry_price
    return position.quantity * (difference if position.setup.direction == "long" else -difference)


def _funding_payment(position: _OpenPosition, funding: FundingRate) -> Decimal:
    payment = position.quantity * funding.mark_price * funding.rate
    return -payment if position.setup.direction == "long" else payment


def _slipped_price(price: Decimal, buy: bool, fraction: Decimal, tick: Decimal) -> Decimal:
    return adverse_fill_price(price, buy, fraction, tick)


def _validate_range(start: datetime, end: datetime, interval: str = "15m") -> None:
    for stamp in (start, end):
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("backtest dates must be timezone-aware")
        utc = stamp.astimezone(UTC)
        seconds = int((next_candle_open(utc, interval) - utc).total_seconds())
        if utc.second or utc.microsecond or (utc.hour * 3600 + utc.minute * 60) % seconds:
            raise ValueError(f"backtest boundaries must align to {interval} opens")
    if end <= start:
        raise ValueError("backtest end must follow start")


def _validate_funding(
    rates: Sequence[FundingRate], symbol: str, start: datetime, end: datetime
) -> tuple[FundingRate, ...]:
    output: list[FundingRate] = []
    previous: datetime | None = None
    for rate in rates:
        if rate.symbol != symbol:
            raise ValueError("funding symbol must match the backtest")
        stamp = rate.funding_time
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("funding timestamps must be timezone-aware")
        if previous is not None and stamp <= previous:
            raise ValueError("funding rates must be strictly ordered and unique")
        if not rate.rate.is_finite() or not rate.mark_price.is_finite() or rate.mark_price <= 0:
            raise ValueError("funding values must be finite with a positive mark price")
        previous = stamp
        if start <= stamp < end:
            output.append(rate)
    return tuple(output)


def _json_values(values: object) -> object:
    if isinstance(values, dict):
        return {key: _json_values(value) for key, value in values.items()}
    if isinstance(values, (list, tuple)):
        return [_json_values(value) for value in values]
    if isinstance(values, Decimal):
        return str(values)
    if isinstance(values, datetime):
        return values.astimezone(UTC).isoformat()
    return values


def _dataset_checksum(
    hourly: Sequence[Kline],
    entries: Sequence[Kline],
    funding: Sequence[FundingRate],
    timeframes: TimeframePair = DEFAULT_TIMEFRAMES,
) -> str:
    entry_interval, trend_interval = timeframes
    values = {
        trend_interval: [asdict(candle) for candle in hourly],
        entry_interval: [asdict(candle) for candle in entries],
        "funding": [asdict(rate) for rate in funding],
    }
    canonical = json.dumps(_json_values(values), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
