from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (registers the tables on Base.metadata)
from app.config import get_settings
from app.db import Base

config = context.config

if config.config_file_name is not None:
    # Keep loggers that already exist (pytest's, for example) working.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate compares the database against the models registered on Base.
target_metadata = Base.metadata


def database_url() -> str:
    """The test setup passes the stock_test URL in; otherwise use DATABASE_URL."""
    return config.attributes.get("database_url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit the migration SQL as a script instead of running it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the database and run the migrations in a transaction."""
    engine = create_engine(
        database_url(), poolclass=pool.NullPool, connect_args={"connect_timeout": 5}
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
