"""POST /counts: worked examples through the API (U3).

The cat fixture's item is whole milk, which tracks batches and expiry, bought
by the 1000 ml bottle; flour (conftest.py) doesn't track batches. Receipts
default to 07:30 UTC on 2 Nov and counts to 20:00 UTC that day (builders.py),
and the API's clock is pinned to noon UTC on 10 Nov.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.reconcile import reconcile
from app.models import Batch, Item, ItemUnit, StockCount, StockMovement
from tests.builders import (
    Catalogue,
    api_headers,
    count_item_json,
    count_json,
    receipt_json,
    receipt_line_json,
    usage_json,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def pinned_now(client: TestClient, pin_now: Callable[[datetime], None]) -> None:
    pin_now(NOW)


# --- Helpers --------------------------------------------------------------------


def post(client: TestClient, path: str, body: dict[str, Any], cat: Catalogue) -> Any:
    return client.post(path, json=body, headers=api_headers(cat))


def receive(
    client: TestClient, cat: Catalogue, *lines: dict[str, Any], **overrides: Any
) -> list[str]:
    """Post a receipt; return the batch each line went into."""
    response = post(client, "/receipts", receipt_json(cat, list(lines), **overrides), cat)
    assert response.status_code == 201, response.json()
    return [m["batch_id"] for m in response.json()["movements"] if m["type"] == "receive"]


def milk_line(cat: Catalogue, packs: str, expires_day: int) -> dict[str, Any]:
    return receipt_line_json(cat.item, cat.unit, packs, "0.850", date(2026, 11, expires_day))


def default_batch_id(db: Session, cat: Catalogue, item: Item) -> str | None:
    found = db.scalar(
        select(Batch.id).where(
            Batch.location_id == cat.location.id, Batch.item_id == item.id, Batch.is_default
        )
    )
    return None if found is None else str(found)


def rows(db: Session, cat: Catalogue) -> tuple[int, int, int]:
    """The business's movements, counts and batches, to show a refusal left nothing."""
    return tuple(
        db.scalar(
            select(func.count()).select_from(model).where(model.business_id == cat.business.id)
        )
        for model in (StockMovement, StockCount, Batch)
    )


# --- A count --------------------------------------------------------------------


def test_a_count_returns_its_lines_and_movements(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    item, sack = flour
    item.sku, cat.item.sku = "FLOUR", "MILK"
    on_12, on_15, _ = receive(
        client,
        cat,
        milk_line(cat, "1", 12),
        milk_line(cat, "1", 15),
        receipt_line_json(item, sack, "1", "12.500"),
    )
    milk_default = default_batch_id(db_session, cat, cat.item)
    flour_default = default_batch_id(db_session, cat, item)
    body = count_json(
        cat, [count_item_json(cat.item, batches=[(on_12, "400")]), count_item_json(item, "24000")]
    )

    response = post(client, "/counts", body, cat)

    assert response.status_code == 201, response.json()
    result = response.json()
    assert result["replayed"] is False
    count = result["count"]
    assert count["location_id"] == str(cat.location.id)
    assert count["counted_at"] == "2026-11-02T20:00:00Z"
    assert count["created_at"].endswith("Z")
    # By SKU, then FEFO with the default batch last.
    assert [
        (x["item_id"], x["batch_id"], x["expected_qty"], x["counted_qty"]) for x in result["lines"]
    ] == [
        (str(item.id), flour_default, "25000.000", "24000.000"),
        (str(cat.item.id), on_12, "1000.000", "400.000"),
        (str(cat.item.id), on_15, "1000.000", "0.000"),
        (str(cat.item.id), milk_default, "0.000", "0.000"),
    ]
    line_ids = [x["id"] for x in result["lines"]]
    assert [
        (m["type"], m["batch_id"], m["qty_delta"], m["unit_cost"], m["stock_count_line_id"])
        for m in result["movements"]
    ] == [
        ("count_adjust", flour_default, "-1000.000", "0.000500", line_ids[0]),
        ("count_adjust", on_12, "-600.000", "0.000850", line_ids[1]),
        ("count_adjust", on_15, "-1000.000", "0.000850", line_ids[2]),
    ]
    assert [m["client_seq"] for m in result["movements"]] == [0, 1, 2]
    assert all(m["source"] == "count" for m in result["movements"])
    assert all(m["occurred_at"] == "2026-11-02T20:00:00Z" for m in result["movements"])
    assert all(m["client_id"] == body["client_id"] for m in result["movements"])
    assert reconcile(db_session, cat.business.id) == []


def test_a_count_sent_twice_replays_byte_for_byte(
    client: TestClient, db_session: Session, cat: Catalogue
):
    (on_12,) = receive(client, cat, milk_line(cat, "1", 12))
    body = count_json(
        cat,
        [count_item_json(cat.item, batches=[(on_12, "250.5")])],
        counted_at="2026-11-02T23:00:00+03:00",
    )

    first = post(client, "/counts", body, cat)
    before = rows(db_session, cat)
    again = post(client, "/counts", body, cat)

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["count"]["counted_at"] == "2026-11-02T20:00:00Z"
    # The bodies differ only in replayed.
    assert again.content == first.content.replace(b'"replayed":false', b'"replayed":true')
    assert rows(db_session, cat) == before


def test_a_count_that_changed_nothing_replays(client: TestClient, cat: Catalogue):
    body = count_json(cat, [count_item_json(cat.item, batches=[])])

    first = post(client, "/counts", body, cat)
    again = post(client, "/counts", body, cat)

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["movements"] == [] and len(first.json()["lines"]) == 1
    assert again.content == first.content.replace(b'"replayed":false', b'"replayed":true')


# --- Bad requests ---------------------------------------------------------------


def test_counted_at_10_minutes_ahead_is_a_400(client: TestClient, cat: Catalogue):
    body = count_json(
        cat, [count_item_json(cat.item, batches=[])], counted_at=NOW + timedelta(minutes=10)
    )

    response = post(client, "/counts", body, cat)

    assert response.status_code == 400
    assert response.json() == {
        "detail": "counted_at is more than 5 minutes ahead of the server clock"
    }


def test_counted_at_without_a_timezone_is_a_422(client: TestClient, cat: Catalogue):
    body = count_json(
        cat, [count_item_json(cat.item, batches=[])], counted_at="2026-11-02T20:00:00"
    )

    response = post(client, "/counts", body, cat)

    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["detail"]] == [["body", "counted_at"]]


