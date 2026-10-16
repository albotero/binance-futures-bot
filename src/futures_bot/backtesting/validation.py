"""Offline parameter research; no runtime configuration or execution side effects."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from futures_bot.backtesting.engine import (
    BACKTEST_VERSION,
    BacktestConfiguration,
    BacktestResult,
    run_backtest,
)
from futures_bot.exchange import FundingRate, Kline, SymbolRules
from futures_bot.risk.configuration import RiskConfiguration
from futures_bot.strategy import StrategyConfiguration

VALIDATION_VERSION = "purged-walk-forward-v1"
_BAR = timedelta(minutes=15)
_ZERO = Decimal(0)


class ResearchWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def aligned_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("research boundaries must be timezone-aware")
        value = value.astimezone(UTC)
        if value.minute % 15 or value.second or value.microsecond:
            raise ValueError("research boundaries must align to 15-minute opens")
        return value

    @model_validator(mode="after")
    def ordered(self) -> ResearchWindow:
        if self.end <= self.start:
            raise ValueError("research window end must follow start")
        return self


class WalkForwardFold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    training: ResearchWindow
    validation: ResearchWindow

    @model_validator(mode="after")
    def chronological(self) -> WalkForwardFold:
        if self.training.end > self.validation.start:
            raise ValueError("training must end before validation")
        return self


class ValidationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    folds: tuple[WalkForwardFold, ...] = Field(min_length=1, max_length=12)
    holdout: ResearchWindow
    purge_bars: StrictInt = Field(ge=1, le=1000)

    @model_validator(mode="after")
    def purged_chronology(self) -> ValidationPlan:
        gap = self.purge_bars * _BAR
        previous: WalkForwardFold | None = None
        for fold in self.folds:
            if fold.validation.start - fold.training.end < gap:
                raise ValueError("training/validation gap is smaller than purge_bars")
            if previous is not None:
                if fold.training.start < previous.training.start:
                    raise ValueError("training starts must not move backwards")
                if fold.training.end <= previous.training.end:
                    raise ValueError("training ends must advance chronologically")
                if fold.validation.start < previous.validation.end:
                    raise ValueError("validation windows must be ordered and non-overlapping")
            previous = fold
        if self.holdout.start - self.folds[-1].validation.end < gap:
            raise ValueError("holdout must follow all validation windows with a purge gap")
        return self


class SearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    minimum_training_trades: StrictInt = Field(default=20, ge=1)
    minimum_validation_trades: StrictInt = Field(default=10, ge=1)
    maximum_drawdown_fraction: Decimal = Field(default=Decimal("0.2"), gt=0, le=1)
    minimum_validation_return: Decimal = Field(default=Decimal("0"), ge=0)
    drawdown_penalty: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    maximum_backtests: StrictInt = Field(default=128, ge=4, le=128)

    @field_validator(
        "maximum_drawdown_fraction",
        "minimum_validation_return",
        "drawdown_penalty",
        mode="before",
    )
    @classmethod
    def exact_decimal(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("search decimal values must be exact strings")
        return value


class FoldResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    training: BacktestResult
    validation: BacktestResult


class CandidateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    configuration: StrategyConfiguration
    checksum: str
    folds: tuple[FoldResult, ...]
    eligible: bool
    rejection_reasons: tuple[str, ...]
    worst_validation_score: Decimal
    mean_validation_score: Decimal


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    configuration_snapshot: dict[str, object]
    plan: ValidationPlan
    policy: SearchPolicy
    baseline_checksum: str
    candidates: tuple[CandidateResult, ...]
    selected_checksum: str | None
    selection_reason: str
    selected_holdout: BacktestResult | None
    baseline_holdout: BacktestResult
    backtests_run: int
    limitations: tuple[str, ...]


def run_walk_forward_search(
    symbol: str,
    hourly_candles: Sequence[Kline],
    entry_candles: Sequence[Kline],
    funding_rates: Sequence[FundingRate],
    rules: SymbolRules,
    baseline: StrategyConfiguration,
    candidates: Sequence[StrategyConfiguration],
    risk: RiskConfiguration,
    simulation: BacktestConfiguration,
    plan: ValidationPlan,
    policy: SearchPolicy,
) -> ValidationResult:
    """Rank fixed candidates using development folds, then evaluate a frozen choice."""
    configurations = (baseline, *candidates)
    if len(configurations) > 16:
        raise ValueError("search allows at most 16 strategies including the baseline")
    checksums = [configuration.checksum for configuration in configurations]
    if len(set(checksums)) != len(checksums):
        raise ValueError("search candidates and baseline must be unique")
    horizon = max(item.entry_expiry_bars + item.time_stop_bars + 1 for item in configurations)
    if plan.purge_bars < horizon:
        raise ValueError("purge_bars must cover every candidate's entry plus exit horizon")
    budget = len(configurations) * len(plan.folds) * 2 + 2
    if budget > policy.maximum_backtests:
        raise ValueError("search exceeds the configured backtest budget")
    hourly = tuple(hourly_candles)
    entries = tuple(entry_candles)
    funding = tuple(funding_rates)

    def evaluate(configuration: StrategyConfiguration, window: ResearchWindow) -> BacktestResult:
        # Later bars and funding never reach a development backtest, even for provenance.
        return run_backtest(
            symbol,
            tuple(candle for candle in hourly if candle.close_time < window.end),
            tuple(candle for candle in entries if candle.close_time < window.end),
            tuple(rate for rate in funding if window.start <= rate.funding_time < window.end),
            rules,
            configuration,
            risk,
            simulation,
            start=window.start,
            end=window.end,
        )

    with localcontext(Context(prec=40)):
        results: list[CandidateResult] = []
        for configuration in configurations:
            folds = tuple(
                FoldResult(
                    training=evaluate(configuration, fold.training),
                    validation=evaluate(configuration, fold.validation),
                )
                for fold in plan.folds
            )
            reasons = _rejection_reasons(folds, policy)
            scores = tuple(
                fold.validation.net_pnl / fold.validation.starting_balance
                - policy.drawdown_penalty * fold.validation.maximum_drawdown_fraction
                for fold in folds
            )
            results.append(
                CandidateResult(
                    configuration=configuration,
                    checksum=configuration.checksum,
                    folds=folds,
                    eligible=not reasons,
                    rejection_reasons=reasons,
                    worst_validation_score=min(scores),
                    mean_validation_score=sum(scores, _ZERO) / len(scores),
                )
            )
        eligible = [result for result in results if result.eligible]
        # Sort identity first so exact score ties cannot depend on candidate input order.
        eligible.sort(key=lambda result: result.checksum)
        eligible.sort(
            key=lambda result: (result.worst_validation_score, result.mean_validation_score),
            reverse=True,
        )
        selected = eligible[0] if eligible else None
        baseline_holdout = evaluate(baseline, plan.holdout)
        selected_holdout = None
        backtests_run = len(configurations) * len(plan.folds) * 2 + 1
        if selected is not None:
            if selected.checksum == baseline.checksum:
                selected_holdout = baseline_holdout
            else:
                selected_holdout = evaluate(selected.configuration, plan.holdout)
                backtests_run += 1
        return ValidationResult(
            symbol=symbol,
            configuration_snapshot={
                "validation_version": VALIDATION_VERSION,
                "engine_version": BACKTEST_VERSION,
                "strategy_version": baseline.implementation,
                "risk": risk.model_dump(mode="json"),
                "simulation": simulation.model_dump(mode="json"),
                "strategies": [item.model_dump(mode="json") for item in configurations],
                "plan": plan.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
            },
            plan=plan,
            policy=policy,
            baseline_checksum=baseline.checksum,
            candidates=tuple(results),
            selected_checksum=selected.checksum if selected is not None else None,
            selection_reason=(
                "Ranked by worst then mean validation return minus drawdown penalty; "
                "checksum breaks ties. Holdout results do not affect selection."
                if selected is not None
                else "No candidate passed every development fold; no strategy selected."
            ),
            selected_holdout=selected_holdout,
            baseline_holdout=baseline_holdout,
            backtests_run=backtests_run,
            limitations=(
                "Deterministic finite parameter sweep, not a trained predictive ML model.",
                "Validation windows guide selection and are not untouched out-of-sample proof.",
                "The final holdout is excluded from selection within this call only; "
                "reusing it to revise candidates contaminates that holdout.",
                "Each fold resets cash and positions; fold returns are not a compounded portfolio.",
                "Prior closed bars can warm indicators, including purge gaps; no gap trades occur.",
                "All single-symbol simulator limitations apply, including static exchange rules.",
                "Reports do not promote parameters, modify risk/leverage, or authorize trading.",
            ),
        )


def _rejection_reasons(folds: tuple[FoldResult, ...], policy: SearchPolicy) -> tuple[str, ...]:
    reasons: list[str] = []
    for index, fold in enumerate(folds, start=1):
        for label, result, minimum in (
            ("training", fold.training, policy.minimum_training_trades),
            ("validation", fold.validation, policy.minimum_validation_trades),
        ):
            prefix = f"fold {index} {label}"
            if result.halted:
                reasons.append(f"{prefix}: simulation halted")
            if len(result.trades) < minimum:
                reasons.append(f"{prefix}: insufficient trades")
            if result.maximum_drawdown_fraction > policy.maximum_drawdown_fraction:
                reasons.append(f"{prefix}: drawdown exceeds research limit")
        if (
            fold.validation.net_pnl / fold.validation.starting_balance
            <= policy.minimum_validation_return
        ):
            reasons.append(f"fold {index} validation: return must exceed research minimum")
    return tuple(reasons)
