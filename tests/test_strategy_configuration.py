from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from futures_bot.persistence.database import create_schema_for_tests, make_engine
from futures_bot.persistence.models import ApplicationConfiguration, AuditEvent
from futures_bot.persistence.models import StrategyConfiguration as StrategyRecord
from futures_bot.persistence.repositories import (
    ConfigurationConflict,
    save_application_configuration,
)
from futures_bot.strategy import StrategyConfiguration
from futures_bot.strategy.configuration import (
    MissingStrategyConfiguration,
    load_strategy_configuration,
    save_strategy_configuration,
)


def test_strategy_settings_are_versioned_with_immutable_snapshots(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'strategy.sqlite3'}")
    create_schema_for_tests(engine)
    try:
        with Session(engine) as session:
            with pytest.raises(MissingStrategyConfiguration):
                load_strategy_configuration(session)
            save_application_configuration(
                session, {"mode": "paper", "risk": {"test": True}}, "op", 0
            )
            original = StrategyConfiguration()
            assert (
                save_strategy_configuration(session, original, actor="op", expected_version=1) == 2
            )
            session.commit()
            updated = StrategyConfiguration(reward_multiple=Decimal("3"))
            assert (
                save_strategy_configuration(session, updated, actor="op", expected_version=2) == 3
            )
            session.commit()
            loaded = load_strategy_configuration(session)
            assert loaded.version == 3
            assert loaded.configuration == updated
            runtime = session.get(ApplicationConfiguration, "runtime")
            assert runtime is not None
            assert runtime.configuration["risk"] == {"test": True}
            records = session.query(StrategyRecord).all()
            assert len(records) == 2
            assert {record.version for record in records} == {original.checksum, updated.checksum}
            with pytest.raises(ConfigurationConflict):
                save_strategy_configuration(session, original, actor="stale", expected_version=2)
            session.rollback()
            assert load_strategy_configuration(session).version == 3
            assert (
                save_strategy_configuration(session, original, actor="op", expected_version=3) == 4
            )
            session.commit()
            assert session.query(StrategyRecord).count() == 2
    finally:
        engine.dispose()


def test_strategy_settings_snapshots_and_audit_roll_back_together(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'strategy.sqlite3'}")
    create_schema_for_tests(engine)
    try:
        with Session(engine) as session:
            save_strategy_configuration(
                session, StrategyConfiguration(), actor="op", expected_version=0
            )
            session.rollback()
            with pytest.raises(MissingStrategyConfiguration):
                load_strategy_configuration(session)
            assert session.query(StrategyRecord).count() == 0
            assert session.query(AuditEvent).count() == 0
    finally:
        engine.dispose()
