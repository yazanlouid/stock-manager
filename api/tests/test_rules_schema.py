"""Database constraints on par_levels and alerts.

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
    new_par_level,
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
    # Reorder only when nothing is left, and a reorder point equal to order_up_to.
    add(db_session, new_par_level(c.location, c.item, reorder_point=Decimal("0")))
    add(
        db_session,
        new_par_level(
            c.location, stock.flour, reorder_point=Decimal("500"), order_up_to=Decimal("500")
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
    pytest.param(
        lambda s: new_par_level(s.cat.location, s.flour, reorder_point=Decimal("-0.001")),
        "ck_par_levels_reorder_point_non_negative",
        id="reorder-point-negative",
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


# --- Required times -----------------------------------------------------------


def test_raised_at_has_no_default(db_session: Session) -> None:
    """Business time comes from the caller, never from the wall clock."""
    columns = {c["name"]: c for c in inspect(db_session.connection()).get_columns("alerts")}
    assert columns["raised_at"]["nullable"] is False
    assert columns["raised_at"]["default"] is None
