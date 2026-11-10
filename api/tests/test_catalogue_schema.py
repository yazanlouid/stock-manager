"""Database constraints on the tenancy and catalogue tables.

Each rejected insert runs in a savepoint (session.begin_nested()), so the test's
outer transaction stays usable. We check the name of the constraint Postgres
reports, so a test only passes when the intended constraint fired. The row
builders and the cat and other fixtures are in builders.py and conftest.py.
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.db import Base
from tests.builders import (
    Catalogue,
    add,
    assert_rejected,
    new_business,
    new_item,
    new_location,
    new_supplier,
    new_supplier_item,
    new_unit,
    new_user,
)

# --- Valid rows ---------------------------------------------------------------


def test_valid_catalogue_gets_server_defaults(cat: Catalogue) -> None:
    assert cat.business.id.version == 7  # uuidv7()
    assert isinstance(cat.business.created_at, datetime)
    assert cat.business.created_at.tzinfo is not None  # timestamptz
    assert cat.business.expiry_warn_days == 3
    assert cat.location.business_id == cat.business.id
    assert cat.item.is_active is True
    assert cat.supplier.lead_time_days == 1
    assert cat.unit.factor_to_base == Decimal("1000.000")


def test_boundary_values_are_accepted(db_session: Session, cat: Catalogue) -> None:
    item = add(db_session, new_item(cat.business.id, shelf_life_days=None))
    unit = add(db_session, new_unit(item, factor_to_base=Decimal("0.001")))
    supplier = add(db_session, new_supplier(cat.business.id, lead_time_days=0))
    add(db_session, new_supplier_item(supplier, item, unit, pack_price=Decimal("0")))
    add(db_session, new_item(cat.business.id, shelf_life_days=1, tracks_batches=True))


# --- Each constraint rejects a bad row ----------------------------------------

BadRow = Callable[[Catalogue], Base]

BAD_ROWS: list[Any] = [
    pytest.param(
        lambda c: new_business(currency="jd"),
        "ck_businesses_currency_3_letters",
        id="currency-too-short",
    ),
    pytest.param(
        lambda c: new_business(currency="JODX"),
        "ck_businesses_currency_3_letters",
        id="currency-too-long",
    ),
    pytest.param(
        lambda c: new_business(currency="jod"),
        "ck_businesses_currency_3_letters",
        id="currency-lowercase",
    ),
    pytest.param(
        lambda c: new_business(expiry_warn_days=0),
        "ck_businesses_expiry_warn_days_positive",
        id="expiry-warn-days-zero",
    ),
    pytest.param(
        lambda c: new_location(c.business.id, kind="warehouse"),
        "ck_locations_kind",
        id="location-kind",
    ),
    pytest.param(
        lambda c: new_user(c.business.id, role="admin"),
        "ck_users_role",
        id="user-role",
    ),
    pytest.param(
        lambda c: new_user(c.business.id, email=c.user.email),
        "uq_users_email",
        id="duplicate-email",
    ),
    pytest.param(
        lambda c: new_item(c.business.id, base_unit="kg"),
        "ck_items_base_unit",
        id="item-base-unit",
    ),
    pytest.param(
        lambda c: new_item(c.business.id, tracks_expiry=True, tracks_batches=False),
        "ck_items_expiry_requires_batches",
        id="expiry-without-batches",
    ),
    pytest.param(
        lambda c: new_item(c.business.id, shelf_life_days=0),
        "ck_items_shelf_life_days_positive",
        id="shelf-life-zero",
    ),
    pytest.param(
        lambda c: new_item(c.business.id, sku=c.item.sku),
        "uq_items_business_id_sku",
        id="duplicate-sku",
    ),
    pytest.param(
        lambda c: new_unit(c.item, name="crate", factor_to_base=Decimal("0")),
        "ck_item_units_factor_to_base_positive",
        id="factor-zero",
    ),
    pytest.param(
        lambda c: new_unit(c.item, name="crate", factor_to_base=Decimal("-1")),
        "ck_item_units_factor_to_base_positive",
        id="factor-negative",
    ),
    pytest.param(
        lambda c: new_unit(c.item, name=c.unit.name),
        "uq_item_units_item_id_name",
        id="duplicate-unit-name",
    ),
    pytest.param(
        lambda c: new_supplier(c.business.id, lead_time_days=-1),
        "ck_suppliers_lead_time_days_non_negative",
        id="lead-time-negative",
    ),
    pytest.param(
        lambda c: new_supplier_item(c.supplier, c.item, c.unit, pack_price=Decimal("-0.001")),
        "ck_supplier_items_pack_price_non_negative",
        id="pack-price-negative",
    ),
    pytest.param(
        # Not preferred, so only the (supplier, item, unit) constraint applies.
        lambda c: new_supplier_item(c.supplier, c.item, c.unit, is_preferred=False),
        "uq_supplier_items_supplier_id_item_id_item_unit_id",
        id="duplicate-supplier-item",
    ),
]


@pytest.mark.parametrize(("bad_row", "constraint"), BAD_ROWS)
def test_constraint_rejects_bad_row(
    db_session: Session, cat: Catalogue, bad_row: BadRow, constraint: str
) -> None:
    assert_rejected(db_session, bad_row(cat), constraint)


# --- Preferred supplier -------------------------------------------------------


def test_second_preferred_supplier_for_an_item_is_rejected(
    db_session: Session, cat: Catalogue
) -> None:
    supplier = add(db_session, new_supplier(cat.business.id, name="Second dairy"))
    row = new_supplier_item(supplier, cat.item, cat.unit, is_preferred=True)
    assert_rejected(db_session, row, "uq_supplier_items_one_preferred_per_item")


def test_non_preferred_supplier_and_preferred_for_another_item_are_accepted(
    db_session: Session, cat: Catalogue
) -> None:
    supplier = add(db_session, new_supplier(cat.business.id, name="Second dairy"))
    add(db_session, new_supplier_item(supplier, cat.item, cat.unit, is_preferred=False))

    item = add(db_session, new_item(cat.business.id))
    unit = add(db_session, new_unit(item))
    add(db_session, new_supplier_item(cat.supplier, item, unit, is_preferred=True))


# --- Tenancy ------------------------------------------------------------------


def test_every_business_id_references_businesses(db_session: Session) -> None:
    inspector = inspect(db_session.connection())
    tables = set(inspector.get_table_names()) - {"alembic_version", "businesses"}
    assert tables  # Guard against passing vacuously.

    for table in sorted(tables):
        columns = {column["name"] for column in inspector.get_columns(table)}
        assert "business_id" in columns, f"{table} has no business_id"
        assert any(
            fk["constrained_columns"] == ["business_id"]
            and fk["referred_table"] == "businesses"
            and fk["referred_columns"] == ["id"]
            for fk in inspector.get_foreign_keys(table)
        ), f"{table}.business_id has no foreign key to businesses(id)"


@pytest.mark.parametrize(
    ("build", "constraint"),
    [
        pytest.param(new_location, "fk_locations_business_id_businesses", id="location"),
        pytest.param(new_user, "fk_users_business_id_businesses", id="user"),
        pytest.param(new_item, "fk_items_business_id_businesses", id="item"),
        pytest.param(new_supplier, "fk_suppliers_business_id_businesses", id="supplier"),
    ],
)
def test_row_for_unknown_business_is_rejected(
    db_session: Session, build: Callable[[uuid.UUID], Base], constraint: str
) -> None:
    assert_rejected(db_session, build(uuid.uuid4()), constraint)


def test_unit_cannot_belong_to_another_business_item(
    db_session: Session, cat: Catalogue, other: Catalogue
) -> None:
    row = new_unit(other.item, business_id=cat.business.id, name="crate")
    assert_rejected(db_session, row, "fk_item_units_business_id_item_id_items")


def test_supplier_item_cannot_reference_another_business_item(
    db_session: Session, cat: Catalogue, other: Catalogue
) -> None:
    # other.unit belongs to other.item, so only the tenancy foreign key fails.
    row = new_supplier_item(cat.supplier, other.item, other.unit)
    assert_rejected(db_session, row, "fk_supplier_items_business_id_item_id_items")


def test_supplier_item_cannot_reference_another_business_supplier(
    db_session: Session, cat: Catalogue, other: Catalogue
) -> None:
    row = new_supplier_item(other.supplier, cat.item, cat.unit, business_id=cat.business.id)
    assert_rejected(db_session, row, "fk_supplier_items_business_id_supplier_id_suppliers")


def test_supplier_item_unit_must_belong_to_the_same_item(
    db_session: Session, cat: Catalogue
) -> None:
    flour = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    sack = add(db_session, new_unit(flour, name="sack", factor_to_base=Decimal("25000")))
    row = new_supplier_item(cat.supplier, cat.item, sack, is_preferred=False)
    assert_rejected(db_session, row, "fk_supplier_items_item_id_item_unit_id_item_units")


def test_same_sku_is_allowed_in_different_businesses(
    db_session: Session, cat: Catalogue, other: Catalogue
) -> None:
    add(db_session, new_item(other.business.id, sku=cat.item.sku))
