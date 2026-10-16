from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from futures_bot.exchange.binance_usdm import SymbolRules
from futures_bot.risk import (
    AccountState,
    PortfolioState,
    RiskPolicy,
    RiskRequest,
    size_position,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def policy(**overrides: object) -> RiskPolicy:
    values: dict[str, object] = {
        "basis": "equity",
        "risk_fraction": Decimal("0.01"),
        "margin_reserve_fraction": Decimal("0.10"),
        "maximum_position_notional": Decimal("10000"),
        "maximum_total_exposure": Decimal("20000"),
        "maximum_aggregate_risk": Decimal("500"),
        "maximum_daily_loss": Decimal("300"),
        "maximum_drawdown_fraction": Decimal("0.20"),
        "maximum_stop_distance_fraction": Decimal("0.10"),
        "minimum_reward_to_risk": Decimal("1.5"),
        "maximum_open_positions": 3,
    }
    values.update(overrides)
    return RiskPolicy(**values)  # type: ignore[arg-type]


def account(**overrides: object) -> AccountState:
    values: dict[str, object] = {
        "wallet_balance": Decimal("10000"),
        "available_balance": Decimal("9000"),
        "equity": Decimal("10000"),
        "unrealized_pnl": Decimal("0"),
        "realized_pnl": Decimal("0"),
        "used_initial_margin": Decimal("1000"),
        "open_exposure": Decimal("0"),
        "captured_at": NOW,
        "quote_asset": "USDT",
    }
    values.update(overrides)
    return AccountState(**values)  # type: ignore[arg-type]


def portfolio(**overrides: object) -> PortfolioState:
    values: dict[str, object] = {
        "open_position_count": 0,
        "aggregate_open_risk": Decimal("0"),
        "daily_loss": Decimal("0"),
        "drawdown_fraction": Decimal("0"),
        "protective_orders_healthy": True,
        "connectivity_healthy": True,
    }
    values.update(overrides)
    return PortfolioState(**values)  # type: ignore[arg-type]


def request(**overrides: object) -> RiskRequest:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "entry_price": Decimal("100"),
        "stop_price": Decimal("98"),
        "leverage": 1,
        "exchange_max_leverage": 20,
        "symbol_rules": SymbolRules(
            symbol="BTCUSDT",
            quote_asset="USDT",
            margin_asset="USDT",
            price_tick=Decimal("0.1"),
            min_price=Decimal("0.1"),
            max_price=Decimal("1000000"),
            quantity_step=Decimal("0.1"),
            min_quantity=Decimal("0.1"),
            max_quantity=Decimal("1000"),
            market_quantity_step=Decimal("0.1"),
            market_min_quantity=Decimal("0.1"),
            market_max_quantity=Decimal("500"),
            min_notional=Decimal("5"),
        ),
        "market_time": NOW,
        "target_price": Decimal("104"),
    }
    values.update(overrides)
    return RiskRequest(**values)  # type: ignore[arg-type]


def test_size_is_decimal_step_aligned_and_leverage_does_not_raise_risk() -> None:
    one_x = size_position(request(), account(), portfolio(), policy(), now=NOW)
    five_x = size_position(
        request(leverage=5),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )

    assert one_x.accepted and five_x.accepted
    assert one_x.quantity == Decimal("50.0")
    assert five_x.quantity == one_x.quantity
    assert one_x.estimated_stop_loss == Decimal("100.0")
    assert five_x.estimated_stop_loss == one_x.estimated_stop_loss
    assert one_x.margin_required == Decimal("5000")
    assert five_x.margin_required == Decimal("1000")


