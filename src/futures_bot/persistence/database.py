from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "sqlite":
        if url.database and url.database not in {":memory:"}:
            Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False, "timeout": 15}

    engine = create_engine(
        url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.close()

        if url.database and url.database != ":memory:":

            @event.listens_for(engine, "first_connect")
            def enable_write_ahead_log(
                dbapi_connection: Any,
                _: Any,
            ) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )


def create_schema_for_tests(engine: Engine) -> None:
    Base.metadata.create_all(engine)
