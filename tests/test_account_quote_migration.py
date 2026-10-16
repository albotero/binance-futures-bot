from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command


def test_account_quote_migration_preserves_unclassified_history_and_schema(
    tmp_path, monkeypatch
) -> None:
    url = f"sqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setenv("BOT_DATABASE_URL", url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, "20261015_0003")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO account_snapshots "
                "(id, environment, wallet_balance, available_balance, account_equity, "
                "unrealized_pnl, realized_pnl, used_initial_margin, open_exposure, captured_at) "
                "VALUES ('legacy', 'simulated', 100, 100, 100, 0, 0, 0, 0, '2026-10-15 12:00:00')"
            )
        command.upgrade(configuration, "head")
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT quote_asset FROM account_snapshots WHERE id='legacy'"
                ).scalar_one()
                == "UNKNOWN"
            )
        command.check(configuration)
        command.downgrade(configuration, "20261015_0003")
        command.upgrade(configuration, "head")
        command.check(configuration)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT count(*) FROM account_snapshots WHERE id='legacy'"
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
