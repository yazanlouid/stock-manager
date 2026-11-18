"""Database constraints on par_levels, alerts and purchase orders.

Same approach as test_stock_schema.py: each rejected row runs in a savepoint,
and we check the name of the constraint Postgres reports. Every bad row breaks
exactly one constraint. The stock fixture (conftest.py) gives a location, the
milk item with a received batch and a default batch, and flour with no batches.
"""

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.db import Base
from tests.builders import (
    OCCURRED_AT,
    Catalogue,
    Stock,
    add,
    assert_rejected,
    new_alert,
    new_location,
    new_order_line,
    new_par_level,
    new_purchase_order,
    new_receipt,
    new_supplier,
    new_unit,
)

LATER = OCCURRED_AT + timedelta(hours=3)

# --- Valid rows ---------------------------------------------------------------


def test_valid_rule_rows_get_server_defaults(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    par = add(db_session, new_par_level(c.location, c.item))
    alert = add(db_session, new_alert(c.location, c.item))

    assert par.id.version == 7  # uuidv7()
    assert par.created_at.tzinfo is not None  # timestamptz
    assert alert.id.version == 7
    assert alert.status == "open"
    assert alert.resolved_at is None
    assert alert.batch_id is None

    db_session.refresh(c.business)
    assert c.business.alerts_swept_on is None  # no alerts date until the first sweep


def test_boundary_values_are_accepted(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    # Reorder and alert only when nothing is left (the builder's low-stock
    # level is half the reorder point, so 0), and all three levels equal.
    add(db_session, new_par_level(c.location, c.item, reorder_point=Decimal("0")))
    add(
        db_session,
        new_par_level(
            c.location,
            stock.flour,
            low_stock_level=Decimal("500"),
            reorder_point=Decimal("500"),
            order_up_to=Decimal("500"),
        ),
    )
    # Resolved at the moment it was raised.
    add(
        db_session,
        new_alert(c.location, c.item, status="resolved", resolved_at=OCCURRED_AT),
    )


def test_the_same_item_can_have_a_par_level_at_each_location(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    second = add(db_session, new_location(c.business.id, name="Second branch"))
    add(db_session, new_par_level(c.location, c.item))
    add(db_session, new_par_level(second, c.item))


# --- One open alert per location, item, type and batch ------------------------


def test_resolved_alerts_pile_up_next_to_one_open_alert(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    for _ in range(2):
        add(db_session, new_alert(c.location, c.item, status="resolved", resolved_at=LATER))
    add(db_session, new_alert(c.location, c.item))


def test_open_alerts_differ_by_type_batch_or_location(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    batch_id = stock.batch.id
    # Two types on one batch, one type on two batches.
    add(db_session, new_alert(c.location, c.item, type="expiring", batch_id=batch_id))
    add(db_session, new_alert(c.location, c.item, type="expired", batch_id=batch_id))
    add(db_session, new_alert(c.location, c.item, type="expired", batch_id=stock.default_batch.id))
    # A low_stock alert has no batch, so only its location tells two apart (D60).
    second = add(db_session, new_location(c.business.id, name="Second branch"))
    add(db_session, new_alert(c.location, c.item))
    add(db_session, new_alert(second, c.item))


# --- Each constraint rejects a bad row ----------------------------------------

BadRow = Callable[[Stock], Base]

BAD_ROWS: list[Any] = [
    # par_levels
    # ck_par_levels_reorder_point_non_negative still stands, but since D83 a
    # negative reorder point always breaks one of the low-stock checks too,
    # and Postgres reports the first failing check by name. The low-stock
    # level's two checks imply it.
    pytest.param(
        lambda s: new_par_level(s.cat.location, s.flour, low_stock_level=Decimal("-0.001")),
        "ck_par_levels_low_stock_level_non_negative",
        id="low-stock-level-negative",
    ),
    pytest.param(
        lambda s: new_par_level(s.cat.location, s.flour, low_stock_level=Decimal("2000.001")),
        "ck_par_levels_low_stock_level_at_most_reorder_point",
        id="low-stock-level-above-reorder-point",
    ),
    pytest.param(
        lambda s: new_par_level(
            s.cat.location, s.flour, reorder_point=Decimal("0"), order_up_to=Decimal("0")
        ),
        "ck_par_levels_order_up_to_positive",
        id="order-up-to-zero",
    ),
    pytest.param(
        lambda s: new_par_level(
            s.cat.location, s.flour, reorder_point=Decimal("6000.001"), order_up_to=Decimal("6000")
        ),
        "ck_par_levels_reorder_point_at_most_order_up_to",
        id="reorder-point-above-order-up-to",
    ),
    # alerts
    pytest.param(
        # With a batch, so the unknown type breaks only the type check.
        lambda s: new_alert(s.cat.location, s.cat.item, type="overstock", batch_id=s.batch.id),
        "ck_alerts_type",
        id="alert-type",
    ),
    pytest.param(
        lambda s: new_alert(s.cat.location, s.flour, status="snoozed"),
        "ck_alerts_status",
        id="alert-status",
    ),
    pytest.param(
        lambda s: new_alert(s.cat.location, s.flour, resolved_at=LATER),
        "ck_alerts_resolved_at_iff_resolved",
        id="open-with-resolved-at",
    ),
    pytest.param(
        lambda s: new_alert(s.cat.location, s.flour, status="resolved"),
        "ck_alerts_resolved_at_iff_resolved",
        id="resolved-without-resolved-at",
    ),
    pytest.param(
        lambda s: new_alert(
            s.cat.location,
            s.flour,
            status="resolved",
            resolved_at=OCCURRED_AT - timedelta(seconds=1),
        ),
        "ck_alerts_resolved_at_not_before_raised_at",
        id="resolved-before-raised",
    ),
    pytest.param(
        lambda s: new_alert(s.cat.location, s.cat.item, batch_id=s.batch.id),
        "ck_alerts_batch_id_unless_low_stock",
        id="low-stock-with-batch",
    ),
    pytest.param(
        lambda s: new_alert(s.cat.location, s.cat.item, type="expired"),
        "ck_alerts_batch_id_unless_low_stock",
        id="expired-without-batch",
    ),
    pytest.param(
        lambda s: new_alert(s.cat.location, s.flour, type="expired", batch_id=s.batch.id),
        "fk_alerts_business_id_location_id_item_id_batch_id_batches",
        id="alert-batch-of-another-item",
    ),
]


@pytest.mark.parametrize(("build", "constraint"), BAD_ROWS)
def test_constraint_rejects_bad_row(
    db_session: Session, stock: Stock, build: BadRow, constraint: str
) -> None:
    assert_rejected(db_session, build(stock), constraint)


def test_second_par_level_for_an_item_and_location_is_rejected(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    add(db_session, new_par_level(c.location, c.item))
    assert_rejected(
        db_session, new_par_level(c.location, c.item), "uq_par_levels_location_id_item_id"
    )


def test_second_open_low_stock_alert_is_rejected(db_session: Session, stock: Stock) -> None:
    """NULLS NOT DISTINCT: two alerts with no batch still collide."""
    c = stock.cat
    add(db_session, new_alert(c.location, c.item))
    assert_rejected(db_session, new_alert(c.location, c.item), "uq_alerts_one_open")


def test_second_open_alert_of_a_type_for_a_batch_is_rejected(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    add(db_session, new_alert(c.location, c.item, type="expired", batch_id=stock.batch.id))
    row = new_alert(c.location, c.item, type="expired", batch_id=stock.batch.id)
    assert_rejected(db_session, row, "uq_alerts_one_open")


# --- Tenancy ------------------------------------------------------------------


def test_par_level_cannot_use_another_business_location_or_item(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    c = stock.cat
    row = new_par_level(other.location, c.item, business_id=c.business.id)
    assert_rejected(db_session, row, "fk_par_levels_business_id_location_id_locations")
    row = new_par_level(c.location, other.item)
    assert_rejected(db_session, row, "fk_par_levels_business_id_item_id_items")


def test_alert_cannot_use_another_business_location_or_item(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    c = stock.cat
    row = new_alert(other.location, c.item, business_id=c.business.id)
    assert_rejected(db_session, row, "fk_alerts_business_id_location_id_locations")
    row = new_alert(c.location, other.item)
    assert_rejected(db_session, row, "fk_alerts_business_id_item_id_items")


def test_alert_cannot_name_another_business_batch(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    c = stock.cat
    row = new_alert(c.location, c.item, type="expired", batch_id=other_stock.batch.id)
    assert_rejected(db_session, row, "fk_alerts_business_id_location_id_item_id_batch_id_batches")


# --- Purchase orders ----------------------------------------------------------


def sent(**overrides: Any) -> dict[str, Any]:
    """Column values of a sent order; pass more to make it received or cancelled."""
    return {"status": "sent", "sent_at": OCCURRED_AT} | overrides


def test_valid_order_rows_get_server_defaults(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    order = add(db_session, new_purchase_order(c.location, c.supplier))
    line = add(db_session, new_order_line(order, c.item, c.unit))

    assert order.id.version == 7  # uuidv7()
    assert order.status == "draft"
    assert (order.sent_at, order.received_at, order.cancelled_at) == (None, None, None)
    assert line.qty_packs is None  # as suggested
    assert line.suggested_packs == 4


def test_each_order_status_takes_its_times(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    add(db_session, new_purchase_order(c.location, c.supplier, **sent()))
    add(
        db_session,
        new_purchase_order(c.location, c.supplier, **sent(status="received", received_at=LATER)),
    )
    add(
        db_session,
        new_purchase_order(c.location, c.supplier, **sent(status="cancelled", cancelled_at=LATER)),
    )
    # A line whose owner says not to order it.
    draft = add(db_session, new_purchase_order(c.location, c.supplier))
    add(db_session, new_order_line(draft, c.item, c.unit, qty_packs=0))


def test_one_draft_per_supplier_and_location_beside_any_number_of_others(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    second_location = add(db_session, new_location(c.business.id, name="Second branch"))
    second_supplier = add(db_session, new_supplier(c.business.id, name="Bakery Co"))
    add(db_session, new_purchase_order(c.location, c.supplier))
    add(db_session, new_purchase_order(c.location, c.supplier, **sent()))
    add(db_session, new_purchase_order(c.location, c.supplier, **sent()))
    add(db_session, new_purchase_order(second_location, c.supplier))
    add(db_session, new_purchase_order(c.location, second_supplier))

    row = new_purchase_order(c.location, c.supplier)
    assert_rejected(db_session, row, "uq_purchase_orders_one_draft_per_location_supplier")


BAD_ORDERS: list[Any] = [
    pytest.param(
        # With sent_at, so the unknown status breaks only the status check.
        sent(status="shipped"),
        "ck_purchase_orders_status",
        id="order-status",
    ),
    pytest.param(
        {"sent_at": OCCURRED_AT},
        "ck_purchase_orders_sent_at_unless_draft",
        id="draft-with-sent-at",
    ),
    pytest.param(
        {"status": "sent"},
        "ck_purchase_orders_sent_at_unless_draft",
        id="sent-without-sent-at",
    ),
    pytest.param(
        sent(status="received"),
        "ck_purchase_orders_received_at_iff_received",
        id="received-without-received-at",
    ),
    pytest.param(
        sent(received_at=LATER),
        "ck_purchase_orders_received_at_iff_received",
        id="sent-with-received-at",
    ),
    pytest.param(
        sent(status="cancelled"),
        "ck_purchase_orders_cancelled_at_iff_cancelled",
        id="cancelled-without-cancelled-at",
    ),
    pytest.param(
        sent(cancelled_at=LATER),
        "ck_purchase_orders_cancelled_at_iff_cancelled",
        id="sent-with-cancelled-at",
    ),
]


@pytest.mark.parametrize(("values", "constraint"), BAD_ORDERS)
def test_order_constraint_rejects_bad_row(
    db_session: Session, stock: Stock, values: dict[str, Any], constraint: str
) -> None:
    row = new_purchase_order(stock.cat.location, stock.cat.supplier, **values)
    assert_rejected(db_session, row, constraint)


def test_order_line_constraints(db_session: Session, stock: Stock, other: Catalogue) -> None:
    c = stock.cat
    order = add(db_session, new_purchase_order(c.location, c.supplier))
    flour_unit = add(db_session, new_unit(stock.flour, name="sack"))

    def rejected(row: Base, constraint: str) -> None:
        assert_rejected(db_session, row, constraint)

    rejected(
        new_order_line(order, c.item, c.unit, suggested_packs=0),
        "ck_purchase_order_lines_suggested_packs_positive",
    )
    rejected(
        new_order_line(order, c.item, c.unit, qty_packs=-1),
        "ck_purchase_order_lines_qty_packs_non_negative",
    )
    # The unit must be one of the line's item's units.
    rejected(
        new_order_line(order, c.item, flour_unit),
        "fk_purchase_order_lines_item_id_item_unit_id_item_units",
    )
    # One line per item per order.
    add(db_session, new_order_line(order, c.item, c.unit))
    rejected(new_order_line(order, c.item, c.unit), "uq_purchase_order_lines_po_id_item_id")
    add(db_session, new_order_line(order, stock.flour, flour_unit))


def test_order_rows_cannot_cross_businesses(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    c = stock.cat
    row = new_purchase_order(other.location, c.supplier, business_id=c.business.id)
    assert_rejected(db_session, row, "fk_purchase_orders_business_id_location_id_locations")
    row = new_purchase_order(c.location, other.supplier)
    assert_rejected(db_session, row, "fk_purchase_orders_business_id_supplier_id_suppliers")

    theirs = add(db_session, new_purchase_order(other.location, other.supplier))
    row = new_order_line(theirs, c.item, c.unit, business_id=c.business.id)
    assert_rejected(db_session, row, "fk_purchase_order_lines_business_id_po_id_purchase_orders")
    row = new_order_line(theirs, c.item, c.unit)
    assert_rejected(db_session, row, "fk_purchase_order_lines_business_id_item_id_items")


# --- Receipts against an order ------------------------------------------------


def test_a_receipt_names_an_order_of_its_own_supplier_and_location_once(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    order = add(db_session, new_purchase_order(c.location, c.supplier, **sent()))
    second_location = add(db_session, new_location(c.business.id, name="Second branch"))
    second_supplier = add(db_session, new_supplier(c.business.id, name="Bakery Co"))

    # Another supplier's delivery, or one at another location, can't close it.
    row = new_receipt(c.location, second_supplier, po_id=order.id)
    assert_rejected(db_session, row, "fk_receipts_purchase_order")
    row = new_receipt(second_location, c.supplier, po_id=order.id)
    assert_rejected(db_session, row, "fk_receipts_purchase_order")

    add(db_session, new_receipt(c.location, c.supplier, po_id=order.id))
    # One receipt per order.
    row = new_receipt(c.location, c.supplier, po_id=order.id)
    assert_rejected(db_session, row, "uq_receipts_po_id")


def test_a_receipt_cannot_name_another_business_order(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    c = stock.cat
    theirs = add(db_session, new_purchase_order(other.location, other.supplier, **sent()))
    row = new_receipt(c.location, c.supplier, po_id=theirs.id)
    assert_rejected(db_session, row, "fk_receipts_purchase_order")


# --- Required times -----------------------------------------------------------


def test_raised_at_has_no_default(db_session: Session) -> None:
    """Business time comes from the caller, never from the wall clock."""
    columns = {c["name"]: c for c in inspect(db_session.connection()).get_columns("alerts")}
    assert columns["raised_at"]["nullable"] is False
    assert columns["raised_at"]["default"] is None
