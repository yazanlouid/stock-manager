from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, MetaData, Numeric, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

# Predictable constraint names, so Alembic migrations can refer to them by name.
# column_0_N_name joins every column, so composite constraints on the same first
# column (business_id) still get distinct names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for the ORM models in app.models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Column types for plain Mapped[...] annotations: text rather than varchar,
    # timestamptz (hard rule 8), and numeric(14,3) for quantities and money (hard rule 4).
    type_annotation_map = {
        str: Text(),
        datetime: DateTime(timezone=True),
        Decimal: Numeric(14, 3),
    }


engine = create_engine(
    get_settings().database_url,
    # Check a pooled connection still works before handing it out.
    pool_pre_ping=True,
    # Fail fast (in seconds) when Postgres is down, so /health answers quickly.
    connect_args={"connect_timeout": 5},
)

SessionLocal = sessionmaker(bind=engine)


def commit_or_rollback(session: Session) -> Iterator[Session]:
    """Yield the session, then commit it, or roll it back if anything raised.

    An exception from the endpoint (or from another dependency) is thrown into
    this generator at the yield, so it rolls back and re-raises. A commit that
    raises is rolled back and re-raised too. get_db uses this, and so does the
    tests' override of get_db.
    """
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session and one transaction per request (D42).

    The API commits here and nowhere else: endpoints and domain functions never
    commit (hard rule 3). The commit runs when the endpoint returns, and any
    exception rolls the request back. Endpoints depend on it through DbSession
    (app/api/deps.py), with scope="function", so the commit runs after the
    response is serialized but before it is sent: a failed commit is a 500,
    never a success response for work that wasn't saved.
    """
    with SessionLocal() as session:
        yield from commit_or_rollback(session)