def test_50x_requires_exchange_permission_but_not_a_portfolio_cap():
    baseline = size_position(request(), account(), portfolio(), policy(), now=NOW)
    high = size_position(
        request(leverage=50, exchange_max_leverage=50),
        account(),
        portfolio(),
        policy(maximum_leverage=50),
        now=NOW,
    )
    assert high.accepted and baseline.accepted
    assert high.quantity == baseline.quantity
    assert high.estimated_stop_loss == baseline.estimated_stop_loss
    assert high.margin_required == baseline.margin_required / 50
    assert not size_position(
        request(leverage=50, exchange_max_leverage=25),
        account(),
        portfolio(),
        policy(maximum_leverage=50),
        now=NOW,
    ).accepted
    assert size_position(
        request(leverage=50, exchange_max_leverage=50),
        account(),
        portfolio(),
        policy(maximum_leverage=5),
        now=NOW,
    ).accepted
    with pytest.raises(ValueError, match="positive integer"):
        policy(maximum_leverage=0)


def test_exchange_notional_bracket_caps_size_and_accounts_for_existing_exposure():
    result = size_position(
        request(leverage=125, exchange_max_leverage=125, exchange_notional_cap=Decimal("1000")),
        account(open_exposure=Decimal("400")),
        portfolio(),
        policy(maximum_leverage=5),
        now=NOW,
    )
    assert result.accepted
    assert result.notional == Decimal("600")
    assert result.quantity == Decimal("6")
    assert result.estimated_stop_loss <= result.risk_budget
    assert result.margin_required == result.notional / 125
    denied = size_position(
        request(leverage=125, exchange_max_leverage=125, exchange_notional_cap=Decimal("1000")),
        account(open_exposure=Decimal("1000")),
        portfolio(),
        policy(),
        now=NOW,
    )
    assert not denied.accepted and "notional capacity" in denied.reasons[0]


