"""Test database setup.

Tests run against a separate database, stock_test, on the same Postgres as
DATABASE_URL. The session fixture creates it if needed and applies the Alembic
migrations. Each test then runs inside a transaction that is rolled back, so
nothing a test writes is kept and ledger rows are never deleted.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.main import app

API_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "stock_test"
# Fail in seconds, not hang, if Postgres can't be reached.
CONNECT_ARGS = {"connect_timeout": 5}


def stock_test_url() -> URL:
    """DATABASE_URL with the database name swapped for stock_test."""
    return make_url(get_settings().database_url).set(database=TEST_DB_NAME)


def create_test_database(url: URL) -> None:
    """Create stock_test if it doesn't exist yet.

    CREATE DATABASE can't run inside a transaction, so this connects to the
    built-in postgres database in autocommit mode.
    """
    admin = create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT", connect_args=CONNECT_ARGS
    )
    try:
        with admin.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            )
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        admin.dispose()


def run_migrations(url: URL) -> None:
    """Bring stock_test up to the latest Alembic revision."""
    config = Config(str(API_DIR / "alembic.ini"))
    # Read by migrations/env.py in place of DATABASE_URL.
    config.attributes["database_url"] = url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = stock_test_url()
    create_test_database(url)
    run_migrations(url)
    engine = create_engine(url, connect_args=CONNECT_ARGS)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session inside a transaction that is rolled back after the test.

    With join_transaction_mode="create_savepoint", a session.commit() in code
    under test only releases a savepoint; the outer transaction still rolls back.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient whose requests use the test's rolled-back session."""
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
