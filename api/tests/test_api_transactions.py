"""The request transaction (D42), the business header (D43) and the error mapping.

The client fixture's get_db runs the app's commit_or_rollback on db_session,
after committing the test's setup rows, so a request's commit releases a
savepoint and its rollback undoes only what the request wrote.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.api.stock as stock_api
import app.domain.stock as stock_domain
from app.db import commit_or_rollback, get_db
from app.main import app
from app.models import Batch, StockMovement
from tests.builders import (
    EXPIRES_ON,
    Catalogue,
    add,
    api_headers,
    new_item,
    receipt_json,
    receipt_line_json,
    reversal_json,
    usage_json,
    waste_json,
)


def movement_count(session: Session, client_id: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.client_id == uuid.UUID(client_id))
    )


# --- The request transaction ----------------------------------------------------


def test_a_successful_write_is_committed(client: TestClient, db_session: Session, cat: Catalogue):
    body = usage_json(cat, "500")

    response = client.post("/usage", json=body, headers=api_headers(cat))

    assert response.status_code == 201
    # The request committed, so rolling back the session now keeps its movements.
    db_session.rollback()
    assert movement_count(db_session, body["client_id"]) == 1


def test_a_refused_write_leaves_nothing(client: TestClient, db_session: Session, cat: Catalogue):
    # An item without batch tracking and no default batch yet. waste() creates
    # the default batch, then refuses because it holds nothing.
    flour = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    body = waste_json(cat, "100", None, item_id=str(flour.id))

    response = client.post("/waste", json=body, headers=api_headers(cat))

    assert response.status_code == 400
    assert response.json() == {"detail": "can't waste 100: the batch has 0.000 on hand"}
    assert movement_count(db_session, body["client_id"]) == 0
    assert db_session.scalars(select(Batch).where(Batch.item_id == flour.id)).all() == []


def test_a_failing_commit_is_a_500_and_saves_nothing(
    db_session: Session, cat: Catalogue, monkeypatch: pytest.MonkeyPatch
):
    def commit_fails() -> None:
        raise RuntimeError("commit failed")

    def failing_get_db() -> Iterator[Session]:
        db_session.commit()  # Keep the setup rows, as the client fixture does.
        monkeypatch.setattr(db_session, "commit", commit_fails)
        yield from commit_or_rollback(db_session)

    body = usage_json(cat, "500")
    app.dependency_overrides[get_db] = failing_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.post("/usage", json=body, headers=api_headers(cat))
    finally:
        app.dependency_overrides.clear()

    # With function scope the commit runs before the response is sent, so the
    # client never sees a 201 for work that wasn't saved.
    assert response.status_code == 500
    assert movement_count(db_session, body["client_id"]) == 0
    assert db_session.scalars(select(Batch).where(Batch.item_id == cat.item.id)).all() == []


# --- The business header --------------------------------------------------------


def write_bodies(cat: Catalogue) -> dict[str, dict[str, Any]]:
    """A valid body for each write endpoint."""
    return {
        "/receipts": receipt_json(
            cat, [receipt_line_json(cat.item, cat.unit, "1", "0.850", EXPIRES_ON)]
        ),
        "/usage": usage_json(cat, "100"),
        "/waste": waste_json(cat, "100", uuid.uuid4()),
        "/reversals": reversal_json(uuid.uuid4()),
    }


ENDPOINTS = [
    ("POST", "/receipts"),
    ("POST", "/usage"),
    ("POST", "/waste"),
    ("POST", "/reversals"),
    ("GET", "/stock"),
    ("GET", "/catalogue"),
]


def send(
    client: TestClient, cat: Catalogue, method: str, path: str, headers: dict[str, str]
) -> Any:
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=write_bodies(cat)[path], headers=headers)


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
@pytest.mark.parametrize("headers", [{}, {"X-Business-Id": "not-a-uuid"}], ids=["missing", "bad"])
def test_a_missing_or_malformed_business_header_is_a_422(
    client: TestClient, cat: Catalogue, method: str, path: str, headers: dict[str, str]
):
    response = send(client, cat, method, path, headers)

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [["header", "x-business-id"]]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
def test_an_unknown_business_is_a_404(client: TestClient, cat: Catalogue, method: str, path: str):
    unknown = uuid.uuid4()

    response = send(client, cat, method, path, {"X-Business-Id": str(unknown)})

    assert response.status_code == 404
    assert response.json() == {"detail": f"business {unknown} not found"}


def test_health_needs_no_business_header(client: TestClient):
    assert client.get("/health").status_code == 200


# --- Errors from the database ---------------------------------------------------


def test_a_unique_violation_is_a_409(
    client: TestClient, db_session: Session, cat: Catalogue, monkeypatch: pytest.MonkeyPatch
):
    body = usage_json(cat, "500")
    assert client.post("/usage", json=body, headers=api_headers(cat)).status_code == 201

    # As if a retry raced the first request: the replay check finds nothing,
    # so the retry writes client_seq 0 again and the unique key refuses it.
    monkeypatch.setattr(stock_domain, "_replay", lambda *args: None)
    response = client.post("/usage", json=body, headers=api_headers(cat))

    assert response.status_code == 409
    assert response.json() == {
        "detail": "another request saved conflicting data at the same moment; retry this request"
    }
    assert movement_count(db_session, body["client_id"]) == 1


def test_any_other_integrity_error_is_a_500(
    error_client: TestClient, cat: Catalogue, monkeypatch: pytest.MonkeyPatch
):
    # Only a bug gets past the domain's checks to a check constraint.
    def use_raises(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("INSERT ...", {}, CheckViolation("a check failed"))

    monkeypatch.setattr(stock_api, "use", use_raises)

    response = error_client.post("/usage", json=usage_json(cat, "1"), headers=api_headers(cat))

    assert response.status_code == 500