@pytest.mark.parametrize(
    ("extra", "loc"),
    [
        ({"source": "simulator"}, ["body", "source"]),
        (
            {"items": [{"item_id": "x", "counted_qty": "1", "unit": "kg"}]},
            ["body", "items", 0, "unit"],
        ),
    ],
    ids=["count", "item"],
)
def test_an_unknown_field_is_a_422(
    client: TestClient, cat: Catalogue, extra: dict[str, Any], loc: list[Any]
):
    body = count_json(cat, [count_item_json(cat.item, batches=[])]) | extra
    if "items" in extra:
        body["items"][0]["item_id"] = str(cat.item.id)

    response = post(client, "/counts", body, cat)

    assert response.status_code == 422
    assert [(e["type"], e["loc"]) for e in response.json()["detail"]] == [("extra_forbidden", loc)]


def test_counted_qty_for_a_batch_tracked_item_is_the_domain_s_400(
    client: TestClient, cat: Catalogue
):
    response = post(client, "/counts", count_json(cat, [count_item_json(cat.item, "5")]), cat)

    assert response.status_code == 400
    assert "tracks batches" in response.json()["detail"]


def cross_tenant_cases(
    client: TestClient, cat: Catalogue, other: Catalogue
) -> list[tuple[dict[str, Any], str]]:
    """Counts for cat that name one of other's rows, and the error each gives."""
    (other_batch,) = receive(client, other, milk_line(other, "1", 12))
    return [
        (
            count_json(cat, [count_item_json(cat.item, batches=[])], location_id=other.location.id),
            f"location {other.location.id} not found",
        ),
        (
            count_json(cat, [count_item_json(other.item, batches=[])]),
            f"item {other.item.id} not found",
        ),
        (
            count_json(cat, [count_item_json(cat.item, batches=[(other_batch, "1")])]),
            f"batch {other_batch} not found",
        ),
    ]


@pytest.mark.parametrize("case", range(3), ids=["location", "item", "batch"])
def test_another_business_s_rows_are_a_400_that_leaves_nothing(
    client: TestClient, db_session: Session, cat: Catalogue, other: Catalogue, case: int
):
    body, detail = cross_tenant_cases(client, cat, other)[case]
    before = rows(db_session, cat)

    response = post(client, "/counts", body, cat)

    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    # No count, no movement, and not even the milk's default batch.
    assert rows(db_session, cat) == before
    assert default_batch_id(db_session, cat, cat.item) is None


def test_a_count_refused_after_its_locks_leaves_no_default_batch_behind(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    item, _ = flour
    # Milk moved on 3 Nov, after the count's 2 Nov time, so the barrier refuses
    # the count only after it has created flour's default batch.
    receive(client, cat, milk_line(cat, "1", 12), received_at=datetime(2026, 11, 3, tzinfo=UTC))
    before = rows(db_session, cat)
    body = count_json(cat, [count_item_json(item, "0"), count_item_json(cat.item, batches=[])])

    response = post(client, "/counts", body, cat)

    assert response.status_code == 400
    assert "has a movement at 2026-11-03T00:00:00Z" in response.json()["detail"]
    assert rows(db_session, cat) == before
    assert default_batch_id(db_session, cat, item) is None


def test_after_a_count_a_usage_dated_before_it_is_a_400(client: TestClient, cat: Catalogue):
    receive(client, cat, milk_line(cat, "1", 12))
    assert (
        post(
            client, "/counts", count_json(cat, [count_item_json(cat.item, batches=[])]), cat
        ).status_code
        == 201
    )

    response = post(
        client, "/usage", usage_json(cat, "100", occurred_at="2026-11-02T19:00:00Z"), cat
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": f"item {cat.item.sku} was counted at 2026-11-02T20:00:00Z at this location,"
        " so nothing for it there can be dated at or before then (got 2026-11-02T19:00:00Z)"
    }


def test_a_count_s_client_id_is_refused_for_a_usage(client: TestClient, cat: Catalogue):
    body = count_json(cat, [count_item_json(cat.item, batches=[])])
    post(client, "/counts", body, cat)

    response = post(
        client, "/usage", usage_json(cat, "1", client_id=body["client_id"], occurred_at=NOW), cat
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": f"client_id {body['client_id']} was already used for a different request"
    }


def test_counts_need_the_business_header(client: TestClient, cat: Catalogue):
    body = count_json(cat, [count_item_json(cat.item, batches=[])])

    response = client.post("/counts", json=body, headers={"X-Business-Id": str(uuid.uuid4())})

    assert response.status_code == 404
