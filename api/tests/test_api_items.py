"""Item setup through the API (D90 to D94).

The domain's rules are worked through in test_items.py; these check the
endpoints: request and response shapes, status codes, tenancy, and that a
write's rules follow in the same request. The API's clock is pinned to noon
UTC on 10 Nov.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.builders import Catalogue, add, api_headers, new_supplier, usage_json

NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def pinned_now(client: TestClient, pin_now: Callable[[datetime], None]) -> None:
    pin_now(NOW)


def call(
    client: TestClient,
    cat: Catalogue,
    method: str,
    path: str,
    status: int = 200,
    params: dict[str, str] | None = None,
    **body: Any,
) -> Any:
    response = client.request(
        method, path, headers=api_headers(cat), params=params, json=body or None
    )
    assert response.status_code == status, response.json()
    return response.json()


FLOUR = {
    "sku": "DRY-FLOUR",
    "name": "Flour",
    "base_unit": "g",
    "category": "dry goods",
    "units": [{"name": "25 kg sack", "factor_to_base": "25000"}],
}


def test_create_then_read_an_item(client: TestClient, cat: Catalogue):
    created = call(client, cat, "POST", "/items", 201, **FLOUR)

    assert created == {
        "id": created["id"],
        "sku": "DRY-FLOUR",
        "name": "Flour",
        "category": "dry goods",
        "base_unit": "g",
        "tracks_batches": False,
        "tracks_expiry": False,
        "shelf_life_days": None,
        "is_active": True,
        "units": [
            {"id": created["units"][0]["id"], "name": "25 kg sack", "factor_to_base": "25000.000"}
        ],
        "supplier_prices": [],
        "par_levels": [],
    }
    assert call(client, cat, "GET", f"/items/{created['id']}") == created
    assert [item["sku"] for item in call(client, cat, "GET", "/items")] == sorted(
        [cat.item.sku, "DRY-FLOUR"]
    )


def test_a_duplicate_sku_is_a_400(client: TestClient, cat: Catalogue):
    call(client, cat, "POST", "/items", 201, **FLOUR)

    body = call(client, cat, "POST", "/items", 400, **FLOUR)
    assert "already used" in body["detail"]


def test_unknown_fields_and_explicit_nulls_are_422s(client: TestClient, cat: Catalogue):
    call(client, cat, "POST", "/items", 422, **(FLOUR | {"business_id": str(cat.business.id)}))
    call(client, cat, "PATCH", f"/items/{cat.item.id}", 422, sku=None)
    call(client, cat, "PATCH", f"/items/{cat.item.id}", 422, is_active=None)


def test_patch_changes_only_what_is_sent(client: TestClient, cat: Catalogue):
    before = call(client, cat, "GET", f"/items/{cat.item.id}")

    after = call(client, cat, "PATCH", f"/items/{cat.item.id}", name="Milk", shelf_life_days=7)

    assert after == before | {"name": "Milk", "shelf_life_days": 7}
    cleared = call(client, cat, "PATCH", f"/items/{cat.item.id}", shelf_life_days=None)
    assert cleared["shelf_life_days"] is None


def test_tracking_is_fixed_after_the_first_movement(client: TestClient, cat: Catalogue):
    response = client.post(
        "/usage",
        json=usage_json(cat, "100", occurred_at=datetime(2026, 11, 10, 8, tzinfo=UTC)),
        headers=api_headers(cat),
    )
    assert response.status_code == 201, response.json()

    body = call(client, cat, "PATCH", f"/items/{cat.item.id}", 400, tracks_expiry=False)
    assert "has movements" in body["detail"]


def test_a_par_level_raises_its_rules_in_the_same_request(client: TestClient, cat: Catalogue):
    levels = {"low_stock_level": "1000", "reorder_point": "2000", "order_up_to": "6000"}

    item = call(client, cat, "PUT", f"/items/{cat.item.id}/par-levels/{cat.location.id}", **levels)

    assert item["par_levels"] == [
        {
            "location_id": str(cat.location.id),
            "low_stock_level": "1000.000",
            "reorder_point": "2000.000",
            "order_up_to": "6000.000",
        }
    ]
    # No stock: low, and on the preferred supplier's draft for 6 bottles.
    alerts = call(client, cat, "GET", "/alerts")
    assert [(a["type"], a["item_id"]) for a in alerts] == [("low_stock", str(cat.item.id))]
    (draft,) = call(client, cat, "GET", "/orders", params={"status": "draft"})
    assert [(line["item_id"], line["suggested_packs"]) for line in draft["lines"]] == [
        (str(cat.item.id), 6)
    ]

    item = call(client, cat, "DELETE", f"/items/{cat.item.id}/par-levels/{cat.location.id}")
    assert item["par_levels"] == []
    assert call(client, cat, "GET", "/alerts", params={"status": "open"}) == []
    assert call(client, cat, "GET", "/orders", params={"status": "draft"}) == []


def test_par_levels_out_of_order_are_a_400(client: TestClient, cat: Catalogue):
    levels = {"low_stock_level": "3000", "reorder_point": "2000", "order_up_to": "6000"}
    body = call(
        client, cat, "PUT", f"/items/{cat.item.id}/par-levels/{cat.location.id}", 400, **levels
    )
    assert "must be in order" in body["detail"]


def test_supplier_prices_and_the_preferred_supplier(
    client: TestClient, db_session: Session, cat: Catalogue
):
    second = add(db_session, new_supplier(cat.business.id, name="A second dairy"))
    unit = call(
        client,
        cat,
        "POST",
        f"/items/{cat.item.id}/units",
        201,
        name="crate",
        factor_to_base="12000",
    )
    crate = next(u for u in unit["units"] if u["name"] == "crate")

    item = call(
        client,
        cat,
        "PUT",
        f"/items/{cat.item.id}/supplier-prices",
        supplier_id=str(second.id),
        item_unit_id=crate["id"],
        pack_price="9.600",
        is_preferred=True,
    )

    # Sorted by supplier name: "A second dairy" first; only it is preferred.
    assert [
        (p["supplier_id"], p["pack_price"], p["is_preferred"]) for p in item["supplier_prices"]
    ] == [
        (str(second.id), "9.600", True),
        (
            str(cat.supplier.id),
            f"{cat.supplier_item.pack_price:.3f}",
            False,
        ),
    ]
    price_id = item["supplier_prices"][0]["id"]
    item = call(client, cat, "DELETE", f"/items/{cat.item.id}/supplier-prices/{price_id}")
    assert [p["supplier_id"] for p in item["supplier_prices"]] == [str(cat.supplier.id)]


def test_units_are_renamed_through_patch(client: TestClient, cat: Catalogue):
    item = call(
        client, cat, "PATCH", f"/items/{cat.item.id}/units/{cat.unit.id}", name="bottle 1 L"
    )

    assert [u["name"] for u in item["units"]] == ["bottle 1 L"]
    call(client, cat, "PATCH", f"/items/{cat.item.id}/units/{cat.unit.id}", 422, factor_to_base="2")


def test_deactivated_items_leave_the_catalogue_but_not_items(client: TestClient, cat: Catalogue):
    call(client, cat, "PATCH", f"/items/{cat.item.id}", is_active=False)

    assert call(client, cat, "GET", "/catalogue")["items"] == []
    assert [item["is_active"] for item in call(client, cat, "GET", "/items")] == [False]


def test_another_business_s_item_is_a_404(client: TestClient, cat: Catalogue, other: Catalogue):
    for method, path, body in [
        ("GET", f"/items/{other.item.id}", {}),
        ("PATCH", f"/items/{other.item.id}", {"name": "Mine"}),
        ("POST", f"/items/{other.item.id}/units", {"name": "x", "factor_to_base": "1"}),
        ("DELETE", f"/items/{other.item.id}/par-levels/{cat.location.id}", {}),
    ]:
        call(client, cat, method, path, 404, **body)
    # Another business's location or supplier is the domain's 400.
    levels = {"low_stock_level": "1", "reorder_point": "2", "order_up_to": "3"}
    call(client, cat, "PUT", f"/items/{cat.item.id}/par-levels/{other.location.id}", 400, **levels)
