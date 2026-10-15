from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from futures_bot.persistence.database import create_schema_for_tests, make_engine
from futures_bot.persistence.models import (
    AccountSnapshot,
    ApplicationConfiguration,
    AuditEvent,
    BacktestRun,
    Fill,
    OrderRecord,
    StrategyConfiguration,
    UTCDateTime,
)
from futures_bot.persistence.repositories import (
    ConfigurationConflict,
    create_backtest_run,
    create_strategy_configuration,
    save_application_configuration,
)


def test_sqlite_schema_and_pragmas_are_initialized(tmp_path) -> None:
    database_path = tmp_path / "platform.sqlite3"
    engine = make_engine(f"sqlite:///{database_path}")

    create_schema_for_tests(engine)

    with engine.connect() as connection:
        tables = set(
            connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).scalars()
        )
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert "account_snapshots" in tables
    assert "orders" in tables
    assert "backtest_runs" in tables
    assert journal_mode == "wal"
    assert foreign_keys == 1
    engine.dispose()


def test_persisted_timestamp_round_trips_as_aware_utc(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)
    captured_at = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add(
            AccountSnapshot(
                environment="simulated",
                wallet_balance=Decimal("1000.25"),
                available_balance=Decimal("950.25"),
                account_equity=Decimal("1010.25"),
                unrealized_pnl=Decimal("10.00"),
                realized_pnl=Decimal("2.50"),
                used_initial_margin=Decimal("50.00"),
                open_exposure=Decimal("250.00"),
                captured_at=captured_at,
            )
        )
        session.commit()
        saved = session.query(AccountSnapshot).one()

    assert saved.captured_at == captured_at
    assert saved.captured_at.tzinfo == UTC
    assert saved.account_equity == Decimal("1010.250000000000")
    engine.dispose()


def test_fill_requires_an_existing_order(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)

    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(
            Fill(
                order_id="missing-order",
                symbol="BTCUSDT",
                exchange_trade_id="trade-1",
                quantity=Decimal("0.001"),
                price=Decimal("50000"),
                commission=Decimal("0.02"),
                commission_asset="USDT",
                filled_at=datetime.now(UTC),
            )
        )
        session.commit()

    engine.dispose()


def test_utc_datetime_rejects_naive_values() -> None:
    converter = UTCDateTime()

    with pytest.raises(ValueError, match="timezone-aware"):
        converter.process_bind_param(datetime(2026, 10, 15), dialect())


