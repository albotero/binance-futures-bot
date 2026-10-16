from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from futures_bot.exchange.binance_usdm import SymbolRules, validate_symbol_name

RiskBasis = Literal["available_balance", "equity"]
Direction = Literal["long", "short"]
_ZERO = Decimal(0)
_ONE = Decimal(1)
UNVERIFIED_SIMULATION_MAX_LEVERAGE = 50


@dataclass(frozen=True, slots=True)
class AccountState:
    wallet_balance: Decimal
    available_balance: Decimal
    equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    used_initial_margin: Decimal
    open_exposure: Decimal
    captured_at: datetime
    quote_asset: Literal["USDC", "USDT"] = "USDC"

    def __post_init__(self) -> None:
        if self.quote_asset not in ("USDC", "USDT"):
            raise ValueError("account quote asset must be USDC or USDT")
        for name in (
            "wallet_balance",
            "available_balance",
            "equity",
            "unrealized_pnl",
            "realized_pnl",
            "used_initial_margin",
            "open_exposure",
        ):
            _require_decimal(getattr(self, name), name)
        for name in (
            "wallet_balance",
            "available_balance",
            "equity",
            "used_initial_margin",
            "open_exposure",
        ):
            if getattr(self, name) < _ZERO:
                raise ValueError(f"{name} cannot be negative")
        _require_aware(self.captured_at, "captured_at")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    open_position_count: int
    aggregate_open_risk: Decimal
    daily_loss: Decimal
    drawdown_fraction: Decimal
    protective_orders_healthy: bool
    connectivity_healthy: bool

    def __post_init__(self) -> None:
        if type(self.open_position_count) is not int or self.open_position_count < 0:
            raise ValueError("open_position_count must be a non-negative integer")
        _require_decimal(self.aggregate_open_risk, "aggregate_open_risk")
        _require_decimal(self.daily_loss, "daily_loss")
        _require_decimal(self.drawdown_fraction, "drawdown_fraction")
        if self.aggregate_open_risk < _ZERO or self.daily_loss < _ZERO:
            raise ValueError("portfolio risk and daily loss cannot be negative")
        if not _ZERO <= self.drawdown_fraction <= _ONE:
            raise ValueError("drawdown_fraction must be between 0 and 1")
        if type(self.protective_orders_healthy) is not bool:
            raise ValueError("protective_orders_healthy must be a boolean")
        if type(self.connectivity_healthy) is not bool:
            raise ValueError("connectivity_healthy must be a boolean")


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    basis: RiskBasis
    risk_fraction: Decimal
    margin_reserve_fraction: Decimal
    maximum_position_notional: Decimal
    maximum_total_exposure: Decimal
    maximum_aggregate_risk: Decimal
    maximum_daily_loss: Decimal
    maximum_drawdown_fraction: Decimal
    maximum_stop_distance_fraction: Decimal
    minimum_reward_to_risk: Decimal
    maximum_open_positions: int
    # Legacy snapshot field only; leverage is now constrained per symbol.
    maximum_leverage: int = 20
    maximum_account_age: timedelta = timedelta(seconds=15)
    maximum_market_age: timedelta = timedelta(seconds=15)

    def __post_init__(self) -> None:
        if self.basis not in ("available_balance", "equity"):
            raise ValueError("basis must be available_balance or equity")
        for name in (
            "risk_fraction",
            "margin_reserve_fraction",
            "maximum_drawdown_fraction",
            "maximum_stop_distance_fraction",
        ):
            value = getattr(self, name)
            _require_decimal(value, name)
            if not _ZERO < value <= _ONE:
                raise ValueError(f"{name} must be greater than 0 and at most 1")
        for name in (
            "maximum_position_notional",
            "maximum_total_exposure",
            "maximum_aggregate_risk",
            "maximum_daily_loss",
            "minimum_reward_to_risk",
        ):
            value = getattr(self, name)
            _require_decimal(value, name)
            if value <= _ZERO:
                raise ValueError(f"{name} must be greater than 0")
        if type(self.maximum_open_positions) is not int or self.maximum_open_positions < 1:
            raise ValueError("maximum_open_positions must be a positive integer")
        if type(self.maximum_leverage) is not int or self.maximum_leverage < 1:
            raise ValueError("legacy maximum_leverage must be a positive integer")
        for name in ("maximum_account_age", "maximum_market_age"):
            age = getattr(self, name)
            if not isinstance(age, timedelta) or age <= timedelta(0):
                raise ValueError(f"{name} must be a positive duration")


