from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (registers the tables on Base.metadata)
from app.db import Base


def test_models_match_migrations(db_session: Session) -> None:
    """The migrated stock_test schema has no differences from the ORM models.

    Fails when a model changes without a migration, or a migration without the model.
    """
    context = MigrationContext.configure(
        db_session.connection(),
        opts={"compare_type": True, "compare_server_default": True},
    )
    assert compare_metadata(context, Base.metadata) == []
