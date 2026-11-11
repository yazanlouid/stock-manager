"""Dependencies shared by every endpoint: the session, the business and the clock."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Business

# The request's session (D42). scope="function" makes get_db commit as soon as
# the endpoint's response is serialized, before it is sent. Every endpoint and
# dependency uses this one alias, so FastAPI hands them all the same session.
DbSession = Annotated[Session, Depends(get_db, scope="function")]


def current_business(
    db: DbSession,
    x_business_id: Annotated[UUID, Header(description="The business the request acts for.")],
) -> Business:
    """The business named by the X-Business-Id header (D43).

    A missing or malformed header fails FastAPI's validation (422); an unknown
    business is a 404. Endpoints take business_id only from here, never from a
    body or a path. When demo login lands, this reads the token instead.
    """
    business = db.scalar(select(Business).where(Business.id == x_business_id))
    if business is None:
        raise HTTPException(status_code=404, detail=f"business {x_business_id} not found")
    return business


CurrentBusiness = Annotated[Business, Depends(current_business)]


def now() -> datetime:
    """The server clock, as a dependency so tests can pin it."""
    return datetime.now(UTC)


Now = Annotated[datetime, Depends(now)]
