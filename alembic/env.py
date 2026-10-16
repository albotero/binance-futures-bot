from logging.config import fileConfig
from typing import Any, Literal

from alembic.autogenerate.api import AutogenContext

from alembic import context
from futures_bot.config import Settings
from futures_bot.execution import paper  # noqa: F401
from futures_bot.persistence.database import make_engine
from futures_bot.persistence.models import Base, UTCDateTime
from futures_bot.web import models as web_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings()
database_url = settings.database_url
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = Base.metadata


def render_item(
    type_: str,
    obj: Any,
    _autogen_context: AutogenContext,
) -> str | Literal[False]:
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = make_engine(database_url)
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                render_item=render_item,
                render_as_batch=connection.dialect.name == "sqlite",
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
