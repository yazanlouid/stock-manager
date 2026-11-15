"""The order endpoints: worked examples through the API (D71, D73).

The cat fixture's item is whole milk, whose preferred supplier sells it by the
1000 ml bottle; the business is in Asia/Amman. Milk's par level is "reorder
at 2000 ml, order up to 6000 ml", and nothing is in stock, so the first read
of the orders sweeps and finds a draft for six bottles. The API's clock is
pinned to noon UTC on 10 Nov.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.domain.reconcile import reconcile
from app.models import Item, ItemUnit
from tests.builders import (
    Catalogue,
    add,
    api_headers,
    new_location,
    new_par_level,
    new_supplier,
    new_supplier_item,
    receipt_json,
    receipt_line_json,
)

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)
NOW_JSON = "2026-11-10T12:00:00Z"


@pytest.fixture(autouse=True)
def pinned_now(client: TestClient, pin_now: Callable[[datetime], None]) -> None:
    pin_now(NOW)


@pytest.fixture(autouse=True)
def par(db_session: Session, cat: Catalogue) -> None:
    add(db_session, new_par_level(cat.location, cat.item))


# --- Helpers --------------------------------------------------------------------


def get_orders(client: TestClient, cat: Catalogue, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/orders", params=params, headers=api_headers(cat))
    assert response.status_code == 200, response.json()
    return response.json()


def the_draft(client: TestClient, cat: Catalogue) -> dict[str, Any]:
    (order,) = get_orders(client, cat, status="draft")
    return order


def act(client: TestClient, cat: Catalogue, order: dict[str, Any], action: str) -> Any:
    return client.post(f"/orders/{order['id']}/{action}", headers=api_headers(cat))


def put_line(client: TestClient, cat: Catalogue, order: dict[str, Any], body: Any) -> Any:
    line_id = order["lines"][0]["id"]
    return client.put(f"/orders/{order['id']}/lines/{line_id}", json=body, headers=api_headers(cat))


def deliver(client: TestClient, cat: Catalogue, order: dict[str, Any], packs: str, **overrides):
    """Post a receipt of milk against the order."""
    line = receipt_line_json(cat.item, cat.unit, packs, "0.850", date(2026, 11, 20))
    body = receipt_json(cat, [line], po_id=order["id"], received_at=NOW, **overrides)
    return client.post("/receipts", json=body, headers=api_headers(cat))


# --- Reading --------------------------------------------------------------------


def test_the_first_read_finds_the_draft_the_rule_raised(client: TestClient, cat: Catalogue):
    (order,) = get_orders(client, cat)

    assert order == {
        "id": order["id"],
        "status": "draft",
        "location_id": str(cat.location.id),
        "location_name": "Main branch",
        "supplier_id": str(cat.supplier.id),
        "supplier_name": "Dairy Co",
        "sent_at": None,
        "received_at": None,
        "cancelled_at": None,
        "created_at": order["created_at"],
        "receipt_id": None,
        "lines": [
            {
                "id": order["lines"][0]["id"],
                "item_id": str(cat.item.id),
                "sku": cat.item.sku,
                "item_name": "Whole milk",
                "base_unit": "ml",
                "item_unit_id": str(cat.unit.id),
                "unit_name": "bottle",
                "factor_to_base": "1000.000",
                "suggested_packs": 6,
                "qty_packs": None,
                "order_packs": 6,
                "pack_price": "0.850",
            }
        ],
    }
    response = client.get(f"/orders/{order['id']}", headers=api_headers(cat))
    assert response.status_code == 200
    assert response.json() == order


def test_orders_are_listed_newest_first_and_filtered(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    item, sack = flour
    bakery = add(db_session, new_supplier(cat.business.id, name="Bakery Co"))
    add(db_session, new_supplier_item(bakery, item, sack, is_preferred=True))
    add(db_session, new_par_level(cat.location, item))
    drafts = {order["supplier_name"]: order for order in get_orders(client, cat)}
    dairy_draft, bakery_draft = drafts["Dairy Co"], drafts["Bakery Co"]
    assert act(client, cat, dairy_draft, "send").status_code == 200

    # Newest first: ids are UUIDv7, so a later order has a larger id.
    listed = get_orders(client, cat)
    assert [o["id"] for o in listed] == sorted((o["id"] for o in listed), reverse=True)
    assert {(o["supplier_name"], o["status"]) for o in listed} == {
        ("Dairy Co", "sent"),
        ("Bakery Co", "draft"),
    }
    assert [o["id"] for o in get_orders(client, cat, status="sent")] == [dairy_draft["id"]]
    assert [o["id"] for o in get_orders(client, cat, supplier_id=bakery.id)] == [bakery_draft["id"]]
    assert len(get_orders(client, cat, location_id=cat.location.id)) == 2
    assert len(get_orders(client, cat, limit=1)) == 1
    assert get_orders(client, cat, status="cancelled") == []


def test_a_draft_with_no_lines_is_left_out(client: TestClient, cat: Catalogue):
    draft = the_draft(client, cat)
    # A delivery of six bottles takes milk off the draft, which stays as an empty row.
    line = receipt_line_json(cat.item, cat.unit, "6", "0.850", date(2026, 11, 20))
    body = receipt_json(cat, [line], received_at=NOW)
    assert client.post("/receipts", json=body, headers=api_headers(cat)).status_code == 201

    assert get_orders(client, cat) == []
    # It can still be read by id.
    response = client.get(f"/orders/{draft['id']}", headers=api_headers(cat))
    assert (response.status_code, response.json()["lines"]) == (200, [])


def test_reads_show_only_the_business_s_orders(
    client: TestClient, db_session: Session, cat: Catalogue, other: Catalogue
):
    draft = the_draft(client, cat)
    second = add(db_session, new_location(other.business.id, name="Elsewhere"))

    assert get_orders(client, other) == []
    assert client.get(f"/orders/{draft['id']}", headers=api_headers(other)).status_code == 404
    assert client.get(f"/orders/{uuid.uuid4()}", headers=api_headers(cat)).status_code == 404
    # Another business's supplier or location matches nothing.
    assert get_orders(client, cat, supplier_id=other.supplier.id) == []
    assert get_orders(client, cat, location_id=second.id) == []
    assert client.get("/orders").status_code == 422  # no X-Business-Id


@pytest.mark.parametrize("params", [{"status": "open"}, {"limit": 0}, {"limit": 1001}])
def test_an_unknown_status_or_a_limit_out_of_range_is_a_422(
    client: TestClient, cat: Catalogue, params: dict[str, Any]
):
    response = client.get("/orders", params=params, headers=api_headers(cat))
    assert response.status_code == 422


# --- Setting a line's quantity --------------------------------------------------


def test_put_sets_and_clears_the_owner_s_number(client: TestClient, cat: Catalogue):
    draft = the_draft(client, cat)

    response = put_line(client, cat, draft, {"qty_packs": 4})
    assert response.status_code == 200
    (line,) = response.json()["lines"]
    assert (line["suggested_packs"], line["qty_packs"], line["order_packs"]) == (6, 4, 4)

    (line,) = put_line(client, cat, draft, {"qty_packs": 0}).json()["lines"]
    assert (line["qty_packs"], line["order_packs"]) == (0, 0)

    (line,) = put_line(client, cat, draft, {"qty_packs": None}).json()["lines"]
    assert (line["qty_packs"], line["order_packs"]) == (None, 6)


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        ({"qty_packs": -1}, 400),  # the domain refuses it
        ({"qty_packs": 2.5}, 422),  # whole packs only
        ({"qty_packs": "4"}, 422),  # a JSON integer, not a string
        ({"qty_packs": True}, 422),
        ({}, 422),  # qty_packs is required; null must be sent on purpose
        ({"qty_packs": 4, "suggested_packs": 9}, 422),  # unknown fields are refused
    ],
)
def test_put_refuses_a_bad_quantity(
    client: TestClient, cat: Catalogue, body: dict[str, Any], status_code: int
):
    draft = the_draft(client, cat)

    assert put_line(client, cat, draft, body).status_code == status_code
    assert the_draft(client, cat)["lines"][0]["qty_packs"] is None


def test_put_refuses_another_business_an_unknown_line_and_a_sent_order(
    client: TestClient, cat: Catalogue, other: Catalogue
):
    draft = the_draft(client, cat)
    line_id = draft["lines"][0]["id"]
    body = {"qty_packs": 2}

    response = client.put(
        f"/orders/{draft['id']}/lines/{line_id}", json=body, headers=api_headers(other)
    )
    assert (response.status_code, "not found" in response.json()["detail"]) == (400, True)
    response = client.put(
        f"/orders/{draft['id']}/lines/{uuid.uuid4()}", json=body, headers=api_headers(cat)
    )
    assert response.status_code == 400

    assert act(client, cat, draft, "send").status_code == 200
    response = put_line(client, cat, draft, body)
    assert response.status_code == 400
    assert "is sent, not a draft" in response.json()["detail"]


# --- Send and cancel ------------------------------------------------------------


def test_send_marks_the_draft_sent_at_the_server_clock(client: TestClient, cat: Catalogue):
    draft = the_draft(client, cat)
    put_line(client, cat, draft, {"qty_packs": 4})

    response = act(client, cat, draft, "send")

    assert response.status_code == 200
    sent = response.json()
    assert (sent["id"], sent["status"], sent["sent_at"]) == (draft["id"], "sent", NOW_JSON)
    assert [(x["suggested_packs"], x["qty_packs"]) for x in sent["lines"]] == [(6, 4)]
    # Four bottles are on order, so the rule drafts the other two at once.
    new_draft = the_draft(client, cat)
    assert new_draft["id"] != draft["id"]
    assert [x["suggested_packs"] for x in new_draft["lines"]] == [2]

    # A retry is refused: the order is no longer a draft.
    again = act(client, cat, draft, "send")
    assert again.status_code == 400
    assert "is sent, not a draft, so it can't be sent" in again.json()["detail"]


def test_a_draft_set_to_order_nothing_can_t_be_sent(client: TestClient, cat: Catalogue):
    draft = the_draft(client, cat)
    put_line(client, cat, draft, {"qty_packs": 0})

    response = act(client, cat, draft, "send")

    assert response.status_code == 400
    assert "has nothing to order" in response.json()["detail"]
    assert the_draft(client, cat)["status"] == "draft"


def test_cancel_takes_a_sent_order_off_order(client: TestClient, cat: Catalogue):
    draft = the_draft(client, cat)
    assert act(client, cat, draft, "cancel").status_code == 400  # a draft can't be cancelled
    act(client, cat, draft, "send")
    assert get_orders(client, cat, status="draft") == []

    response = act(client, cat, draft, "cancel")

    assert response.status_code == 200
    cancelled = response.json()
    assert (cancelled["status"], cancelled["sent_at"], cancelled["cancelled_at"]) == (
        "cancelled",
        NOW_JSON,
        NOW_JSON,
    )
    assert [x["suggested_packs"] for x in the_draft(client, cat)["lines"]] == [6]
    assert act(client, cat, draft, "cancel").status_code == 400


def test_actions_on_another_business_s_order_are_refused(
    client: TestClient, cat: Catalogue, other: Catalogue
):
    draft = the_draft(client, cat)

    for action in ("send", "cancel"):
        response = client.post(f"/orders/{draft['id']}/{action}", headers=api_headers(other))
        assert (response.status_code, "not found" in response.json()["detail"]) == (400, True)
    assert the_draft(client, cat)["status"] == "draft"


# --- Receiving against an order -------------------------------------------------


def test_a_receipt_with_po_id_closes_the_sent_order(
    client: TestClient, db_session: Session, cat: Catalogue
):
    draft = the_draft(client, cat)
    act(client, cat, draft, "send")
    client_id = str(uuid.uuid4())

    response = deliver(client, cat, draft, "6", client_id=client_id)

    assert response.status_code == 201
    receipt = response.json()["receipt"]
    assert receipt["po_id"] == draft["id"]
    order = client.get(f"/orders/{draft['id']}", headers=api_headers(cat)).json()
    assert (order["status"], order["received_at"], order["receipt_id"]) == (
        "received",
        NOW_JSON,
        receipt["id"],
    )
    assert get_orders(client, cat, status="draft") == []

    # A retry replays; a second delivery against the closed order is refused.
    replay = deliver(client, cat, draft, "6", client_id=client_id)
    assert (replay.status_code, replay.json()["replayed"]) == (200, True)
    second = deliver(client, cat, draft, "1")
    assert second.status_code == 400
    assert "is received, not sent" in second.json()["detail"]
    assert reconcile(db_session, cat.business.id) == []


def test_a_short_delivery_closes_the_order_and_the_rest_is_drafted_again(
    client: TestClient, db_session: Session, cat: Catalogue
):
    draft = the_draft(client, cat)
    act(client, cat, draft, "send")

    assert deliver(client, cat, draft, "2").status_code == 201

    # 2000 ml on hand is at the reorder point, with nothing on order now.
    assert [x["suggested_packs"] for x in the_draft(client, cat)["lines"]] == [4]
    assert reconcile(db_session, cat.business.id) == []


def test_a_receipt_can_t_be_against_a_draft_or_another_supplier_s_order(
    client: TestClient, db_session: Session, cat: Catalogue
):
    draft = the_draft(client, cat)

    response = deliver(client, cat, draft, "6")
    assert response.status_code == 400
    assert "is draft, not sent" in response.json()["detail"]

    act(client, cat, draft, "send")
    bakery = add(db_session, new_supplier(cat.business.id, name="Bakery Co"))
    response = deliver(client, cat, draft, "6", supplier_id=bakery.id)
    assert response.status_code == 400
    assert "is for another supplier" in response.json()["detail"]

    response = deliver(client, cat, {"id": str(uuid.uuid4())}, "6")
    assert response.status_code == 400
    assert "not found" in response.json()["detail"]

    # None of the refusals closed the order.
    order = client.get(f"/orders/{draft['id']}", headers=api_headers(cat)).json()
    assert order["status"] == "sent"