@pytest.mark.parametrize("cap", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_exchange_notional_cap_must_be_positive_and_finite(cap):
    with pytest.raises(ValueError):
        request(exchange_notional_cap=cap)


def test_short_position_uses_mirrored_stop_and_target_distances() -> None:
    result = size_position(
        request(
            direction="short",
            stop_price=Decimal("102"),
            target_price=Decimal("96"),
        ),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )

    assert result.accepted
    assert result.quantity == Decimal("50.0")
    assert result.estimated_stop_loss == Decimal("100.0")


def test_equity_or_available_balance_basis_is_applied_explicitly() -> None:
    result = size_position(
        request(),
        account(available_balance=Decimal("8000")),
        portfolio(),
        policy(basis="available_balance"),
        now=NOW,
    )

    assert result.accepted
    assert result.risk_budget == Decimal("80.00")
    assert result.estimated_stop_loss == Decimal("80.0")


def test_quantity_and_notional_are_capped_and_rounded_down() -> None:
    result = size_position(
        request(),
        account(),
        portfolio(),
        policy(maximum_position_notional=Decimal("1200")),
        now=NOW,
    )

    assert result.accepted
    assert result.quantity == Decimal("12.0")
    assert result.notional == Decimal("1200.0")


def test_remaining_aggregate_risk_caps_new_position_size() -> None:
    result = size_position(
        request(),
        account(),
        portfolio(aggregate_open_risk=Decimal("495")),
        policy(),
        now=NOW,
    )

    assert result.accepted
    assert result.risk_budget == Decimal("100.00")
    assert result.quantity == Decimal("2.5")
    assert result.estimated_stop_loss == Decimal("5.0")


@pytest.mark.parametrize(
    ("portfolio_state", "expected_reason"),
    [
        (portfolio(open_position_count=3), "maximum open positions"),
        (portfolio(aggregate_open_risk=Decimal("500")), "aggregate risk"),
        (portfolio(daily_loss=Decimal("300")), "daily loss"),
        (portfolio(drawdown_fraction=Decimal("0.20")), "drawdown"),
        (portfolio(protective_orders_healthy=False), "protective-order"),
        (portfolio(connectivity_healthy=False), "connectivity"),
    ],
)
def test_portfolio_guards_fail_closed(
    portfolio_state: PortfolioState,
    expected_reason: str,
) -> None:
    result = size_position(request(), account(), portfolio_state, policy(), now=NOW)

    assert not result.accepted
    assert result.quantity is None
    assert any(expected_reason in reason for reason in result.reasons)


def test_stale_or_future_account_and_market_data_are_rejected() -> None:
    stale = size_position(
        request(market_time=NOW - timedelta(seconds=16)),
        account(captured_at=NOW - timedelta(seconds=16)),
        portfolio(),
        policy(),
        now=NOW,
    )
    future = size_position(
        request(market_time=NOW + timedelta(seconds=1)),
        account(captured_at=NOW + timedelta(seconds=1)),
        portfolio(),
        policy(),
        now=NOW,
    )

    assert not stale.accepted
    assert "account snapshot is stale" in stale.reasons
    assert "market data is stale" in stale.reasons
    assert not future.accepted
    assert "account snapshot timestamp is in the future" in future.reasons
    assert "market data timestamp is in the future" in future.reasons


def test_exchange_minimum_quantity_and_notional_reject_undersized_orders() -> None:
    tiny_risk = size_position(
        request(),
        account(equity=Decimal("5")),
        portfolio(),
        policy(),
        now=NOW,
    )
    min_notional_request = request(
        symbol_rules=SymbolRules(
            symbol="BTCUSDT",
            quote_asset="USDT",
            margin_asset="USDT",
            price_tick=Decimal("0.1"),
            min_price=Decimal("0.1"),
            max_price=Decimal("1000000"),
            quantity_step=Decimal("0.1"),
            min_quantity=Decimal("0.1"),
            max_quantity=Decimal("1000"),
            market_quantity_step=Decimal("0.1"),
            market_min_quantity=Decimal("0.1"),
            market_max_quantity=Decimal("500"),
            min_notional=Decimal("20"),
        )
    )
    min_notional = size_position(
        min_notional_request,
        account(equity=Decimal("30")),
        portfolio(),
        policy(),
        now=NOW,
    )

    assert not tiny_risk.accepted
    assert "minimum quantity" in tiny_risk.reasons[0]
    assert not min_notional.accepted
    assert "minimum notional" in min_notional.reasons[0]


@pytest.mark.parametrize(
    ("leverage", "exchange_max"),
    [(31, 30), (10, 5)],
)
def test_leverage_is_capped_by_verified_exchange_limit(
    leverage: int,
    exchange_max: int,
) -> None:
    result = size_position(
        request(leverage=leverage, exchange_max_leverage=exchange_max),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )

    assert not result.accepted
    assert "leverage" in result.reasons[0]


def test_direction_stop_target_and_tick_constraints_are_checked() -> None:
    invalid = size_position(
        request(stop_price=Decimal("101"), target_price=Decimal("99")),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )
    unaligned = size_position(
        request(stop_price=Decimal("98.05")),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )
    poor_reward = size_position(
        request(target_price=Decimal("101")),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )
    invalid_target = size_position(
        request(target_price=Decimal("1000000.1")),
        account(),
        portfolio(),
        policy(),
        now=NOW,
    )

    assert not invalid.accepted
    assert any("long stop" in reason for reason in invalid.reasons)
    assert any("target price is invalid" in reason for reason in invalid.reasons)
    assert not unaligned.accepted
    assert any("tick" in reason for reason in unaligned.reasons)
    assert not poor_reward.accepted
    assert any("target reward-to-risk" in reason for reason in poor_reward.reasons)
    assert not invalid_target.accepted
    assert "target price is outside exchange limits" in invalid_target.reasons


def test_margin_reserve_caps_size_without_changing_risk_budget() -> None:
    result = size_position(
        request(leverage=2),
        account(available_balance=Decimal("1900"), equity=Decimal("10000")),
        portfolio(),
        policy(margin_reserve_fraction=Decimal("0.10")),
        now=NOW,
    )

    assert result.accepted
    assert result.risk_budget == Decimal("100.00")
    assert result.notional == Decimal("1800.0")
    assert result.margin_required == Decimal("900")


def test_policy_and_account_reject_non_decimal_or_ambiguous_values() -> None:
    with pytest.raises(ValueError, match="finite Decimal"):
        policy(risk_fraction=0.01)
    with pytest.raises(ValueError, match="cannot be negative"):
        account(available_balance=Decimal("-1"))
    with pytest.raises(ValueError, match="aware"):
        account(captured_at=datetime(2026, 10, 15))
