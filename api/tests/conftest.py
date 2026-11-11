"""Test database setup.

Tests run against a separate database, stock_test, on the same Postgres as
DATABASE_URL. The session fixture creates it if needed and applies the Alembic
migrations. Each test then runs inside a transaction that is rolled back, so
nothing a test writes is kept and ledger rows are never deleted.
"""

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.api.deps import now
from app.config import get_settings
from app.db import commit_or_rollback, get_db
from app.main import app
from tests.builders import Catalogue, Stock, build_catalogue, build_stock

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


def override_get_db(session: Session) -> Callable[[], Iterator[Session]]:
    """A get_db for the tests: the app's commit-or-rollback, on the test's session.

    Each request first commits the test's setup rows (the cat and stock
    fixtures, rows a test added). In create_savepoint mode a commit only
    releases a savepoint, so the outer transaction still rolls back after the
    test, but without it a request that rolls back would undo the setup too.
    """

    def get_test_db() -> Iterator[Session]:
        session.commit()
        yield from commit_or_rollback(session)

    return get_test_db


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient whose requests commit or roll back like the app's, on db_session."""
    app.dependency_overrides[get_db] = override_get_db(db_session)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def error_client(db_session: Session) -> Iterator[TestClient]:
    """Like client, but an unhandled error is a 500 response instead of an exception."""
    app.dependency_overrides[get_db] = override_get_db(db_session)
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def pin_now() -> Callable[[datetime], None]:
    """Pin the API's clock (the Now dependency) to a given time.

    The client fixture clears the override after the test.
    """

    def pin(moment: datetime) -> None:
        app.dependency_overrides[now] = lambda: moment

    return pin


@pytest.fixture
def cat(db_session: Session) -> Catalogue:
    """One business with one row in every tenancy and catalogue table."""
    return build_catalogue(db_session)


@pytest.fixture
def other(db_session: Session) -> Catalogue:
    """A second business, for cross-tenant tests."""
    return build_catalogue(db_session)


@pytest.fixture
def stock(db_session: Session, cat: Catalogue) -> Stock:
    """Batches, a receipt, a movement and a balance for cat's item."""
    return build_stock(db_session, cat)


@pytest.fixture
def other_stock(db_session: Session, other: Catalogue) -> Stock:
    """The same stock rows in the second business."""
    return build_stock(db_session, other)
