"""Atomic shared-account hourly paper batches; no exchange order transport."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from futures_bot.backtesting.engine import (
    _require_history,
    _slipped_price,
    size_entry_with_leverage,
)
from futures_bot.exchange import FundingRate, Kline, SymbolRules
from futures_bot.execution.paper import (
    PaperConfiguration,
    PaperEvent,
    PaperPosition,
    PaperRun,
    PaperState,
    _audit,
    _pnl,
    _record_entry,
    _record_exit,
    _save,
    fault_paper_run,
)
from futures_bot.risk.engine import PortfolioState, RiskDecision
from futures_bot.strategy import evaluate_pullback
from futures_bot.strategy.lifecycle import evaluate_entry, evaluate_exit
from futures_bot.strategy.pullback import _validate_candles, adverse_fill_price, market_direction

ZERO = Decimal(0)


def process_portfolio_bar(
    session: Session,
    run_id: str,
    histories: Mapping[str, Sequence[Kline]],
    funding: Mapping[str, Sequence[FundingRate]],
    rules: Mapping[str, SymbolRules],
    market_candles: Sequence[Kline],
    *,
    now: datetime,
) -> PaperRun:
    with localcontext(Context(prec=40)):
        return _process(session, run_id, histories, funding, rules, market_candles, now=now)


def _process(session, run_id, histories, funding, rules, market_candles, *, now):
    row = session.get(PaperRun, run_id)
    if row is None or row.status not in ("running", "paused", "stopping"):
        raise ValueError("portfolio run is not processing")
    config = PaperConfiguration.model_validate(row.configuration)
    state = PaperState.model_validate(row.state)
    symbols = config.symbols
    if not symbols or config.strategy.profile != "hourly_market":
        raise ValueError("invalid hourly portfolio snapshot")
    if set(histories) != set(symbols) or set(rules) != set(symbols) or set(funding) != set(symbols):
        raise ValueError("portfolio batch must cover every configured symbol")
    bars = {}
    for symbol in symbols:
        history = histories[symbol]
        if not history:
            raise ValueError(f"missing hourly portfolio history: {symbol}")
        _validate_candles(history, "1h")
        bars[symbol] = history[-1]
        contract = rules[symbol]
        if (
            contract.symbol != symbol
            or contract.quote_asset != config.risk.quote_asset
            or contract.margin_asset != config.risk.quote_asset
        ):
            raise ValueError("portfolio contract denomination mismatch")
    time = bars[symbols[0]].open_time
    if any(c.open_time != time for c in bars.values()):
        raise ValueError("portfolio candles must share one hourly boundary")
    if now.tzinfo is None or any(c.close_time >= now for c in bars.values()):
        raise ValueError("portfolio requires closed candles and an aware clock")
    if time <= state.last_bar:
        return row
    if time != state.last_bar + timedelta(hours=1):
        fault_paper_run(session, row.id, "missing portfolio bars; operator reconciliation required")
        return row
    direction = market_direction(market_candles, as_of=time)
    positions, pendings = dict(state.positions), dict(state.pendings)
    if state.position is not None or state.pending is not None:
        raise ValueError("portfolio snapshot contains single-symbol state")
    if not set(positions) <= set(symbols) or not set(pendings) <= set(symbols):
        raise ValueError("portfolio contains an unconfigured position or pending entry")
    cash, realized = state.cash, state.realized_pnl
    day = time.astimezone(ZoneInfo(config.simulation.reporting_timezone)).date().isoformat()
    day_open = state.equity if day != state.local_day else state.daily_open_equity
    opening_equity = cash + sum((_pnl(p, bars[s].open) for s, p in positions.items()), ZERO)
    peak = max(state.peak, opening_equity)
    details = {
        "simulated": True,
        "portfolio": True,
        "market_direction": direction,
        "daily_open_equity": str(day_open),
        "symbols": {},
    }
    entered = set()
    occupied = set(positions)
    # Alphabetical order is stable. Exits later in this bar cannot fund new entries.
    for symbol in symbols:
        info = {}
        details["symbols"][symbol] = info
        if symbol in occupied or row.status != "running":
            pendings.pop(symbol, None)
            continue
        history, contract, candle = histories[symbol], rules[symbol], bars[symbol]
        evaluation = evaluate_pullback(
            symbol,
            history,
            history,
            contract,
            config.strategy,
            as_of=time,
            expected_entry_slippage=config.simulation.slippage_fraction,
            minimum_reward_to_risk=config.risk.policy.minimum_reward_to_risk,
            expected_fee_fraction=config.simulation.fee_fraction,
            market_candles=market_candles,
        )
        _require_history(evaluation.reason)
        info["strategy_reason"] = evaluation.reason
        pending = pendings.get(symbol) or evaluation.setup
        if pending is None or pending.direction != direction:
            pendings.pop(symbol, None)
            continue
        trigger = evaluate_entry(pending, candle)
        if trigger.status == "pending":
            pendings[symbol] = pending
            continue
        pendings.pop(symbol, None)
        if trigger.status != "triggered":
            continue
        assert trigger.fill_price is not None
        price = _slipped_price(
            trigger.fill_price,
            pending.direction == "long",
            config.simulation.slippage_fraction,
            contract.price_tick,
        )
        equity = cash + sum((_pnl(p, bars[s].open) for s, p in positions.items()), ZERO)
        margin = sum((p.quantity * p.entry_price / p.leverage for p in positions.values()), ZERO)
        exposure = sum((p.quantity * bars[s].open for s, p in positions.items()), ZERO)
        aggregate = ZERO
        for s, p in positions.items():
            stop_fill = adverse_fill_price(
                p.setup.stop_price,
                p.setup.direction == "short",
                config.simulation.slippage_fraction,
                rules[s].price_tick,
            )
            # Include loss from current marked equity to SL, and original modeled
            # account risk, so floating profits cannot erase portfolio risk.
            marked_loss = (bars[s].open - stop_fill) * (1 if p.setup.direction == "long" else -1)
            original = p.entry_sizing.estimated_stop_loss if p.entry_sizing else None
            aggregate += max(
                ZERO,
                original or ZERO,
                p.quantity * (marked_loss + stop_fill * config.simulation.fee_fraction),
            )
        portfolio = PortfolioState(
            open_position_count=len(positions),
            aggregate_open_risk=aggregate,
            daily_loss=max(ZERO, day_open - equity) + aggregate,
            drawdown_fraction=min(Decimal(1), max(ZERO, (peak - equity) / peak)),
            protective_orders_healthy=True,
            connectivity_healthy=True,
        )
        leverage = next(s.leverage for s in config.risk.symbols if s.symbol == symbol)
        decision, leverage = size_entry_with_leverage(
            pending,
            price,
            cash,
            equity,
            realized,
            time,
            peak,
            day_open,
            contract,
            config.risk,
            leverage,
            fee_fraction=config.simulation.fee_fraction,
            stop_slippage_fraction=config.simulation.slippage_fraction,
            portfolio_override=portfolio,
            available_balance=max(ZERO, cash - margin),
            open_exposure=exposure,
        )
        info["sizing"] = TypeAdapter(RiskDecision).dump_python(decision, mode="json")
        info["leverage"] = leverage
        if not decision.accepted:
            info["risk_reasons"] = list(decision.reasons)
            continue
        if now - candle.close_time > timedelta(seconds=90):
            info["risk_reasons"] = ["late recovered bar cannot open a new position"]
            continue
        quantity = decision.quantity
        assert quantity is not None
        fee = quantity * price * config.simulation.fee_fraction
        reserve = max(ZERO, cash - margin) * config.risk.policy.margin_reserve_fraction
        if quantity * price / leverage + fee > cash - margin - reserve:
            info["risk_reasons"] = ["entry fee would consume reserved portfolio margin"]
            continue
        position = PaperPosition(
            id=str(uuid4()),
            setup=pending,
            quantity=quantity,
            entry_price=price,
            entry_fee=fee,
            entry_time=time,
            leverage=leverage,
            entry_sizing=decision,
        )
        positions[symbol] = position
        cash -= fee
        entered.add(symbol)
        _record_entry(session, position, config)
        info["entry"] = position.model_dump(mode="json")
    exits, positive_funding = {}, {}
    for symbol, position in list(positions.items()):
        candle = bars[symbol]
        position = position.model_copy(update={"bars_held": position.bars_held + 1})
        result = evaluate_exit(
            position.setup, candle, bars_held=position.bars_held, entry_bar=symbol in entered
        )
        exits[symbol] = result
        payment, positive_payment, previous = ZERO, ZERO, None
        for rate in funding[symbol]:
            if (
                rate.symbol != symbol
                or not rate.rate.is_finite()
                or not rate.mark_price.is_finite()
                or rate.mark_price <= 0
                or (previous is not None and rate.funding_time <= previous)
            ):
                raise ValueError("invalid portfolio funding observations")
            previous = rate.funding_time
            if candle.open_time <= rate.funding_time <= candle.close_time and (
                rate.funding_time > position.entry_time
            ):
                amount = position.quantity * rate.mark_price * rate.rate
                if position.setup.direction == "long":
                    amount = -amount
                if symbol in entered or result:
                    payment += min(ZERO, amount)
                else:
                    payment += amount
                    positive_payment += max(ZERO, amount)
        cash += payment
        positive_funding[symbol] = positive_payment
        positions[symbol] = position.model_copy(
            update={
                "funding_pnl": position.funding_pnl + payment,
            }
        )
    worst_equity = cash + sum(
        (
            _pnl(p, bars[s].low if p.setup.direction == "long" else bars[s].high)
            for s, p in positions.items()
        ),
        ZERO,
    )
    maintenance = sum(
        (
            p.quantity
            * (bars[s].low if p.setup.direction == "long" else bars[s].high)
            * config.simulation.maintenance_margin_fraction
            for s, p in positions.items()
        ),
        ZERO,
    )
    liquidated = (
        bool(positions) and worst_equity - sum(positive_funding.values(), ZERO) <= maintenance
    )
    if liquidated:
        # OHLC cannot prove favorable funding preceded an intrabar liquidation.
        for symbol, payment in positive_funding.items():
            cash -= payment
            position = positions[symbol]
            positions[symbol] = position.model_copy(
                update={"funding_pnl": position.funding_pnl - payment}
            )
    for symbol, position in list(positions.items()):
        result = exits[symbol]
        if not liquidated and result is None:
            continue
        candle, contract = bars[symbol], rules[symbol]
        reason = "conservative_liquidation" if liquidated else result.reason
        raw = (
            (candle.low if position.setup.direction == "long" else candle.high)
            if (liquidated)
            else result.price
        )
        price = _slipped_price(
            raw,
            position.setup.direction == "short",
            config.simulation.slippage_fraction,
            contract.price_tick,
        )
        fee_rate = config.simulation.fee_fraction + (
            config.simulation.liquidation_fee_fraction if liquidated else ZERO
        )
        fee, gross = price * position.quantity * fee_rate, _pnl(position, price)
        cash += gross - fee
        net = gross - position.entry_fee - fee + position.funding_pnl
        realized += net
        _record_exit(session, position, config, price, fee, gross, net, reason, candle)
        details["symbols"][symbol]["exit"] = {
            "reason": reason,
            "net_pnl": str(net),
            "position": position.id,
        }
        del positions[symbol]
    equity = cash + sum((_pnl(p, bars[s].close) for s, p in positions.items()), ZERO)
    peak = max(peak, equity)
    status = "faulted" if liquidated or cash <= 0 else row.status
    if status == "stopping" and not positions:
        status = "stopped"
    if status != "running":
        pendings.clear()
    updated = state.model_copy(
        update={
            "cash": cash,
            "equity": equity,
            "peak": peak,
            "daily_open_equity": day_open,
            "local_day": day,
            "last_bar": time,
            "positions": positions,
            "pendings": pendings,
            "realized_pnl": realized,
            "fault": "portfolio liquidation or exhausted cash" if status == "faulted" else None,
        }
    )
    details.update(
        {
            "cash": str(cash),
            "equity": str(equity),
            "open_positions": len(positions),
            "drawdown": str((peak - equity) / peak),
        }
    )
    session.add(PaperEvent(run_id=row.id, bar_open=time, details=details))
    _save(session, row, updated, status)
    _audit(session, row.id, "paper-worker", "paper.portfolio_bar", details)
    return row