@dataclass(frozen=True, slots=True)
class RiskRequest:
    symbol: str
    direction: Direction
    entry_price: Decimal
    stop_price: Decimal
    leverage: int
    exchange_max_leverage: int
    symbol_rules: SymbolRules
    market_time: datetime
    target_price: Decimal | None = None
    exchange_notional_cap: Decimal | None = None

    def __post_init__(self) -> None:
        validate_symbol_name(self.symbol)
        if self.direction not in ("long", "short"):
            raise ValueError("direction must be long or short")
        _require_decimal(self.entry_price, "entry_price")
        _require_decimal(self.stop_price, "stop_price")
        if self.target_price is not None:
            _require_decimal(self.target_price, "target_price")
        if type(self.leverage) is not int or self.leverage < 1:
            raise ValueError("leverage must be a positive integer")
        if type(self.exchange_max_leverage) is not int or self.exchange_max_leverage < 1:
            raise ValueError("exchange_max_leverage must be a positive integer")
        if self.exchange_notional_cap is not None:
            _require_decimal(self.exchange_notional_cap, "exchange_notional_cap")
            if self.exchange_notional_cap <= _ZERO:
                raise ValueError("exchange_notional_cap must be positive")
        if not isinstance(self.symbol_rules, SymbolRules):
            raise ValueError("symbol_rules must be exchange symbol rules")
        if self.symbol_rules.symbol != self.symbol:
            raise ValueError("symbol rules do not match request symbol")
        _require_aware(self.market_time, "market_time")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    accepted: bool
    reasons: tuple[str, ...]
    risk_budget: Decimal
    stop_distance: Decimal
    quantity: Decimal | None = None
    notional: Decimal | None = None
    margin_required: Decimal | None = None
    estimated_stop_loss: Decimal | None = None


