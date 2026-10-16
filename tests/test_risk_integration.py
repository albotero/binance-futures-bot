from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from futures_bot.exchange import SymbolRules
from futures_bot.persistence.database import create_schema_for_tests, make_engine
from futures_bot.persistence.models import AccountSnapshot, ApplicationConfiguration, AuditEvent
from futures_bot.persistence.repositories import (
    ConfigurationConflict,
    save_application_configuration,
)
from futures_bot.risk import AccountState, PortfolioState, RiskPolicy, RiskRequest, size_position
from futures_bot.risk.accounts import (
    MissingAccountSnapshot,
    load_latest_account_state,
    save_account_state,
)
from futures_bot.risk.configuration import (
    MissingRiskConfiguration,
    RiskConfiguration,
    SymbolConfiguration,
    load_risk_configuration,
    save_risk_configuration,
)
from futures_bot.risk.service import evaluate_configured_entry

NOW = datetime(2026, 10, 16, 1, tzinfo=UTC)


@pytest.fixture
def session(tmp_path) -> Iterator[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'risk.sqlite3'}")
    create_schema_for_tests(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


@pytest.fixture
def settings() -> RiskConfiguration:
    return RiskConfiguration(
        quote_asset="USDT",
        policy=RiskPolicy(
            basis="equity",
            risk_fraction=Decimal("0.01"),
            margin_reserve_fraction=Decimal("0.1"),
            maximum_position_notional=Decimal("10000"),
            maximum_total_exposure=Decimal("20000"),
            maximum_aggregate_risk=Decimal("500"),
            maximum_daily_loss=Decimal("300"),
            maximum_drawdown_fraction=Decimal("0.2"),
            maximum_stop_distance_fraction=Decimal("0.1"),
            minimum_reward_to_risk=Decimal("2"),
            maximum_open_positions=3,
        ),
        symbols=(SymbolConfiguration(symbol="BTCUSDT"),),
    )


@pytest.fixture
def account_state() -> AccountState:
    return AccountState(
        wallet_balance=Decimal("10000.25"),
        available_balance=Decimal("9000.25"),
        equity=Decimal("10100.25"),
        unrealized_pnl=Decimal("100"),
        realized_pnl=Decimal("-2.5"),
        used_initial_margin=Decimal("1000"),
        open_exposure=Decimal("3000"),
        captured_at=NOW,
        quote_asset="USDT",
    )


@pytest.fixture
def entry() -> RiskRequest:
    return RiskRequest(
        symbol="BTCUSDT",
        direction="long",
        entry_price=Decimal("100"),
        stop_price=Decimal("98"),
        target_price=Decimal("104"),
        leverage=20,
        exchange_max_leverage=10,
        symbol_rules=SymbolRules(
            symbol="BTCUSDT",
            quote_asset="USDT",
            margin_asset="USDT",
            price_tick=Decimal("0.1"),
            min_price=Decimal("0.1"),
            max_price=Decimal("1000000"),
            quantity_step=Decimal("0.1"),
            min_quantity=Decimal("0"),
            max_quantity=Decimal("1000"),
            market_quantity_step=None,
            market_min_quantity=None,
            market_max_quantity=None,
            min_notional=None,
        ),
        market_time=NOW,
    )


@pytest.fixture
def portfolio() -> PortfolioState:
    return PortfolioState(
        open_position_count=0,
        aggregate_open_risk=Decimal("0"),
        daily_loss=Decimal("0"),
        drawdown_fraction=Decimal("0"),
        protective_orders_healthy=True,
        connectivity_healthy=True,
    )


def test_risk_configuration_round_trips_exact_values_and_schema(
    settings: RiskConfiguration,
) -> None:
    restored = RiskConfiguration.model_validate_json(settings.model_dump_json())

    assert restored == settings
    assert restored.symbols[0].leverage == 1
    assert restored.policy.risk_fraction == Decimal("0.01")
    assert restored.policy.maximum_account_age == timedelta(seconds=15)
    assert settings.model_dump(mode="json")["policy"]["risk_fraction"] == "0.01"
    assert "policy" in RiskConfiguration.model_json_schema()["properties"]


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("risk_fraction", 0.01),
        ("risk_fraction", "NaN"),
        ("risk_fraction", "-0.01"),
        ("maximum_leverage", True),
        ("maximum_leverage", "5"),
        ("maximum_leverage", 21),
        ("maximum_account_age", True),
        ("maximum_market_age", "PT0S"),
        ("unknown", "value"),
    ],
)
def test_risk_policy_payload_rejects_ambiguous_or_invalid_values(
    settings: RiskConfiguration, field: str, invalid: object
) -> None:
    payload = settings.model_dump(mode="json")
    payload["policy"][field] = invalid
    with pytest.raises(ValidationError):
        RiskConfiguration.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "btcusdt"},
        {"symbol": "BTC USDT"},
        {"symbol": "BTCUSDT", "leverage": 0},
        {"symbol": "BTCUSDT", "leverage": 21},
        {"symbol": "BTCUSDT", "leverage": True},
        {"symbol": "BTCUSDT", "leverage": "5"},
        {"symbol": "BTCUSDT", "enabled": "false"},
        {"symbol": "BTCUSDT", "api_key": "not-allowed"},
    ],
)
def test_symbol_settings_are_strict(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SymbolConfiguration.model_validate(payload)


def test_duplicate_symbols_and_leverage_above_policy_cap_are_rejected(
    settings: RiskConfiguration,
) -> None:
    with pytest.raises(ValidationError, match="unique"):
        RiskConfiguration(quote_asset="USDT", policy=settings.policy, symbols=settings.symbols * 2)
    with pytest.raises(ValidationError, match="policy maximum"):
        RiskConfiguration(
            quote_asset="USDT",
            policy=replace(settings.policy, maximum_leverage=2),
            symbols=(SymbolConfiguration(symbol="BTCUSDT", leverage=3),),
        )


def test_risk_settings_preserve_other_runtime_fields_and_audit_changes(
    session: Session, settings: RiskConfiguration
) -> None:
    save_application_configuration(
        session, {"mode": "paper", "strategy": {"version": "test"}}, "operator", 0
    )
    assert save_risk_configuration(session, settings, actor="operator", expected_version=1) == 2
    session.commit()
    loaded = load_risk_configuration(session)
    assert loaded.version == 2
    assert loaded.configuration == settings
    runtime = session.get(ApplicationConfiguration, "runtime")
    assert runtime is not None
    assert runtime.configuration["strategy"] == {"version": "test"}
    assert runtime.configuration["mode"] == "paper"
    audits = session.scalars(select(AuditEvent).order_by(AuditEvent.created_at)).all()
    assert len(audits) == 2
    assert audits[-1].details["version"] == 2
    with pytest.raises(ConfigurationConflict):
        save_risk_configuration(session, settings, actor="stale-operator", expected_version=1)
    assert session.query(AuditEvent).count() == 2


def test_configuration_and_audit_roll_back_together(
    session: Session, settings: RiskConfiguration
) -> None:
    save_risk_configuration(session, settings, actor="operator", expected_version=0)
    session.rollback()
    with pytest.raises(MissingRiskConfiguration):
        load_risk_configuration(session)
    assert session.query(AuditEvent).count() == 0


def test_stale_writer_cannot_overwrite_a_new_configuration(
    session: Session, settings: RiskConfiguration
) -> None:
    save_risk_configuration(session, settings, actor="operator", expected_version=0)
    session.commit()
    stale = load_risk_configuration(session)
    with Session(session.get_bind()) as writer:
        save_application_configuration(
            writer,
            {"risk": settings.model_dump(mode="json"), "strategy": {"version": "new"}},
            "other-operator",
            expected_version=1,
        )
        writer.commit()
    with pytest.raises(ConfigurationConflict):
        save_risk_configuration(
            session, settings, actor="stale-operator", expected_version=stale.version
        )
    session.rollback()
    assert load_risk_configuration(session).version == 2
    stored = session.get(ApplicationConfiguration, "runtime")
    assert stored is not None
    assert stored.configuration["strategy"] == {"version": "new"}
    assert session.query(AuditEvent).count() == 2


def test_missing_and_invalid_stored_configuration_fail_explicitly(
    session: Session,
) -> None:
    with pytest.raises(MissingRiskConfiguration):
        load_risk_configuration(session)
    save_application_configuration(session, {"risk": {}}, "operator", 0)
    with pytest.raises(ValidationError):
        load_risk_configuration(session)


def test_account_state_round_trip_and_environment_isolation(
    session: Session, account_state: AccountState
) -> None:
    first = save_account_state(session, account_state, environment="simulated")
    save_account_state(
        session,
        replace(account_state, captured_at=NOW + timedelta(seconds=1)),
        environment="production",
    )
    session.commit()
    saved = load_latest_account_state(session, environment="simulated", quote_asset="USDT")
    assert saved.snapshot_id == first
    assert saved.state == account_state
    assert saved.environment == "simulated"
    with pytest.raises(MissingAccountSnapshot, match="testnet"):
        load_latest_account_state(session, environment="testnet", quote_asset="USDT")


def test_account_selection_uses_capture_time_not_insertion_order(
    session: Session, account_state: AccountState
) -> None:
    newest = save_account_state(session, account_state, environment="simulated")
    save_account_state(
        session,
        replace(account_state, captured_at=NOW - timedelta(seconds=30)),
        environment="simulated",
    )
    session.commit()
    assert (
        load_latest_account_state(session, environment="simulated", quote_asset="USDT").snapshot_id
        == newest
    )


def test_account_transaction_rollback_leaves_no_state(
    session: Session, account_state: AccountState
) -> None:
    save_account_state(session, account_state, environment="simulated")
    session.rollback()
    with pytest.raises(MissingAccountSnapshot):
        load_latest_account_state(session, environment="simulated")


def test_configured_evaluation_uses_persisted_leverage_and_tracks_provenance(
    session: Session,
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    configured = RiskConfiguration(
        quote_asset="USDT",
        policy=settings.policy,
        symbols=(SymbolConfiguration(symbol="BTCUSDT", leverage=5),),
    )
    save_risk_configuration(session, configured, actor="operator", expected_version=0)
    snapshot = save_account_state(session, account_state, environment="simulated")
    session.commit()
    result = evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    assert result.configuration_version == 1
    assert result.account_snapshot_id == snapshot
    assert result.leverage == 5
    assert entry.leverage == 20
    assert result.decision.accepted
    assert result.decision.notional is not None
    assert result.decision.estimated_stop_loss is not None
    assert result.decision.margin_required == result.decision.notional / Decimal(5)
    assert result.decision.estimated_stop_loss <= result.decision.risk_budget


def test_saved_risk_changes_affect_subsequent_decisions(
    session: Session,
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    save_risk_configuration(session, settings, actor="operator", expected_version=0)
    save_account_state(session, account_state, environment="simulated")
    session.commit()
    first = evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    updated = RiskConfiguration(
        quote_asset="USDT",
        policy=replace(settings.policy, risk_fraction=Decimal("0.005")),
        symbols=(SymbolConfiguration(symbol="BTCUSDT", leverage=3),),
    )
    save_risk_configuration(session, updated, actor="operator", expected_version=1)
    session.commit()
    second = evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    assert first.decision.accepted and second.decision.accepted
    assert second.configuration_version == 2
    assert second.account_snapshot_id == first.account_snapshot_id
    assert second.leverage == 3
    assert second.decision.risk_budget == first.decision.risk_budget / 2
    assert second.decision.quantity == Decimal("25.2")


def test_configured_leverage_is_still_rejected_above_exchange_limit(
    session: Session,
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    configured = RiskConfiguration(
        quote_asset="USDT",
        policy=settings.policy,
        symbols=(SymbolConfiguration(symbol="BTCUSDT", leverage=20),),
    )
    save_risk_configuration(session, configured, actor="operator", expected_version=0)
    save_account_state(session, account_state, environment="simulated")
    result = evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    assert not result.decision.accepted
    assert "requested leverage exceeds a platform or exchange limit" in result.decision.reasons


def test_configured_evaluation_rejects_disabled_symbol_and_stale_accounts(
    session: Session,
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    disabled = RiskConfiguration(
        quote_asset="USDT",
        policy=settings.policy,
        symbols=(SymbolConfiguration(symbol="BTCUSDT", enabled=False),),
    )
    save_risk_configuration(session, disabled, actor="operator", expected_version=0)
    with pytest.raises(ValueError, match="not enabled"):
        evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    save_risk_configuration(session, settings, actor="operator", expected_version=1)
    with pytest.raises(MissingAccountSnapshot):
        evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    save_account_state(
        session,
        replace(account_state, captured_at=NOW - timedelta(seconds=16)),
        environment="simulated",
    )
    result = evaluate_configured_entry(session, entry, portfolio, environment="simulated", now=NOW)
    assert not result.decision.accepted
    assert "account snapshot is stale" in result.decision.reasons


def test_invalid_persisted_account_is_not_accepted(
    session: Session, account_state: AccountState
) -> None:
    snapshot = save_account_state(session, account_state, environment="simulated")
    row = session.get(AccountSnapshot, snapshot)
    assert row is not None
    row.available_balance = Decimal("-1")
    session.commit()
    with pytest.raises(ValueError, match="cannot be negative"):
        load_latest_account_state(session, environment="simulated", quote_asset="USDT")


def test_tiny_equity_never_produces_an_accepted_zero_quantity(
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    result = size_position(
        replace(entry, leverage=1),
        replace(account_state, equity=Decimal("0.01")),
        portfolio,
        settings.policy,
        now=NOW,
    )
    assert not result.accepted
    assert result.quantity is None


def test_usdc_is_default_quote_and_usdt_remains_an_explicit_option(
    settings: RiskConfiguration,
) -> None:
    default = RiskConfiguration(
        policy=settings.policy, symbols=(SymbolConfiguration(symbol="BTCUSDC"),)
    )
    assert default.quote_asset == "USDC"
    assert default.symbols[0].leverage == 1
    assert RiskConfiguration.model_validate_json(default.model_dump_json()) == default
    assert settings.quote_asset == "USDT"
    with pytest.raises(ValidationError, match="selected quote"):
        RiskConfiguration(policy=settings.policy, symbols=(SymbolConfiguration(symbol="BTCUSDT"),))
    with pytest.raises(ValidationError):
        RiskConfiguration.model_validate({**default.model_dump(mode="json"), "quote_asset": "BTC"})


def test_account_queries_isolate_usdc_and_usdt_balances(
    session: Session, account_state: AccountState
) -> None:
    usdc = replace(account_state, quote_asset="USDC", available_balance=Decimal("200"))
    usdc_id = save_account_state(session, usdc, environment="simulated")
    usdt_id = save_account_state(
        session,
        replace(account_state, captured_at=NOW + timedelta(seconds=1)),
        environment="simulated",
    )
    session.commit()
    assert load_latest_account_state(session, environment="simulated").snapshot_id == usdc_id
    assert load_latest_account_state(session, environment="simulated").state == usdc
    assert (
        load_latest_account_state(session, environment="simulated", quote_asset="USDT").snapshot_id
        == usdt_id
    )


def test_usdc_entry_uses_only_usdc_account_and_exchange_metadata(
    session: Session,
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    configured = RiskConfiguration(
        policy=settings.policy, symbols=(SymbolConfiguration(symbol="BTCUSDC"),)
    )
    save_risk_configuration(session, configured, actor="op", expected_version=0)
    save_account_state(session, account_state, environment="simulated")
    usdc_entry = replace(
        entry,
        symbol="BTCUSDC",
        symbol_rules=replace(
            entry.symbol_rules, symbol="BTCUSDC", quote_asset="USDC", margin_asset="USDC"
        ),
    )
    with pytest.raises(MissingAccountSnapshot, match="USDC"):
        evaluate_configured_entry(session, usdc_entry, portfolio, environment="simulated", now=NOW)
    save_account_state(session, replace(account_state, quote_asset="USDC"), environment="simulated")
    decision = evaluate_configured_entry(
        session, usdc_entry, portfolio, environment="simulated", now=NOW
    )
    assert decision.decision.accepted
    assert decision.leverage == 1
    with pytest.raises(ValueError, match="metadata"):
        evaluate_configured_entry(
            session,
            replace(
                usdc_entry,
                symbol_rules=replace(usdc_entry.symbol_rules, margin_asset="USDT"),
            ),
            portfolio,
            environment="simulated",
            now=NOW,
        )


def test_sizing_cannot_use_the_other_quote_balance(
    settings: RiskConfiguration,
    account_state: AccountState,
    entry: RiskRequest,
    portfolio: PortfolioState,
) -> None:
    result = size_position(
        replace(entry, leverage=1),
        replace(account_state, quote_asset="USDC"),
        portfolio,
        settings.policy,
        now=NOW,
    )
    assert not result.accepted
    assert any("denomination" in reason for reason in result.reasons)


def test_unknown_legacy_account_denomination_is_never_assumed(
    session: Session, account_state: AccountState
) -> None:
    snapshot = save_account_state(session, account_state, environment="simulated")
    row = session.get(AccountSnapshot, snapshot)
    assert row is not None
    row.quote_asset = "UNKNOWN"
    session.commit()
    for quote in ("USDC", "USDT"):
        with pytest.raises(MissingAccountSnapshot):
            load_latest_account_state(session, environment="simulated", quote_asset=quote)
