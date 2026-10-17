"""Typed operator settings backed by the versioned runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from futures_bot.exchange.binance_usdm import validate_symbol_name
from futures_bot.exchange.leverage import LeverageProvider, LeverageSchedule
from futures_bot.persistence.models import ApplicationConfiguration
from futures_bot.persistence.repositories import (
    ConfigurationConflict,
    save_application_configuration,
)
from futures_bot.risk.engine import RiskPolicy

_DECIMAL_FIELDS = frozenset(
    {
        "risk_fraction",
        "margin_reserve_fraction",
        "maximum_position_notional",
        "maximum_total_exposure",
        "maximum_aggregate_risk",
        "maximum_daily_loss",
        "maximum_drawdown_fraction",
        "maximum_stop_distance_fraction",
        "minimum_reward_to_risk",
    }
)


class MissingRiskConfiguration(RuntimeError):
    """Risk settings must be explicitly saved before evaluating entries."""


class SymbolConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    enabled: StrictBool = True
    leverage: Annotated[StrictInt, Field(ge=1)] = 1
    leverage_schedule: LeverageSchedule | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        validate_symbol_name(value)
        return value


class RiskConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_asset: Literal["USDC", "USDT"] = "USDC"
    policy: RiskPolicy
    symbols: Annotated[tuple[SymbolConfiguration, ...], Field(min_length=1)]
    daily_loss_fraction: Decimal = Field(default=Decimal("0"), ge=0, le=1, allow_inf_nan=False)
    leverage_mode: Literal["fixed", "bracket_aware"] = "fixed"

    @field_validator("daily_loss_fraction", mode="before")
    @classmethod
    def exact_daily_fraction(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("daily loss fraction must be an exact decimal string")
        return value

    @field_validator("policy", mode="before")
    @classmethod
    def validate_policy_input(cls, value: object) -> object:
        if isinstance(value, RiskPolicy):
            return value
        if not isinstance(value, dict):
            raise ValueError("policy must be an object")
        unknown = set(value) - {field.name for field in fields(RiskPolicy)}
        if unknown:
            raise ValueError("policy contains unknown fields")
        normalized = dict(value)
        for name in _DECIMAL_FIELDS & value.keys():
            number = value[name]
            if not isinstance(number, (str, Decimal)):
                raise ValueError(f"{name} must be an exact decimal string")
            try:
                normalized[name] = Decimal(number)
            except InvalidOperation as exc:
                raise ValueError(f"{name} must be an exact decimal string") from exc
        for name in ("maximum_open_positions", "maximum_leverage"):
            if name in value and type(value[name]) is not int:
                raise ValueError(f"{name} must be an integer")
        for name in ("maximum_account_age", "maximum_market_age"):
            if isinstance(value.get(name), bool):
                raise ValueError(f"{name} must be a duration")
        return normalized

    @field_validator("symbols")
    @classmethod
    def validate_symbols(
        cls, value: tuple[SymbolConfiguration, ...]
    ) -> tuple[SymbolConfiguration, ...]:
        names = [item.symbol for item in value]
        if len(names) != len(set(names)):
            raise ValueError("symbols must be unique")
        return value

    @model_validator(mode="after")
    def validate_leverage_limits(self) -> RiskConfiguration:
        if any(not symbol.symbol.endswith(self.quote_asset) for symbol in self.symbols):
            raise ValueError("all configured symbols must match the selected quote asset")
        for symbol in self.symbols:
            if symbol.leverage_schedule is not None:
                if symbol.leverage_schedule.symbol != symbol.symbol:
                    raise ValueError("leverage schedule must match its symbol")
                symbol.leverage_schedule.notional_limit(symbol.leverage)
        return self


async def verify_leverage_configuration(
    configuration: RiskConfiguration, provider: LeverageProvider
) -> RiskConfiguration:
    schedules = {item.symbol: item for item in await provider.schedules()}
    symbols = []
    for symbol in configuration.symbols:
        schedule = schedules.get(symbol.symbol)
        if schedule is None:
            raise ValueError(f"{symbol.symbol} has no verified Binance leverage brackets")
        schedule.notional_limit(symbol.leverage)
        symbols.append(symbol.model_copy(update={"leverage_schedule": schedule}))
    return configuration.model_copy(update={"symbols": tuple(symbols)})


@dataclass(frozen=True, slots=True)
class VersionedRiskConfiguration:
    version: int
    configuration: RiskConfiguration


def load_risk_configuration(session: Session) -> VersionedRiskConfiguration:
    row = session.execute(
        select(ApplicationConfiguration.version, ApplicationConfiguration.configuration).where(
            ApplicationConfiguration.key == "runtime"
        )
    ).one_or_none()
    if row is None or "risk" not in row.configuration:
        raise MissingRiskConfiguration("risk configuration has not been initialized")
    return VersionedRiskConfiguration(
        version=row.version,
        configuration=RiskConfiguration.model_validate(row.configuration["risk"]),
    )


def save_risk_configuration(
    session: Session,
    configuration: RiskConfiguration,
    *,
    actor: str,
    expected_version: int,
) -> int:
    if not isinstance(configuration, RiskConfiguration):
        raise ValueError("configuration must be validated risk settings")
    row = session.execute(
        select(ApplicationConfiguration.version, ApplicationConfiguration.configuration).where(
            ApplicationConfiguration.key == "runtime"
        )
    ).one_or_none()
    current_version = row.version if row is not None else 0
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer")
    if current_version != expected_version:
        raise ConfigurationConflict("configuration version changed; reload before updating")
    runtime = dict(row.configuration) if row is not None else {}
    runtime["risk"] = configuration.model_dump(mode="json")
    return save_application_configuration(session, runtime, actor, expected_version)
