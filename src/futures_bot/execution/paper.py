"""Restart-safe single-symbol paper execution with transactional bar checkpoints."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import JSON, CheckConstraint, Integer, String, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from futures_bot.backtesting.engine import (
    BacktestConfiguration,
    _require_history,
    _size_entry,
    _slipped_price,
)
from futures_bot.exchange import FundingRate, Kline, SymbolRules
from futures_bot.persistence.models import (
    AuditEvent,
    Base,
    Fill,
    OrderRecord,
    Position,
    Signal,
    Trade,
    UTCDateTime,
    utc_now,
)
from futures_bot.persistence.repositories import ConfigurationConflict
from futures_bot.risk.configuration import RiskConfiguration, load_risk_configuration
from futures_bot.strategy import EntrySetup, StrategyConfiguration, evaluate_pullback
from futures_bot.strategy.configuration import load_strategy_configuration
from futures_bot.strategy.lifecycle import evaluate_entry, evaluate_exit

ZERO = Decimal(0)
PAPER_VERSION = "paper-closed-bars-v1"


class PaperRun(Base):
    __tablename__ = "paper_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','paused','stopping','stopped','faulted')",
            name="ck_paper_run_status",
        ),
        UniqueConstraint("active_slot", name="uq_paper_active_run"),
        CheckConstraint(
            "(status = 'stopped' AND active_slot IS NULL) OR "
            "(status != 'stopped' AND active_slot IS NOT NULL AND active_slot = 1)",
            name="ck_paper_active_slot",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_slot: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class PaperEvent(Base):
    __tablename__ = "paper_events"
    __table_args__ = (UniqueConstraint("run_id", "bar_open", name="uq_paper_event_run_bar"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    bar_open: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PaperPosition(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    setup: EntrySetup
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    entry_time: datetime
    leverage: int
    bars_held: int = 0
    funding_pnl: Decimal = ZERO


class PaperState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cash: Decimal
    equity: Decimal
    peak: Decimal
    daily_open_equity: Decimal
    local_day: str
    realized_pnl: Decimal = ZERO
    last_bar: datetime
    pending: EntrySetup | None = None
    position: PaperPosition | None = None
    fault: str | None = None


class PaperConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    implementation: str = PAPER_VERSION
    environment: Literal["testnet", "production"]
    risk: RiskConfiguration
    strategy: StrategyConfiguration
    simulation: BacktestConfiguration
    runtime_version: int = Field(ge=1)

    @field_validator("strategy", mode="before")
    @classmethod
    def preserve_legacy_strategy(cls, value: object) -> object:
        if isinstance(value, dict) and "protection_mode" not in value:
            return {**value, "protection_mode": "legacy_atr"}
        return value


def create_paper_run(
    session: Session,
    symbol: str,
    simulation: BacktestConfiguration,
    *,
    environment: Literal["testnet", "production"],
    start: datetime,
    actor: str,
    risk_override: RiskConfiguration | None = None,
    expected_risk_version: int | None = None,
) -> PaperRun:
    from zoneinfo import ZoneInfo

    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("paper start must be timezone-aware")
    start = start.astimezone(UTC)
    if start.minute % 15 or start.second or start.microsecond:
        raise ValueError("paper start must align to a 15-minute open")
    # One simulated portfolio, not independent accounts pretending to share capital.
    if session.scalar(select(PaperRun.id).where(PaperRun.status != "stopped")) is not None:
        raise ValueError("stop the existing paper run before creating another")
    risk = load_risk_configuration(session)
    if risk_override is not None:
        if expected_risk_version != risk.version:
            raise ConfigurationConflict("risk settings changed while verifying leverage; reload")
        risk = type(risk)(version=risk.version, configuration=risk_override)
    strategy = load_strategy_configuration(session)
    if not any(item.symbol == symbol and item.enabled for item in risk.configuration.symbols):
        raise ValueError("paper symbol must be enabled in saved risk settings")
    configuration = PaperConfiguration(
        environment=environment,
        risk=risk.configuration,
        strategy=strategy.configuration,
        simulation=simulation,
        runtime_version=risk.version,
    )
    state = PaperState(
        cash=simulation.starting_balance,
        equity=simulation.starting_balance,
        peak=simulation.starting_balance,
        daily_open_equity=simulation.starting_balance,
        local_day=start.astimezone(ZoneInfo(simulation.reporting_timezone)).date().isoformat(),
        last_bar=start - timedelta(minutes=15),
    )
    row = PaperRun(
        status="paused",
        symbol=symbol,
        configuration=configuration.model_dump(mode="json"),
        state=state.model_dump(mode="json"),
    )
    session.add(row)
    session.flush()
    _audit(session, row.id, actor, "paper.created", {"quote": risk.configuration.quote_asset})
    return row


def control_paper_run(
    session: Session,
    run_id: str,
    action: Literal["resume", "pause", "stop", "recover"],
    *,
    expected_version: int,
    actor: str,
) -> PaperRun:
    row = _load(session, run_id, expected_version)
    state = PaperState.model_validate(row.state)
    if action == "recover":
        if row.status != "faulted":
            raise ValueError("only a faulted run requires recovery acknowledgment")
        status = "paused"
        state = state.model_copy(update={"fault": None})
    elif action == "resume":
        if row.status != "paused":
            raise ValueError("only a healthy paused run can resume")
        status = "running"
    elif action == "pause":
        if row.status != "running":
            raise ValueError("only a running paper run can pause")
        status = "paused"
    elif action == "stop":
        if row.status == "stopped":
            raise ValueError("paper run is already stopped")
        status = "stopping" if state.position is not None else "stopped"
    else:
        raise ValueError("unsupported paper action")
    state = state.model_copy(update={"pending": None})
    _save(session, row, state, status)
    _audit(session, row.id, actor, f"paper.{action}", {"status": status})
    return row


def fault_paper_run(session: Session, run_id: str, message: str) -> None:
    row = session.get(PaperRun, run_id)
    if row is None or row.status in ("stopped", "faulted"):
        return
    state = PaperState.model_validate(row.state).model_copy(
        update={"fault": message, "pending": None}
    )
    _save(session, row, state, "faulted")
    _audit(session, run_id, "paper-worker", "paper.faulted", {"reason": message})


def process_paper_bar(
    session: Session,
    run_id: str,
    hourly: Sequence[Kline],
    entries: Sequence[Kline],
    funding: Sequence[FundingRate],
    rules: SymbolRules,
    *,
    now: datetime,
) -> PaperRun:
    """Commit is caller-owned; a repeated bar is idempotent, gaps fault explicitly."""
    from zoneinfo import ZoneInfo

    row = session.get(PaperRun, run_id)
    if row is None:
        raise ValueError("paper run does not exist")
    if row.status not in ("running", "paused", "stopping"):
        raise ValueError("paper run is not processing bars")
    configuration = PaperConfiguration.model_validate(row.configuration)
    state = PaperState.model_validate(row.state)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("paper clock must be timezone-aware")
    if not entries:
        raise ValueError("paper candle history is empty")
    candle = entries[-1]
    if candle.close_time >= now:
        raise ValueError("paper cannot execute an unclosed candle")
    if candle.open_time <= state.last_bar:
        return row
    if candle.open_time != state.last_bar + timedelta(minutes=15):
        fault_paper_run(session, run_id, "missing paper bars; operator reconciliation required")
        return row
    if (
        rules.symbol != row.symbol
        or rules.quote_asset != configuration.risk.quote_asset
        or rules.margin_asset != configuration.risk.quote_asset
    ):
        fault_paper_run(session, run_id, "exchange contract denomination mismatch")
        return row
    evaluation = evaluate_pullback(
        row.symbol,
        hourly,
        entries,
        rules,
        configuration.strategy,
        as_of=candle.open_time,
        expected_entry_slippage=configuration.simulation.slippage_fraction,
        minimum_reward_to_risk=configuration.risk.policy.minimum_reward_to_risk,
    )
    _require_history(evaluation.reason)
    # Validate the execution bar as well as preceding strategy inputs.
    from futures_bot.strategy.pullback import _validate_candles

    _validate_candles((candle,), "15m")
    with localcontext(Context(prec=40)):
        cash = state.cash
        realized = state.realized_pnl
        position = state.position
        pending = state.pending
        details: dict[str, Any] = {
            "simulated": True,
            "entries_enabled": row.status == "running",
            "strategy_reason": evaluation.reason,
        }
        day = (
            candle.open_time.astimezone(ZoneInfo(configuration.simulation.reporting_timezone))
            .date()
            .isoformat()
        )
        day_open = state.equity if day != state.local_day else state.daily_open_equity
        entered = False
        if position is None and row.status == "running":
            pending = pending or evaluation.setup
            if pending is not None:
                trigger = evaluate_entry(pending, candle)
                if trigger.status in ("invalidated", "expired"):
                    pending = None
                elif trigger.status == "triggered":
                    assert trigger.fill_price is not None
                    price = _slipped_price(
                        trigger.fill_price,
                        pending.direction == "long",
                        configuration.simulation.slippage_fraction,
                        rules.price_tick,
                    )
                    leverage = next(
                        item.leverage
                        for item in configuration.risk.symbols
                        if item.symbol == row.symbol
                    )
                    decision = _size_entry(
                        pending,
                        price,
                        cash,
                        cash,
                        realized,
                        candle.open_time,
                        state.peak,
                        day_open,
                        rules,
                        configuration.risk,
                        leverage,
                    )
                    details["risk_reasons"] = list(decision.reasons)
                    if decision.accepted:
                        assert decision.quantity is not None
                        quantity = decision.quantity
                        fee = quantity * price * configuration.simulation.fee_fraction
                        reserve = cash * configuration.risk.policy.margin_reserve_fraction
                        if quantity * price / leverage + fee > cash - reserve:
                            details["risk_reasons"] = ["entry fee would consume reserved margin"]
                        elif now - candle.close_time > timedelta(seconds=90):
                            details["risk_reasons"] = [
                                "late recovered bar cannot open a new position"
                            ]
                        else:
                            position = PaperPosition(
                                id=str(uuid4()),
                                setup=pending,
                                quantity=quantity,
                                entry_price=price,
                                entry_fee=fee,
                                entry_time=candle.open_time,
                                leverage=leverage,
                            )
                            cash -= fee
                            entered = True
                            _record_entry(session, position, configuration)
                            details["entry"] = position.model_dump(mode="json")
                    pending = None
        if position is not None:
            position = position.model_copy(update={"bars_held": position.bars_held + 1})
            exit_result = evaluate_exit(
                position.setup, candle, bars_held=position.bars_held, entry_bar=entered
            )
            worst = candle.low if position.setup.direction == "long" else candle.high
            maintenance = (
                position.quantity * worst * configuration.simulation.maintenance_margin_fraction
            )
            ambiguous = (
                entered
                or (exit_result is not None and exit_result.reason in ("stop", "target"))
                or cash + _pnl(position, worst) <= maintenance
            )
            seen: datetime | None = None
            payment = ZERO
            for rate in funding:
                if rate.symbol != row.symbol or not rate.rate.is_finite():
                    raise ValueError("invalid paper funding symbol or rate")
                if rate.mark_price <= 0 or not rate.mark_price.is_finite():
                    raise ValueError("paper funding requires finite positive mark price")
                if seen is not None and rate.funding_time <= seen:
                    raise ValueError("paper funding must be strictly ordered and unique")
                seen = rate.funding_time
                if candle.open_time <= rate.funding_time <= candle.close_time:
                    if rate.funding_time <= position.entry_time:
                        continue
                    amount = position.quantity * rate.mark_price * rate.rate
                    if position.setup.direction == "long":
                        amount = -amount
                    payment += min(ZERO, amount) if ambiguous else amount
            cash += payment
            position = position.model_copy(update={"funding_pnl": position.funding_pnl + payment})
            liquidated = cash + _pnl(position, worst) <= maintenance
            if liquidated or exit_result is not None:
                if liquidated:
                    reason = "conservative_liquidation"
                    price = worst
                else:
                    assert exit_result is not None
                    reason = exit_result.reason
                    price = exit_result.price
                price = _slipped_price(
                    price,
                    position.setup.direction == "short",
                    configuration.simulation.slippage_fraction,
                    rules.price_tick,
                )
                fee_rate = configuration.simulation.fee_fraction
                if liquidated:
                    fee_rate += configuration.simulation.liquidation_fee_fraction
                fee = price * position.quantity * fee_rate
                gross = _pnl(position, price)
                cash += gross - fee
                net = gross - position.entry_fee - fee + position.funding_pnl
                realized += net
                _record_exit(
                    session, position, configuration, price, fee, gross, net, reason, candle
                )
                details["exit"] = {"reason": reason, "net_pnl": str(net), "position": position.id}
                position = None
                if liquidated:
                    row.status = "faulted"
                    details["fault"] = "liquidation stress triggered; operator review required"
        equity = cash + (_pnl(position, candle.close) if position else ZERO)
        next_status = row.status
        if row.status == "stopping" and position is None:
            next_status = "stopped"
        new_state = PaperState(
            cash=cash,
            equity=equity,
            peak=max(state.peak, equity),
            daily_open_equity=day_open,
            local_day=day,
            realized_pnl=realized,
            last_bar=candle.open_time,
            pending=pending,
            position=position,
            fault=details.get("fault"),
        )
        _save(session, row, new_state, next_status)
        session.add(
            PaperEvent(
                run_id=row.id,
                bar_open=candle.open_time,
                details={**details, "equity": str(equity), "cash": str(cash)},
            )
        )
        session.flush()
    return row


def _load(session: Session, run_id: str, version: int) -> PaperRun:
    row = session.get(PaperRun, run_id)
    if row is None:
        raise ValueError("paper run does not exist")
    if row.version != version:
        raise ConfigurationConflict("paper state changed; reload before acting")
    return row


def _save(session: Session, row: PaperRun, state: PaperState, status: str) -> None:
    version = row.version
    result = session.execute(
        update(PaperRun)
        .where(PaperRun.id == row.id, PaperRun.version == version)
        .values(
            version=version + 1,
            state=state.model_dump(mode="json"),
            status=status,
            active_slot=None if status == "stopped" else 1,
            updated_at=utc_now(),
        )
        .returning(PaperRun.id),
        execution_options={"synchronize_session": False},
    )
    if result.scalar_one_or_none() is None:
        raise ConfigurationConflict("paper bar or control raced another writer")
    session.expire(row)
    session.refresh(row)


def _audit(session: Session, run_id: str, actor: str, action: str, details: dict[str, Any]) -> None:
    if not actor.strip():
        raise ValueError("paper audit actor must not be empty")
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type="paper_run",
            resource_id=run_id,
            details=details,
        )
    )
    session.flush()


def _pnl(position: PaperPosition, price: Decimal) -> Decimal:
    difference = price - position.entry_price
    return position.quantity * (difference if position.setup.direction == "long" else -difference)


def _record_entry(
    session: Session,
    position: PaperPosition,
    configuration: PaperConfiguration,
) -> None:
    setup = position.setup
    signal_id = str(uuid5(NAMESPACE_URL, f"paper:{position.id}:{setup.id}"))
    existing = session.scalar(
        select(Signal).where(
            Signal.strategy_version == configuration.strategy.checksum,
            Signal.symbol == setup.symbol,
            Signal.direction == setup.direction,
            Signal.signal_time == setup.signal_time,
        )
    )
    if existing is None:
        session.add(
            Signal(
                id=signal_id,
                strategy_version=configuration.strategy.checksum,
                symbol=setup.symbol,
                direction=setup.direction,
                signal_time=setup.signal_time,
                status="filled",
                configuration_snapshot=configuration.model_dump(mode="json"),
                inputs={"setup_id": setup.id, "simulated": True},
            )
        )
        session.flush()
    else:
        signal_id = existing.id
    order = OrderRecord(
        signal_id=signal_id,
        environment="simulated",
        symbol=setup.symbol,
        client_order_id=f"paper-{position.id}",
        side="buy" if setup.direction == "long" else "sell",
        order_type="stop_market",
        status="filled",
        quantity=position.quantity,
        average_fill_price=position.entry_price,
    )
    session.add(order)
    session.flush()
    session.add(
        Fill(
            order_id=order.id,
            symbol=setup.symbol,
            quantity=position.quantity,
            price=position.entry_price,
            commission=position.entry_fee,
            commission_asset=configuration.risk.quote_asset,
            filled_at=position.entry_time,
        )
    )
    session.add(
        Position(
            id=position.id,
            mode="paper",
            symbol=setup.symbol,
            side=setup.direction,
            quantity=position.quantity,
            entry_price=position.entry_price,
            stop_price=setup.stop_price,
            target_price=setup.target_price,
            status="open",
            opened_at=position.entry_time,
        )
    )
    session.flush()


def _record_exit(
    session: Session,
    position: PaperPosition,
    configuration: PaperConfiguration,
    price: Decimal,
    fee: Decimal,
    gross: Decimal,
    net: Decimal,
    reason: str,
    candle: Kline,
) -> None:
    stored = session.get(Position, position.id)
    if stored is None or stored.status != "open":
        raise ValueError("paper position ledger is inconsistent")
    stored.status = "closed"
    stored.closed_at = candle.close_time
    order = OrderRecord(
        environment="simulated",
        symbol=position.setup.symbol,
        client_order_id=f"paper-exit-{position.id}",
        side="sell" if position.setup.direction == "long" else "buy",
        order_type="market",
        status="filled",
        quantity=position.quantity,
        average_fill_price=price,
    )
    session.add(order)
    session.flush()
    session.add(
        Fill(
            order_id=order.id,
            symbol=position.setup.symbol,
            quantity=position.quantity,
            price=price,
            commission=fee,
            commission_asset=configuration.risk.quote_asset,
            filled_at=candle.close_time,
        )
    )
    session.add(
        Trade(
            position_id=position.id,
            strategy_version=configuration.strategy.checksum,
            symbol=position.setup.symbol,
            side=position.setup.direction,
            entry_price=position.entry_price,
            exit_price=price,
            quantity=position.quantity,
            gross_pnl=gross,
            fees=position.entry_fee + fee,
            funding_pnl=position.funding_pnl,
            net_pnl=net,
            exit_reason=reason,
            opened_at=position.entry_time,
            closed_at=candle.close_time,
        )
    )
