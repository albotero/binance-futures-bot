import hashlib
import json
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import (
    ApplicationConfiguration,
    AuditEvent,
    BacktestRun,
    StrategyConfiguration,
    utc_now,
)

_SENSITIVE_KEY_FRAGMENTS = (
    "apikey",
    "secret",
    "password",
    "token",
    "signature",
    "privatekey",
)


class ConfigurationConflict(Exception):
    """Raised when a configuration update is stale or races another writer."""


def save_application_configuration(
    session: Session,
    configuration: dict[str, Any],
    actor: str,
    expected_version: int,
) -> int:
    snapshot, _ = _snapshot_mapping(configuration, "configuration")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must not be empty")
    if not isinstance(expected_version, int) or isinstance(expected_version, bool):
        raise ValueError("expected_version must be an integer")
    if expected_version < 0:
        raise ValueError("expected_version must be non-negative")
    actor = actor.strip()

    updated_at = utc_now()
    if expected_version == 0:
        current = session.get(ApplicationConfiguration, "runtime")
        if current is not None:
            raise ConfigurationConflict("configuration version changed; reload before updating")
        next_version = 1
        session.add(
            ApplicationConfiguration(
                key="runtime",
                version=next_version,
                configuration=snapshot,
                updated_by=actor,
                updated_at=updated_at,
            )
        )
    else:
        next_version = expected_version + 1
        result = session.execute(
            update(ApplicationConfiguration)
            .where(
                ApplicationConfiguration.key == "runtime",
                ApplicationConfiguration.version == expected_version,
            )
            .values(
                version=next_version,
                configuration=snapshot,
                updated_by=actor,
                updated_at=updated_at,
            )
            .returning(ApplicationConfiguration.key)
        )
        if result.scalar_one_or_none() is None:
            raise ConfigurationConflict("configuration version changed; reload before updating")

    session.add(
        AuditEvent(
            actor=actor,
            action="configuration.updated",
            resource_type="application_configuration",
            resource_id="runtime",
            details={
                "version": next_version,
                "keys": sorted(snapshot),
            },
            created_at=updated_at,
        )
    )
    session.flush()
    return next_version


def create_strategy_configuration(
    session: Session,
    version: str,
    configuration: dict[str, Any],
    actor: str,
) -> StrategyConfiguration:
    if not isinstance(version, str) or not version.strip():
        raise ValueError("strategy version must not be empty")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must not be empty")
    snapshot, canonical_json = _snapshot_mapping(configuration, "strategy configuration")
    record = StrategyConfiguration(
        version=version.strip(),
        configuration=snapshot,
        checksum=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )
    session.add(record)
    session.add(
        AuditEvent(
            actor=actor.strip(),
            action="strategy_configuration.created",
            resource_type="strategy_configuration",
            resource_id=version.strip(),
            details={"checksum": record.checksum, "keys": sorted(snapshot)},
        )
    )
    session.flush()
    return record


def create_backtest_run(
    session: Session,
    strategy_version: str,
    configuration_snapshot: dict[str, Any],
    dataset_metadata: dict[str, Any],
    actor: str,
) -> BacktestRun:
    if not isinstance(strategy_version, str) or not strategy_version.strip():
        raise ValueError("strategy version must not be empty")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("actor must not be empty")
    configuration, _ = _snapshot_mapping(configuration_snapshot, "backtest configuration")
    dataset, _ = _snapshot_mapping(dataset_metadata, "dataset metadata")
    run = BacktestRun(
        strategy_version=strategy_version.strip(),
        configuration_snapshot=configuration,
        dataset_metadata=dataset,
        status="queued",
    )
    session.add(run)
    session.flush()
    session.add(
        AuditEvent(
            actor=actor.strip(),
            action="backtest_run.created",
            resource_type="backtest_run",
            resource_id=run.id,
            details={"strategy_version": strategy_version.strip()},
        )
    )
    session.flush()
    return run


def _snapshot_mapping(value: Any, label: str) -> tuple[dict[str, Any], str]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a mapping with string keys")
    _reject_secret_fields(value)
    try:
        canonical_json = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON-compatible values") from exc
    snapshot = json.loads(canonical_json)
    if not isinstance(snapshot, dict):
        raise ValueError(f"{label} must be a JSON object")
    return snapshot, canonical_json


def _reject_secret_fields(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = "".join(
                character for character in str(key).lower() if character.isalnum()
            )
            if any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS):
                raise ValueError(f"secret-like configuration field is not persistable: {path}{key}")
            _reject_secret_fields(item, f"{path}{key}.")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}{index}.")
