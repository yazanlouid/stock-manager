"""GET /catalogue: the business's locations, suppliers and active items (D45)."""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.builders import (
    Catalogue,
    add,
    api_headers,
    new_item,
    new_location,
    new_supplier,
    new_supplier_item,
    new_unit,
)


def catalogue(client: TestClient, cat: Catalogue) -> dict:
    response = client.get("/catalogue", headers=api_headers(cat))
    assert response.status_code == 200, response.json()
    return response.json()


def test_catalogue_lists_the_business_s_rows(client: TestClient, cat: Catalogue):
    data = catalogue(client, cat)

    assert data == {
        "business": {
            "id": str(cat.business.id),
            "name": "Test Café",
            "currency": "JOD",
            "timezone": "Asia/Amman",
        },
        "locations": [{"id": str(cat.location.id), "name": "Main branch", "kind": "branch"}],
        "suppliers": [
            {"id": str(cat.supplier.id), "name": "Dairy Co", "phone": None, "lead_time_days": 1}
        ],
        "items": [
            {
                "id": str(cat.item.id),
                "sku": cat.item.sku,
                "name": "Whole milk",
                "category": None,
                "base_unit": "ml",
                "tracks_batches": True,
                "tracks_expiry": True,
                "shelf_life_days": None,
                # Decimals are strings at the column's scale.
                "units": [{"id": str(cat.unit.id), "name": "bottle", "factor_to_base": "1000.000"}],
                "supplier_prices": [
                    {
                        "supplier_id": str(cat.supplier.id),
                        "item_unit_id": str(cat.unit.id),
                        "pack_price": "0.850",
                        "is_preferred": True,
                    }
                ],
            }
        ],
    }


def test_catalogue_leaves_out_inactive_items(
    client: TestClient, db_session: Session, cat: Catalogue
):
    old = add(db_session, new_item(cat.business.id, name="Old syrup", is_active=False))
    add(db_session, new_unit(old))

    assert [item["id"] for item in catalogue(client, cat)["items"]] == [str(cat.item.id)]


def test_catalogue_leaves_out_another_business_s_rows(
    client: TestClient, cat: Catalogue, other: Catalogue
):
    data = catalogue(client, cat)

    assert [row["id"] for row in data["locations"]] == [str(cat.location.id)]
    assert [row["id"] for row in data["suppliers"]] == [str(cat.supplier.id)]
    assert [row["id"] for row in data["items"]] == [str(cat.item.id)]
    (item,) = data["items"]
    assert [u["id"] for u in item["units"]] == [str(cat.unit.id)]
    assert [p["supplier_id"] for p in item["supplier_prices"]] == [str(cat.supplier.id)]


def test_catalogue_order_is_fixed(client: TestClient, db_session: Session, cat: Catalogue):
    business_id = cat.business.id
    annex = add(db_session, new_location(business_id, name="Annex", kind="storeroom"))
    farm = add(db_session, new_supplier(business_id, name="Agri Farm"))
    dairy_2 = add(db_session, new_supplier(business_id, name="Dairy Co"))
    crate = add(db_session, new_unit(cat.item, name="crate", factor_to_base=Decimal("12000")))
    add(db_session, new_supplier_item(cat.supplier, cat.item, crate, pack_price=Decimal("9.600")))
    add(db_session, new_supplier_item(farm, cat.item, cat.unit, pack_price=Decimal("0.800")))
    add(db_session, new_supplier_item(dairy_2, cat.item, cat.unit, pack_price=Decimal("0.900")))
    flour = add(db_session, new_item(business_id, name="Flour", base_unit="g"))
    # Two suppliers are called Dairy Co, so their ids decide their order.
    dairies = sorted([cat.supplier, dairy_2], key=lambda supplier: supplier.id)

    data = catalogue(client, cat)

    assert [row["id"] for row in data["locations"]] == [str(annex.id), str(cat.location.id)]
    assert [row["id"] for row in data["suppliers"]] == [
        str(farm.id),
        *(str(dairy.id) for dairy in dairies),
    ]
    by_sku = sorted([cat.item, flour], key=lambda item: item.sku)
    assert [row["id"] for row in data["items"]] == [str(item.id) for item in by_sku]

    (milk,) = [row for row in data["items"] if row["id"] == str(cat.item.id)]
    # Units by size.
    assert [unit["name"] for unit in milk["units"]] == ["bottle", "crate"]
    # Prices by supplier name, then supplier id, then unit name.
    expected = [(farm.id, cat.unit.id)]
    for dairy in dairies:
        expected.append((dairy.id, cat.unit.id))
        if dairy.id == cat.supplier.id:
            expected.append((dairy.id, crate.id))
    assert [(p["supplier_id"], p["item_unit_id"]) for p in milk["supplier_prices"]] == [
        (str(supplier_id), str(unit_id)) for supplier_id, unit_id in expected
    ]
