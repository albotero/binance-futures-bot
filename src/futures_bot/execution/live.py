"""Fail-closed live order saga for an explicitly supplied, reconciled gateway.

No signed Binance transport or application route is provided by this module.
The deployed application's modes remain paper/backtest only.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from futures_bot.persistence.models import AuditEvent, OrderRecord
from futures_bot.risk import PortfolioState, RiskRequest
from futures_bot.risk.service import evaluate_configured_entry


class LiveExecutionDisabled(RuntimeError):
    pass


class AmbiguousSubmission(RuntimeError):
    """A transport failure after submission may still represent an accepted order."""


class ExchangeRejected(RuntimeError):
    """The gateway definitively rejected an order; no fill occurred."""


@dataclass(frozen=True, slots=True)
class LiveAuthorization:
    environment: Literal["testnet", "production"]
    expires_at: datetime
    reconciled_at: datetime
    enabled: bool = False

    def verify(self, now: datetime) -> None:
        for value in (self.expires_at, self.reconciled_at, now):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("live authorization timestamps must be timezone-aware")
        if not self.enabled or not self.reconciled_at <= now < self.expires_at:
            raise LiveExecutionDisabled("live execution is disabled or authorization expired")
        if (now - self.reconciled_at).total_seconds() > 15:
            raise LiveExecutionDisabled("live account reconciliation is stale")


@dataclass(frozen=True, slots=True)
class EntryFill:
    exchange_order_id: str
    quantity: Decimal
    price: Decimal


class ReconciledGateway(Protocol):
    environment: Literal["testnet", "production"]

    async def set_leverage(self, *, symbol: str, leverage: int) -> int: ...

    async def enter(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_order_id: str,
    ) -> EntryFill: ...

    async def protect(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        client_order_id: str,
        reduce_only: bool,
    ) -> None:
        """Return only after confirming BOTH exchange-native protections exist."""
        ...

    async def flatten(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        client_order_id: str,
        reduce_only: bool,
    ) -> None:
        """Return only after reconciling the resulting zero position."""
        ...


def prepare_entry(
    session: Session,
    request: RiskRequest,
    portfolio: PortfolioState,
    authorization: LiveAuthorization,
    *,
    client_order_id: str,
    actor: str,
    now: datetime,
) -> OrderRecord:
    authorization.verify(now)
    if not actor.strip():
        raise ValueError("live intent actor must not be empty")
    if (
        not client_order_id.isascii()
        or not 2 <= len(client_order_id) <= 32
        or not all(character.isalnum() or character in "-_" for character in client_order_id)
    ):
        raise ValueError("live client order identity must be 2-32 safe ASCII characters")
    existing = session.scalar(
        select(OrderRecord).where(OrderRecord.client_order_id == client_order_id)
    )
    if existing is not None:
        raise ValueError("live client order identity has already been used; reconcile it")
    unresolved = session.scalar(
        select(OrderRecord.id).where(
            OrderRecord.environment == authorization.environment,
            OrderRecord.status.in_(("prepared", "submitting", "uncertain", "unprotected")),
        )
    )
    if unresolved is not None:
        raise ValueError("unresolved live order blocks new entries")
    approved = evaluate_configured_entry(
        session, request, portfolio, environment=authorization.environment, now=now
    )
    if not approved.decision.accepted:
        raise ValueError("live risk check rejected: " + "; ".join(approved.decision.reasons))
    quantity = approved.decision.quantity
    assert quantity is not None
    row = OrderRecord(
        environment=authorization.environment,
        symbol=request.symbol,
        client_order_id=client_order_id,
        side="buy" if request.direction == "long" else "sell",
        order_type="market",
        status="prepared",
        quantity=quantity,
    )
    session.add(row)
    session.flush()
    session.add(
        AuditEvent(
            actor=actor,
            action="live.intent_prepared",
            resource_type="order",
            resource_id=row.id,
            details={
                "configuration_version": approved.configuration_version,
                "account_snapshot_id": approved.account_snapshot_id,
                "leverage": approved.leverage,
                "stop_price": str(request.stop_price),
                "target_price": str(request.target_price),
                "authorization_expires_at": authorization.expires_at.isoformat(),
            },
        )
    )
    session.flush()
    return row


async def submit_prepared_entry(
    factory: sessionmaker[Session],
    order_id: str,
    gateway: ReconciledGateway,
    authorization: LiveAuthorization,
    *,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(UTC)
    authorization.verify(current)
    with factory() as session:
        row = session.get(OrderRecord, order_id)
        if row is None or row.status != "prepared":
            raise ValueError(
                "order is not a committed prepared intent; reconcile instead of retrying"
            )
        if row.environment != authorization.environment or gateway.environment != row.environment:
            raise LiveExecutionDisabled("live gateway environment mismatch")
        if current - row.created_at > timedelta(seconds=15):
            raise LiveExecutionDisabled("live intent expired; re-evaluate account and market risk")
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == row.id,
                AuditEvent.action == "live.intent_prepared",
            )
        )
        if audit is None:
            raise ValueError("live intent lacks risk provenance")
        details = audit.details
        stop = Decimal(details["stop_price"])
        target = Decimal(details["target_price"])
        result = session.execute(
            update(OrderRecord)
            .where(
                OrderRecord.id == order_id,
                OrderRecord.status == "prepared",
            )
            .values(status="submitting")
            .returning(OrderRecord.id)
        )
        if result.scalar_one_or_none() is None:
            raise ValueError("live submission raced another worker")
        symbol, side, quantity, identity = row.symbol, row.side, row.quantity, row.client_order_id
        session.commit()
    try:
        confirmed_leverage = await gateway.set_leverage(
            symbol=symbol,
            leverage=details["leverage"],
        )
        if confirmed_leverage != details["leverage"]:
            raise AmbiguousSubmission("exchange leverage differs from saved risk approval")
        fill = await gateway.enter(
            symbol=symbol,
            side=side,
            quantity=quantity,
            client_order_id=identity,
        )
    except ExchangeRejected:
        _status(factory, order_id, "rejected")
        raise
    except (AmbiguousSubmission, TimeoutError, ConnectionError):
        _status(factory, order_id, "uncertain")
        raise
    if (
        not fill.exchange_order_id
        or not fill.quantity.is_finite()
        or not fill.price.is_finite()
        or not 0 < fill.quantity <= quantity
        or fill.price <= 0
    ):
        _status(factory, order_id, "uncertain")
        raise AmbiguousSubmission(
            "gateway returned an invalid fill; exchange reconciliation required"
        )
    with factory() as session:
        row = session.get(OrderRecord, order_id)
        assert row is not None
        row.status = "unprotected"
        row.exchange_order_id = fill.exchange_order_id
        row.average_fill_price = fill.price
        session.commit()
    exit_side = "sell" if side == "buy" else "buy"
    try:
        await gateway.protect(
            symbol=symbol,
            side=exit_side,
            quantity=fill.quantity,
            stop_price=stop,
            target_price=target,
            client_order_id=f"{identity}-p",
            reduce_only=True,
        )
    except (ExchangeRejected, AmbiguousSubmission, TimeoutError, ConnectionError):
        # This is a separate idempotent reduce-only action, never an entry retry.
        try:
            await gateway.flatten(
                symbol=symbol,
                side=exit_side,
                quantity=fill.quantity,
                client_order_id=f"{identity}-x",
                reduce_only=True,
            )
        except (ExchangeRejected, AmbiguousSubmission, TimeoutError, ConnectionError):
            _status(factory, order_id, "uncertain")
            raise
        _status(factory, order_id, "emergency_closed")
        raise
    _status(factory, order_id, "protected")


def _status(factory: sessionmaker[Session], order_id: str, status: str) -> None:
    with factory() as session:
        row = session.get(OrderRecord, order_id)
        if row is None:
            raise ValueError("live order disappeared during reconciliation")
        row.status = status
        session.add(
            AuditEvent(
                actor="live-coordinator",
                action=f"live.{status}",
                resource_type="order",
                resource_id=order_id,
                details={"requires_reconciliation": status == "uncertain"},
            )
        )
        session.commit()