def test_client_order_ids_are_unique(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)
    first = OrderRecord(
        environment="testnet",
        symbol="BTCUSDT",
        client_order_id="strategy-entry-1",
        side="buy",
        order_type="limit",
        status="new",
        quantity=Decimal("0.01"),
        limit_price=Decimal("50000"),
    )

    with Session(engine) as session:
        session.add(first)
        session.commit()
        session.add(
            OrderRecord(
                environment="testnet",
                symbol="BTCUSDT",
                client_order_id="strategy-entry-1",
                side="buy",
                order_type="limit",
                status="new",
                quantity=Decimal("0.01"),
                limit_price=Decimal("50000"),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    engine.dispose()


def test_configuration_updates_are_versioned_and_audited_atomically(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)
    with Session(engine) as session:
        version = save_application_configuration(
            session,
            {"mode": "paper", "symbols": ["BTCUSDT"]},
            actor="operator",
            expected_version=0,
        )
        session.commit()
        assert version == 1

    with Session(engine) as session:
        version = save_application_configuration(
            session,
            {"mode": "paper", "symbols": ["ETHUSDT"]},
            actor="operator",
            expected_version=1,
        )
        session.commit()
        assert version == 2
        saved_configuration = session.get(ApplicationConfiguration, "runtime")
        assert saved_configuration is not None
        assert saved_configuration.configuration == {"mode": "paper", "symbols": ["ETHUSDT"]}
        audit_events = session.query(AuditEvent).all()

    assert [event.details["version"] for event in audit_events] == [1, 2]
    assert all(event.details["keys"] == ["mode", "symbols"] for event in audit_events)
    engine.dispose()


def test_stale_configuration_and_secret_fields_are_rejected(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)
    with Session(engine) as session:
        save_application_configuration(
            session,
            {"mode": "paper"},
            actor="operator",
            expected_version=0,
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(ConfigurationConflict, match="version changed"):
            save_application_configuration(
                session,
                {"mode": "paper"},
                actor="operator",
                expected_version=0,
            )
        with pytest.raises(ValueError, match="secret-like"):
            save_application_configuration(
                session,
                {"exchange": {"api_secret": "must-not-persist"}},
                actor="operator",
                expected_version=1,
            )
        assert session.query(AuditEvent).count() == 1

    engine.dispose()


def test_strategy_and_backtest_configuration_snapshots_are_immutable(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)
    strategy_settings = {"ema_fast": 20, "symbols": ["BTCUSDT"]}
    run_settings = {"risk_pct": 0.5, "symbols": ["BTCUSDT"]}
    dataset_metadata = {"source": "fixture", "candles": 300}

    with Session(engine) as session:
        strategy = create_strategy_configuration(
            session,
            version="trend-pullback-v1",
            configuration=strategy_settings,
            actor="operator",
        )
        run = create_backtest_run(
            session,
            strategy_version=strategy.version,
            configuration_snapshot=run_settings,
            dataset_metadata=dataset_metadata,
            actor="operator",
        )
        strategy_settings["ema_fast"] = 99
        run_settings["risk_pct"] = 9.0
        dataset_metadata["candles"] = 0
        strategy_checksum = strategy.checksum
        run_id = run.id
        session.commit()

    with Session(engine) as session:
        saved_strategy = session.query(StrategyConfiguration).one()
        saved_run = session.get(BacktestRun, run_id)
        audit_events = session.query(AuditEvent).all()

    assert saved_strategy.configuration == {"ema_fast": 20, "symbols": ["BTCUSDT"]}
    assert len(strategy_checksum) == 64
    assert saved_run is not None
    assert saved_run.configuration_snapshot == {"risk_pct": 0.5, "symbols": ["BTCUSDT"]}
    assert saved_run.dataset_metadata == {"source": "fixture", "candles": 300}
    assert any(event.resource_id == run_id for event in audit_events)
    engine.dispose()


def test_configuration_and_audit_changes_roll_back_together(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)

    with Session(engine) as session:
        save_application_configuration(
            session,
            {"mode": "paper"},
            actor="operator",
            expected_version=0,
        )
        session.rollback()

    with Session(engine) as session:
        assert session.get(ApplicationConfiguration, "runtime") is None
        assert session.query(AuditEvent).count() == 0

    engine.dispose()


def test_concurrent_configuration_update_rejects_the_stale_writer(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    create_schema_for_tests(engine)
    with Session(engine) as session:
        save_application_configuration(
            session,
            {"mode": "paper"},
            actor="operator",
            expected_version=0,
        )
        session.commit()

    with Session(engine) as first_writer, Session(engine) as stale_writer:
        first_snapshot = first_writer.get(ApplicationConfiguration, "runtime")
        stale_snapshot = stale_writer.get(ApplicationConfiguration, "runtime")
        assert first_snapshot is not None and stale_snapshot is not None
        assert first_snapshot.version == stale_snapshot.version == 1

        save_application_configuration(
            first_writer,
            {"mode": "paper", "symbols": ["BTCUSDT"]},
            actor="first-writer",
            expected_version=first_snapshot.version,
        )
        first_writer.commit()

        with pytest.raises(ConfigurationConflict, match="version changed"):
            save_application_configuration(
                stale_writer,
                {"mode": "paper", "symbols": ["ETHUSDT"]},
                actor="stale-writer",
                expected_version=stale_snapshot.version,
            )
        stale_writer.rollback()

    with Session(engine) as session:
        saved_configuration = session.get(ApplicationConfiguration, "runtime")
        assert saved_configuration is not None
        assert saved_configuration.configuration["symbols"] == ["BTCUSDT"]
        assert session.query(AuditEvent).count() == 2

    engine.dispose()
