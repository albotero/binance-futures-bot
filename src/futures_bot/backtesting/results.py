import csv
import io

from sqlalchemy.orm import Session

from futures_bot.backtesting.engine import BacktestResult, SimulatedTrade
from futures_bot.backtesting.validation import ValidationResult
from futures_bot.persistence.models import AuditEvent, BacktestRun, utc_now
from futures_bot.persistence.repositories import create_backtest_run
from futures_bot.strategy.pullback import TIMEFRAME_PAIRS


def save_backtest_result(session: Session, result: BacktestResult, *, actor: str) -> BacktestRun:
    """Save a completed snapshot and ledger; the caller owns the transaction."""
    timeframes = result.configuration_snapshot.get("timeframes")
    intervals = ["1h", "15m"]
    if timeframes is not None:
        if (
            not isinstance(timeframes, dict)
            or (timeframes.get("entry"), timeframes.get("trend")) not in TIMEFRAME_PAIRS
        ):
            raise ValueError("backtest snapshot has unsupported timeframe metadata")
        intervals = list(dict.fromkeys((timeframes["trend"], timeframes["entry"])))
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
            "intervals": intervals,
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


def save_validation_result(
    session: Session, result: ValidationResult, *, actor: str
) -> BacktestRun:
    """Persist all development trials and holdout results without promoting settings."""
    row = create_backtest_run(
        session,
        strategy_version=str(result.configuration_snapshot["strategy_version"]),
        configuration_snapshot=result.configuration_snapshot,
        dataset_metadata={
            "kind": "walk_forward_search",
            "symbol": result.symbol,
            "development": {
                candidate.checksum: [
                    {
                        "training": fold.training.dataset_checksum,
                        "validation": fold.validation.dataset_checksum,
                    }
                    for fold in candidate.folds
                ]
                for candidate in result.candidates
            },
            "baseline_holdout": result.baseline_holdout.dataset_checksum,
            "selected_holdout": (
                result.selected_holdout.dataset_checksum if result.selected_holdout else None
            ),
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
            action="walk_forward_search.completed",
            resource_type="backtest_run",
            resource_id=row.id,
            details={
                "candidates": len(result.candidates),
                "backtests_run": result.backtests_run,
                "selected_checksum": result.selected_checksum,
                "runtime_configuration_changed": False,
            },
        )
    )
    session.flush()
    return row