def size_position(
    request: RiskRequest,
    account: AccountState,
    portfolio: PortfolioState,
    policy: RiskPolicy,
    *,
    now: datetime | None = None,
) -> RiskDecision:
    """Evaluate an entry and size it without allowing leverage to raise risk."""
    current_time = now or datetime.now(UTC)
    _require_aware(current_time, "now")
    current_time = current_time.astimezone(UTC)
    risk_basis = (
        account.available_balance if policy.basis == "available_balance" else account.equity
    )
    risk_budget = risk_basis * policy.risk_fraction
    stop_distance = abs(request.entry_price - request.stop_price)
    reasons: list[str] = []

    if (
        request.symbol_rules.quote_asset != account.quote_asset
        or request.symbol_rules.margin_asset != account.quote_asset
    ):
        reasons.append("exchange quote and margin assets must match the account denomination")
    if account.captured_at > current_time:
        reasons.append("account snapshot timestamp is in the future")
    elif current_time - account.captured_at.astimezone(UTC) > policy.maximum_account_age:
        reasons.append("account snapshot is stale")
    if request.market_time > current_time:
        reasons.append("market data timestamp is in the future")
    elif current_time - request.market_time.astimezone(UTC) > policy.maximum_market_age:
        reasons.append("market data is stale")
    if not portfolio.connectivity_healthy:
        reasons.append("exchange connectivity is unhealthy")
    if not portfolio.protective_orders_healthy:
        reasons.append("protective-order state is unhealthy")
    if portfolio.open_position_count >= policy.maximum_open_positions:
        reasons.append("maximum open positions reached")
    if portfolio.aggregate_open_risk >= policy.maximum_aggregate_risk:
        reasons.append("maximum aggregate risk reached")
    if portfolio.daily_loss >= policy.maximum_daily_loss:
        reasons.append("maximum daily loss reached")
    if portfolio.drawdown_fraction >= policy.maximum_drawdown_fraction:
        reasons.append("maximum drawdown reached")
    if request.leverage > request.exchange_max_leverage:
        reasons.append("requested leverage exceeds the exchange limit")

    _validate_symbol_rules(request.symbol_rules)
    rules = request.symbol_rules
    for name, price in (("entry", request.entry_price), ("stop", request.stop_price)):
        if price <= _ZERO or not rules.min_price <= price <= rules.max_price:
            reasons.append(f"{name} price is outside exchange limits")
        elif price % rules.price_tick:
            reasons.append(f"{name} price is not aligned to the exchange tick")
    if request.direction == "long" and request.stop_price >= request.entry_price:
        reasons.append("long stop must be below entry")
    if request.direction == "short" and request.stop_price <= request.entry_price:
        reasons.append("short stop must be above entry")
    if stop_distance <= _ZERO:
        reasons.append("stop distance must be positive")
    elif request.entry_price > _ZERO:
        distance_fraction = stop_distance / request.entry_price
        if distance_fraction > policy.maximum_stop_distance_fraction:
            reasons.append("stop distance exceeds the configured maximum")

    reward_to_risk: Decimal | None = None
    if request.target_price is not None and stop_distance > _ZERO:
        target_is_valid = (
            request.target_price > request.entry_price
            if request.direction == "long"
            else request.target_price < request.entry_price
        )
        if request.target_price <= _ZERO or not target_is_valid:
            reasons.append("target price is invalid for the requested direction")
        elif not rules.min_price <= request.target_price <= rules.max_price:
            reasons.append("target price is outside exchange limits")
        elif request.target_price % rules.price_tick:
            reasons.append("target price is not aligned to the exchange tick")
        else:
            reward = abs(request.target_price - request.entry_price)
            reward_to_risk = reward / stop_distance
            if reward_to_risk < policy.minimum_reward_to_risk:
                reasons.append("target reward-to-risk is below the configured minimum")
    elif request.target_price is None and policy.minimum_reward_to_risk > _ZERO:
        reasons.append("target price is required by the configured reward-to-risk policy")

    if reasons:
        return _denied(reasons, risk_budget, stop_distance)

    available_risk = min(
        risk_budget,
        policy.maximum_aggregate_risk - portfolio.aggregate_open_risk,
        policy.maximum_daily_loss - portfolio.daily_loss,
    )
    exposure_room = policy.maximum_total_exposure - account.open_exposure
    basis_reserve = risk_basis * policy.margin_reserve_fraction
    available_margin = max(_ZERO, account.available_balance - basis_reserve)
    if available_risk <= _ZERO:
        return _denied(("no remaining risk budget",), risk_budget, stop_distance)
    if exposure_room <= _ZERO:
        return _denied(("maximum total exposure reached",), risk_budget, stop_distance)
    if available_margin <= _ZERO:
        return _denied(("no margin available after required reserve",), risk_budget, stop_distance)

    quantity_step, minimum_quantity, maximum_quantity = _quantity_rules(rules)
    notional_cap = min(
        policy.maximum_position_notional,
        exposure_room,
        available_margin * request.leverage,
        maximum_quantity * request.entry_price,
    )
    if request.exchange_notional_cap is not None:
        bracket_room = request.exchange_notional_cap - account.open_exposure
        if bracket_room <= _ZERO:
            return _denied(
                ("Binance leverage bracket has no remaining notional capacity",),
                risk_budget,
                stop_distance,
            )
        notional_cap = min(notional_cap, bracket_room)
    quantity_cap = min(available_risk / stop_distance, notional_cap / request.entry_price)
    quantity = (quantity_cap / quantity_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * quantity_step
    notional = quantity * request.entry_price
    estimated_loss = quantity * stop_distance
    margin_required = notional / request.leverage

    if quantity <= _ZERO or quantity < minimum_quantity:
        return _denied(
            ("risk and account limits produce less than the exchange minimum quantity",),
            risk_budget,
            stop_distance,
        )
    if rules.min_notional is not None and notional < rules.min_notional:
        return _denied(
            ("sized order is below the exchange minimum notional",),
            risk_budget,
            stop_distance,
        )
    if margin_required > available_margin:
        return _denied(
            ("sized order exceeds available margin after reserve",),
            risk_budget,
            stop_distance,
        )
    return RiskDecision(
        accepted=True,
        reasons=(),
        risk_budget=risk_budget,
        stop_distance=stop_distance,
        quantity=quantity,
        notional=notional,
        margin_required=margin_required,
        estimated_stop_loss=estimated_loss,
    )


def _denied(
    reasons: tuple[str, ...] | list[str],
    risk_budget: Decimal,
    stop_distance: Decimal,
) -> RiskDecision:
    return RiskDecision(
        accepted=False,
        reasons=tuple(reasons),
        risk_budget=risk_budget,
        stop_distance=stop_distance,
    )


def _quantity_rules(rules: SymbolRules) -> tuple[Decimal, Decimal, Decimal]:
    step = rules.market_quantity_step
    minimum = rules.market_min_quantity
    maximum = rules.market_max_quantity
    if step is None or step <= _ZERO:
        step = rules.quantity_step
        minimum = rules.min_quantity
        maximum = rules.max_quantity
    elif minimum is None or maximum is None:
        raise ValueError("market quantity filter is incomplete")
    if minimum is None or maximum is None or step <= _ZERO:
        raise ValueError("symbol quantity rules are incomplete")
    return step, minimum, maximum


def _validate_symbol_rules(rules: SymbolRules) -> None:
    if rules.quote_asset not in ("USDC", "USDT") or rules.margin_asset != rules.quote_asset:
        raise ValueError("only same-asset USDC or USDT collateral contracts are supported")
    for name in (
        "price_tick",
        "min_price",
        "max_price",
        "quantity_step",
        "min_quantity",
        "max_quantity",
    ):
        value = getattr(rules, name)
        _require_decimal(value, f"symbol_rules.{name}")
    if (
        rules.price_tick <= _ZERO
        or rules.quantity_step <= _ZERO
        or rules.min_price < _ZERO
        or rules.max_price < rules.min_price
        or rules.min_quantity < _ZERO
        or rules.max_quantity < rules.min_quantity
    ):
        raise ValueError("symbol price or quantity rules are invalid")
    if rules.min_notional is not None:
        _require_decimal(rules.min_notional, "symbol_rules.min_notional")
        if rules.min_notional < _ZERO:
            raise ValueError("symbol minimum notional cannot be negative")
    for name in ("market_quantity_step", "market_min_quantity", "market_max_quantity"):
        value = getattr(rules, name)
        if value is not None:
            _require_decimal(value, f"symbol_rules.{name}")
            if value < _ZERO:
                raise ValueError(f"symbol_rules.{name} cannot be negative")
    if (
        rules.market_min_quantity is not None
        and rules.market_max_quantity is not None
        and rules.market_max_quantity < rules.market_min_quantity
    ):
        raise ValueError("market quantity rules are invalid")


def _require_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
