"""Versioned operator configuration and immutable strategy snapshots."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from futures_bot.persistence.models import ApplicationConfiguration
from futures_bot.persistence.models import StrategyConfiguration as StrategyRecord
from futures_bot.persistence.repositories import (
    ConfigurationConflict,
    create_strategy_configuration,
    save_application_configuration,
)
from futures_bot.strategy.pullback import STRATEGY_VERSION, StrategyConfiguration


class MissingStrategyConfiguration(RuntimeError):
    """The strategy must be explicitly initialized before execution."""


@dataclass(frozen=True, slots=True)
class VersionedStrategyConfiguration:
    version: int
    configuration: StrategyConfiguration


def load_strategy_configuration(session: Session) -> VersionedStrategyConfiguration:
    row = session.execute(
        select(ApplicationConfiguration.version, ApplicationConfiguration.configuration).where(
            ApplicationConfiguration.key == "runtime"
        )
    ).one_or_none()
    if row is None or "strategy" not in row.configuration:
        raise MissingStrategyConfiguration("strategy configuration has not been initialized")
    return VersionedStrategyConfiguration(
        version=row.version,
        configuration=StrategyConfiguration.model_validate(row.configuration["strategy"]),
    )


def save_strategy_configuration(
    session: Session,
    configuration: StrategyConfiguration,
    *,
    actor: str,
    expected_version: int,
) -> int:
    if not isinstance(configuration, StrategyConfiguration):
        raise ValueError("configuration must be validated strategy settings")
    if type(expected_version) is not int or expected_version < 0:
        raise ValueError("expected_version must be a non-negative integer")
    row = session.execute(
        select(ApplicationConfiguration.version, ApplicationConfiguration.configuration).where(
            ApplicationConfiguration.key == "runtime"
        )
    ).one_or_none()
    if (row.version if row is not None else 0) != expected_version:
        raise ConfigurationConflict("configuration version changed; reload before updating")
    runtime = dict(row.configuration) if row is not None else {}
    runtime["strategy"] = configuration.model_dump(mode="json")
    version = save_application_configuration(session, runtime, actor, expected_version)
    existing = session.scalar(
        select(StrategyRecord).where(StrategyRecord.version == configuration.checksum)
    )
    if existing is None:
        create_strategy_configuration(
            session,
            configuration.checksum,
            {
                "implementation": STRATEGY_VERSION,
                "parameters": configuration.model_dump(mode="json"),
            },
            actor,
        )
    return version
