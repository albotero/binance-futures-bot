"""Persistence boundary for normalized, environment-isolated account state."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from futures_bot.persistence.models import AccountSnapshot
from futures_bot.risk.engine import AccountState

AccountEnvironment = Literal["simulated", "testnet", "production"]


class MissingAccountSnapshot(RuntimeError):
    """No account state is available for the requested environment."""


@dataclass(frozen=True, slots=True)
class StoredAccountState:
    snapshot_id: str
    environment: AccountEnvironment
    state: AccountState


def save_account_state(
    session: Session,
    state: AccountState,
    *,
    environment: AccountEnvironment,
) -> str:
    _validate_environment(environment)
    row = AccountSnapshot(
        environment=environment,
        quote_asset=state.quote_asset,
        wallet_balance=state.wallet_balance,
        available_balance=state.available_balance,
        account_equity=state.equity,
        unrealized_pnl=state.unrealized_pnl,
        realized_pnl=state.realized_pnl,
        used_initial_margin=state.used_initial_margin,
        open_exposure=state.open_exposure,
        captured_at=state.captured_at,
    )
    session.add(row)
    session.flush()
    return row.id


def load_latest_account_state(
    session: Session,
    *,
    environment: AccountEnvironment,
    quote_asset: Literal["USDC", "USDT"] = "USDC",
) -> StoredAccountState:
    _validate_environment(environment)
    if quote_asset not in ("USDC", "USDT"):
        raise ValueError("quote asset must be USDC or USDT")
    row = session.scalar(
        select(AccountSnapshot)
        .where(AccountSnapshot.environment == environment)
        .where(AccountSnapshot.quote_asset == quote_asset)
        .order_by(AccountSnapshot.captured_at.desc(), AccountSnapshot.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise MissingAccountSnapshot(f"no {quote_asset} account snapshot exists for {environment}")
    return StoredAccountState(
        snapshot_id=row.id,
        environment=environment,
        state=AccountState(
            wallet_balance=row.wallet_balance,
            available_balance=row.available_balance,
            equity=row.account_equity,
            unrealized_pnl=row.unrealized_pnl,
            realized_pnl=row.realized_pnl,
            used_initial_margin=row.used_initial_margin,
            open_exposure=row.open_exposure,
            captured_at=row.captured_at,
            quote_asset=quote_asset,
        ),
    )


def _validate_environment(environment: str) -> None:
    if environment not in ("simulated", "testnet", "production"):
        raise ValueError("environment must be simulated, testnet, or production")
