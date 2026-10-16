"""Evaluate entries against persisted operator settings and account state."""

from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.orm import Session

from futures_bot.risk.accounts import AccountEnvironment, load_latest_account_state
from futures_bot.risk.configuration import load_risk_configuration
from futures_bot.risk.engine import PortfolioState, RiskDecision, RiskRequest, size_position


@dataclass(frozen=True, slots=True)
class ConfiguredRiskDecision:
    configuration_version: int
    account_snapshot_id: str
    leverage: int
    decision: RiskDecision


def evaluate_configured_entry(
    session: Session,
    request: RiskRequest,
    portfolio: PortfolioState,
    *,
    environment: AccountEnvironment,
    now: datetime | None = None,
) -> ConfiguredRiskDecision:
    saved = load_risk_configuration(session)
    settings = saved.configuration
    symbol = next((item for item in settings.symbols if item.symbol == request.symbol), None)
    if symbol is None or not symbol.enabled:
        raise ValueError("entry symbol is not enabled in operator configuration")
    account = load_latest_account_state(session, environment=environment)
    configured_request = replace(request, leverage=symbol.leverage)
    decision = size_position(
        configured_request,
        account.state,
        portfolio,
        settings.policy,
        now=now,
    )
    return ConfiguredRiskDecision(
        configuration_version=saved.version,
        account_snapshot_id=account.snapshot_id,
        leverage=symbol.leverage,
        decision=decision,
    )
