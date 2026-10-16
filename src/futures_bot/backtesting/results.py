import csv
import io

from sqlalchemy.orm import Session

from futures_bot.backtesting.engine import BacktestResult, SimulatedTrade
from futures_bot.persistence.models import AuditEvent, BacktestRun, utc_now
from futures_bot.persistence.repositories import create_backtest_run


def save_backtest_result(session: Session, result: BacktestResult, *, actor: str) -> BacktestRun:
    """Save a completed snapshot and ledger; the caller owns the transaction."""
    row = create_backtest_run(
        session,
        strategy_version=str(result.configuration_snapshot["strategy_version"]),
        configuration_snapshot=result.configuration_snapshot,
        dataset_metadata={
            "checksum": result.dataset_checksum,
            "symbol": result.symbol,
            "quote_asset": result.quote_asset,
            "start": result.start.isoformat(),
            "end": result.end.isoformat(),
            "intervals": ["1h", "15m"],
        },
        actor=actor,
    )
    row.status = "completed"
    row.metrics = result.model_dump(mode="json")
    row.started_at = utc_now()
    row.completed_at = row.started_at
    session.add(
        AuditEvent(
            actor=actor.strip(),
            action="backtest_run.completed",
            resource_type="backtest_run",
            resource_id=row.id,
            details={
                "dataset_checksum": result.dataset_checksum,
                "trades": len(result.trades),
                "halted": result.halted,
            },
        )
    )
    session.flush()
    return row


def export_trades_csv(result: BacktestResult) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(SimulatedTrade.model_fields))
    writer.writeheader()
    for trade in result.trades:
        writer.writerow(trade.model_dump(mode="json"))
    return output.getvalue()
