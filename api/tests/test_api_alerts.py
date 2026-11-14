"""GET /alerts: worked examples through the API (D62, D65).

The cat fixture's item is whole milk, which tracks batches and expiry, bought
by the 1000 ml bottle; flour (conftest.py) doesn't track batches. The business
is in Asia/Amman (UTC+3). The API's clock is pinned to noon UTC on 10 Nov
unless a test pins another time; reading the alerts sweeps them as of the
clock when the local date has moved on.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.domain.reconcile import reconcile
from app.models import Business, Item, ItemUnit
from tests.builders import (
    Catalogue,
    add,
    api_headers,
    new_alert,
    new_location,
    new_par_level,
    receipt_json,
    receipt_line_json,
    usage_json,
    waste_json,
)


def nov(day: int, hour: int = 12) -> datetime:
    """A time in November 2026, in UTC."""
    return datetime(2026, 11, day, hour, tzinfo=UTC)


NOW = nov(10)


@pytest.fixture(autouse=True)
def pinned_now(client: TestClient, pin_now: Callable[[datetime], None]) -> None:
    pin_now(NOW)


# --- Helpers --------------------------------------------------------------------


def get_alerts(client: TestClient, cat: Catalogue, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/alerts", params=params, headers=api_headers(cat))
    assert response.status_code == 200, response.json()
    return response.json()


def receive_milk(client: TestClient, cat: Catalogue, packs: str, expires_day: int, at: datetime):
    """Post a receipt of milk; return the batch it went into."""
    line = receipt_line_json(cat.item, cat.unit, packs, "0.850", date(2026, 11, expires_day))
    body = receipt_json(cat, [line], received_at=at)
    response = client.post("/receipts", json=body, headers=api_headers(cat))
    assert response.status_code == 201, response.json()
    return response.json()["movements"][0]["batch_id"]


def use_milk(client: TestClient, cat: Catalogue, quantity: str, at: datetime) -> None:
    body = usage_json(cat, quantity, occurred_at=at)
    response = client.post("/usage", json=body, headers=api_headers(cat))
    assert response.status_code == 201, response.json()


def kinds(alerts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(alert["type"], alert["status"]) for alert in alerts]


# --- The list -------------------------------------------------------------------


def test_alerts_carry_their_item_location_and_batch(
    client: TestClient, db_session: Session, cat: Catalogue
):
    add(db_session, new_par_level(cat.location, cat.item, reorder_point=Decimal("2000")))
    batch_id = receive_milk(client, cat, "2", expires_day=12, at=nov(9, 6))

    expiring, low = get_alerts(client, cat)

    # Raised by this read's sweep, as of the pinned clock.
    assert expiring == {
        "id": expiring["id"],
        "type": "expiring",
        "status": "open",
        "raised_at": "2026-11-10T12:00:00Z",
        "resolved_at": None,
        "location_id": str(cat.location.id),
        "location_name": "Main branch",
        "item_id": str(cat.item.id),
        "sku": cat.item.sku,
        "item_name": "Whole milk",
        "base_unit": "ml",
        "batch": {
            "id": batch_id,
            "is_default": False,
            "expires_on": "2026-11-12",
            "lot_code": None,
            "on_hand": "2000.000",
        },
    }
    # The receipt had raised low stock a day earlier (2000 on hand is at the
    # reorder point), so it is listed after the newer alert.
    assert (low["type"], low["batch"]) == ("low_stock", None)
    assert low["raised_at"] == "2026-11-09T06:00:00Z"


def test_alerts_are_listed_newest_first(client: TestClient, cat: Catalogue):
    use_milk(client, cat, "100", at=nov(8))  # nothing on hand: negative stock
    receive_milk(client, cat, "1", expires_day=11, at=nov(9))  # settles it

    alerts = get_alerts(client, cat, status="all")

    assert [(a["type"], a["status"], a["raised_at"]) for a in alerts] == [
        ("expiring", "open", "2026-11-10T12:00:00Z"),
        ("negative_stock", "resolved", "2026-11-08T12:00:00Z"),
    ]
    assert alerts[1]["resolved_at"] == "2026-11-09T12:00:00Z"
    assert alerts[1]["batch"]["is_default"] is True
    assert alerts[1]["batch"]["on_hand"] == "0.000"


def test_a_stock_write_shows_up_in_the_next_read(client: TestClient, cat: Catalogue):
    assert get_alerts(client, cat) == []

    use_milk(client, cat, "250", at=nov(10, 11))

    (alert,) = get_alerts(client, cat)
    assert (alert["type"], alert["raised_at"]) == ("negative_stock", "2026-11-10T11:00:00Z")
    assert alert["batch"]["on_hand"] == "-250.000"


# --- The sweep on read (D62) ----------------------------------------------------


def test_reading_alerts_sweeps_them_when_the_local_date_has_moved_on(
    client: TestClient,
    db_session: Session,
    cat: Catalogue,
    pin_now: Callable[[datetime], None],
):
    batch_id = receive_milk(client, cat, "2", expires_day=11, at=nov(9, 6))

    # Nothing has swept yet, so the batch isn't known to be expiring.
    business = db_session.get(Business, cat.business.id, populate_existing=True)
    assert business.alerts_swept_on is None

    # The first read sweeps as of the clock: 10 Nov, one day before expiry.
    assert kinds(get_alerts(client, cat)) == [("expiring", "open")]
    db_session.refresh(business)
    assert business.alerts_swept_on == date(2026, 11, 10)

    # 21:30 UTC on the 11th is already the 12th in Amman: expired.
    pin_now(datetime(2026, 11, 11, 21, 30, tzinfo=UTC))
    expired, expiring = get_alerts(client, cat, status="all")
    assert (expired["type"], expired["status"]) == ("expired", "open")
    assert (expiring["type"], expiring["status"]) == ("expiring", "resolved")
    assert expired["raised_at"] == expiring["resolved_at"] == "2026-11-11T21:30:00Z"
    assert expired["batch"]["id"] == batch_id
    db_session.refresh(business)
    assert business.alerts_swept_on == date(2026, 11, 12)

    assert reconcile(db_session, cat.business.id) == []


def test_a_second_read_on_the_same_day_raises_nothing_new(client: TestClient, cat: Catalogue):
    receive_milk(client, cat, "2", expires_day=11, at=nov(9, 6))

    first = get_alerts(client, cat, status="all")
    second = get_alerts(client, cat, status="all")

    assert first == second
    assert kinds(first) == [("expiring", "open")]


# --- Filters --------------------------------------------------------------------


def test_alerts_filter_by_status_type_item_and_location(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    item, _ = flour
    second = add(db_session, new_location(cat.business.id, name="Second branch"))
    add(db_session, new_par_level(cat.location, item))
    add(db_session, new_par_level(second, item))
    use_milk(client, cat, "100", at=nov(8))
    receive_milk(client, cat, "1", expires_day=11, at=nov(9))
    # Open: expiring milk, low flour at both locations. Resolved: negative milk.

    assert sorted(kinds(get_alerts(client, cat))) == [
        ("expiring", "open"),
        ("low_stock", "open"),
        ("low_stock", "open"),
    ]
    assert kinds(get_alerts(client, cat, status="resolved")) == [("negative_stock", "resolved")]
    assert len(get_alerts(client, cat, status="all")) == 4
    assert kinds(get_alerts(client, cat, type="expiring")) == [("expiring", "open")]
    assert kinds(get_alerts(client, cat, type="negative_stock")) == []
    assert {a["sku"] for a in get_alerts(client, cat, item_id=item.id)} == {item.sku}
    assert kinds(get_alerts(client, cat, status="all", item_id=cat.item.id)) == [
        ("expiring", "open"),
        ("negative_stock", "resolved"),
    ]
    (at_second,) = get_alerts(client, cat, location_id=second.id)
    assert (at_second["type"], at_second["location_name"]) == ("low_stock", "Second branch")


def test_limit_keeps_the_newest(client: TestClient, db_session: Session, cat: Catalogue):
    for day in (1, 2, 3):
        add(
            db_session,
            new_alert(
                cat.location, cat.item, status="resolved", raised_at=nov(day), resolved_at=nov(day)
            ),
        )

    alerts = get_alerts(client, cat, status="resolved", limit=2)

    assert [a["raised_at"] for a in alerts] == ["2026-11-03T12:00:00Z", "2026-11-02T12:00:00Z"]


@pytest.mark.parametrize(
    "params",
    [{"status": "snoozed"}, {"type": "overstock"}, {"limit": 0}, {"limit": 1001}],
)
def test_an_unknown_status_or_type_or_a_limit_out_of_range_is_a_422(
    client: TestClient, cat: Catalogue, params: dict[str, Any]
):
    response = client.get("/alerts", params=params, headers=api_headers(cat))
    assert response.status_code == 422


# --- Tenancy --------------------------------------------------------------------


def test_alerts_show_only_the_business_s_rows(client: TestClient, cat: Catalogue, other: Catalogue):
    use_milk(client, cat, "100", at=nov(10, 9))

    assert kinds(get_alerts(client, cat)) == [("negative_stock", "open")]
    assert get_alerts(client, other) == []
    # Another business's item or location matches nothing.
    assert get_alerts(client, cat, item_id=other.item.id) == []
    assert get_alerts(client, cat, location_id=other.location.id) == []


def test_alerts_need_a_known_business(client: TestClient):
    assert client.get("/alerts").status_code == 422
    unknown = {"X-Business-Id": str(uuid.uuid4())}
    assert client.get("/alerts", headers=unknown).status_code == 404


# --- After a waste --------------------------------------------------------------


def test_wasting_an_expired_batch_moves_its_alert_to_the_history(
    client: TestClient, cat: Catalogue
):
    batch_id = receive_milk(client, cat, "2", expires_day=8, at=nov(5))
    assert kinds(get_alerts(client, cat)) == [("expired", "open")]

    body = waste_json(cat, "2000", batch_id, reason="expired", occurred_at=nov(10, 11))
    assert client.post("/waste", json=body, headers=api_headers(cat)).status_code == 201

    assert get_alerts(client, cat) == []
    (resolved,) = get_alerts(client, cat, status="resolved")
    # Raised by the first read's sweep at noon; the waste is dated an hour
    # before that, so the alert resolves at the moment it was raised (D60).
    assert (resolved["raised_at"], resolved["resolved_at"]) == (
        "2026-11-10T12:00:00Z",
        "2026-11-10T12:00:00Z",
    )
    assert resolved["batch"]["on_hand"] == "0.000"
