import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from futures_bot.execution.live import (
    AmbiguousSubmission,
    EntryFill,
    ExchangeRejected,
    LiveAuthorization,
    LiveExecutionDisabled,
    prepare_entry,
    submit_prepared_entry,
)
from futures_bot.persistence.models import OrderRecord
from futures_bot.risk import AccountState, PortfolioState, RiskRequest
from futures_bot.risk.accounts import save_account_state

pytest_plugins = ["test_paper"]
D = Decimal


@pytest.fixture
def intent(factory, rules):
    now = datetime.now(UTC)
    authorization = LiveAuthorization(
        environment="testnet",
        expires_at=now + timedelta(seconds=10),
        reconciled_at=now,
        enabled=True,
    )
    with factory() as session:
        save_account_state(
            session,
            AccountState(
                wallet_balance=D(10000),
                available_balance=D(10000),
                equity=D(10000),
                unrealized_pnl=D(0),
                realized_pnl=D(0),
                used_initial_margin=D(0),
                open_exposure=D(0),
                captured_at=now,
            ),
            environment="testnet",
        )
        request = RiskRequest(
            symbol="BTCUSDC",
            direction="long",
            entry_price=D(100),
            stop_price=D(98),
            target_price=D(104),
            leverage=1,
            exchange_max_leverage=20,
            symbol_rules=rules,
            market_time=now,
        )
        portfolio = PortfolioState(
            open_position_count=0,
            aggregate_open_risk=D(0),
            daily_loss=D(0),
            drawdown_fraction=D(0),
            protective_orders_healthy=True,
            connectivity_healthy=True,
        )
        row = prepare_entry(
            session,
            request,
            portfolio,
            authorization,
            client_order_id="safe-entry-1",
            actor="test",
            now=now,
        )
        session.commit()
        return row.id, authorization, now


class Gateway:
    environment = "testnet"

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def set_leverage(self, **kwargs):
        self.calls.append(("leverage", kwargs))
        return kwargs["leverage"]

    async def enter(self, **kwargs):
        self.calls.append(("enter", kwargs))
        if self.fail == "timeout":
            raise TimeoutError("ambiguous")
        if self.fail == "rejected":
            raise ExchangeRejected("rejected")
        return EntryFill("exchange-1", kwargs["quantity"], D(100))

    async def protect(self, **kwargs):
        self.calls.append(("protect", kwargs))
        assert kwargs["reduce_only"]
        if self.fail in ("protection", "flatten"):
            raise ExchangeRejected("stop rejected")

    async def flatten(self, **kwargs):
        self.calls.append(("flatten", kwargs))
        assert kwargs["reduce_only"]
        if self.fail == "flatten":
            raise AmbiguousSubmission("flatten unknown")


def test_disabled_expired_and_wrong_environment_never_reach_gateway(factory, intent):
    run_id, authorization, now = intent
    gateway = Gateway()
    for auth in (
        replace(authorization, enabled=False),
        replace(authorization, expires_at=now),
        replace(authorization, reconciled_at=now - timedelta(minutes=1)),
        replace(authorization, environment="production"),
    ):
        with pytest.raises(LiveExecutionDisabled):
            asyncio.run(submit_prepared_entry(factory, run_id, gateway, auth, now=now))
    assert not gateway.calls


@pytest.mark.parametrize(
    "failure,status,error",
    [
        ("timeout", "uncertain", TimeoutError),
        ("rejected", "rejected", ExchangeRejected),
        ("protection", "emergency_closed", ExchangeRejected),
        ("flatten", "uncertain", AmbiguousSubmission),
    ],
)
def test_uncertainty_and_protection_failures_are_durable_not_entry_retries(
    factory, intent, failure, status, error
):
    run_id, authorization, now = intent
    gateway = Gateway(failure)
    with pytest.raises(error):
        asyncio.run(submit_prepared_entry(factory, run_id, gateway, authorization, now=now))
    with factory() as session:
        assert session.get(OrderRecord, run_id).status == status
    with pytest.raises(ValueError, match="reconcile"):
        asyncio.run(submit_prepared_entry(factory, run_id, gateway, authorization, now=now))
    assert sum(name == "enter" for name, _ in gateway.calls) == 1


def test_protections_use_confirmed_fill_and_opposite_reduce_only_side(factory, intent):
    run_id, authorization, now = intent
    gateway = Gateway()
    asyncio.run(submit_prepared_entry(factory, run_id, gateway, authorization, now=now))
    assert [name for name, _ in gateway.calls] == ["leverage", "enter", "protect"]
    assert gateway.calls[-1][1]["side"] == "sell"
    with factory() as session:
        assert session.get(OrderRecord, run_id).status == "protected"
