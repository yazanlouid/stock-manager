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


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    It never commits. Committing is the caller's job (CLAUDE.md hard rule 3);
    how endpoints commit is decided in week 1.
    """
    with SessionLocal() as session:
        yield session
