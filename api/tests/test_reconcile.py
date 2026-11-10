"""The reconcile check: silent on a ledger built by the domain functions, and
catching each kind of problem.

Each broken ledger is built inside the test's rolled-back transaction by
inserting rows with tests/builders.py, or by editing a stock_balances row (a
projection, so a test may break it). No test updates or deletes a movement
(hard rule 1). Each one asserts that the whole result is exactly the one
problem it built, so a broken state can't hide behind another.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.domain.reconcile import reconcile
from app.domain.stock import ReceiptLineInput, receive, reverse, use, waste
from app.models import StockBalance
from tests.builders import (
    OCCURRED_AT,
    Catalogue,
    add,
    new_balance,
    new_batch,
    new_default_batch,
    new_item,
    new_movement,
    new_receipt,
    new_receipt_line,
    new_receive,
    new_unit,
)


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def receive_milk(db: Session, cat: Catalogue, packs: str, expires_on: date, when: datetime):
    return receive(
        db,
        cat.business.id,
        location_id=cat.location.id,
        supplier_id=cat.supplier.id,
        received_at=when,
        invoice_ref=None,
        lines=[
            ReceiptLineInput(cat.item.id, cat.unit.id, Decimal(packs), Decimal("0.850"), expires_on)
        ],
        client_id=uuid.uuid4(),
        source="manual",
    )


def kinds(db: Session, cat: Catalogue) -> list[str]:
    return [problem.kind for problem in reconcile(db, cat.business.id)]


def test_a_ledger_built_by_the_domain_functions_reconciles(db_session: Session, cat: Catalogue):
    business_id, location_id = cat.business.id, cat.location.id
    receive_milk(db_session, cat, "2", date(2026, 11, 9), at(2, 6))
    receive_milk(db_session, cat, "3", date(2026, 11, 12), at(2, 7))
    # Splits across both batches, then falls short onto the default batch.
    use(
        db_session, business_id, location_id=location_id, item_id=cat.item.id,
        quantity=Decimal("2500"), occurred_at=at(2, 9), client_id=uuid.uuid4(), source="manual",
    )  # fmt: skip
    use(
        db_session, business_id, location_id=location_id, item_id=cat.item.id,
        quantity=Decimal("3000"), occurred_at=at(2, 10), client_id=uuid.uuid4(), source="manual",
    )  # fmt: skip
    # The next receipt settles the shortfall.
    later = receive_milk(db_session, cat, "4", date(2026, 11, 14), at(3, 6))
    assert any(m.type == "settle" for m in later.movements)
    wasted_from = later.movements[0].batch_id
    mistake = uuid.uuid4()
    waste(
        db_session, business_id, location_id=location_id, item_id=cat.item.id,
        batch_id=wasted_from, quantity=Decimal("100"), reason="spoiled", occurred_at=at(3, 12),
        client_id=mistake, source="manual",
    )  # fmt: skip
    reverse(
        db_session, business_id, original_client_id=mistake, client_id=uuid.uuid4(),
        source="manual",
    )  # fmt: skip

    # An item without batch tracking: receive into, use from and waste from its default batch.
    flour = add(db_session, new_item(business_id, name="Flour", base_unit="g"))
    bag = add(db_session, new_unit(flour, name="bag", factor_to_base=Decimal("1000")))
    receive(
        db_session, business_id, location_id=location_id, supplier_id=cat.supplier.id,
        received_at=at(2, 6), invoice_ref="INV-FLOUR",
        lines=[ReceiptLineInput(flour.id, bag.id, Decimal("5"), Decimal("1.200"))],
        client_id=uuid.uuid4(), source="manual",
    )  # fmt: skip
    use(
        db_session, business_id, location_id=location_id, item_id=flour.id,
        quantity=Decimal("1250"), occurred_at=at(2, 11), client_id=uuid.uuid4(), source="manual",
    )  # fmt: skip
    waste(
        db_session, business_id, location_id=location_id, item_id=flour.id, batch_id=None,
        quantity=Decimal("50"), reason="damaged", occurred_at=at(2, 12),
        client_id=uuid.uuid4(), source="manual",
    )  # fmt: skip

    assert reconcile(db_session, business_id) == []


def test_a_balance_that_differs_from_the_ledger(db_session: Session, cat: Catalogue):
    result = receive_milk(db_session, cat, "2", date(2026, 11, 9), at(2, 6))
    db_session.execute(
        update(StockBalance)
        .where(StockBalance.batch_id == result.movements[0].batch_id)
        .values(qty_on_hand=StockBalance.qty_on_hand - 1)
    )

    problems = reconcile(db_session, cat.business.id)

    assert [p.kind for p in problems] == ["balance_mismatch"]
    assert "1999.000" in problems[0].message and "2000.000" in problems[0].message


def test_a_non_default_batch_below_zero(db_session: Session, cat: Catalogue):
    default = add(db_session, new_default_batch(cat.location, cat.item))
    add(db_session, new_balance(default))
    batch = add(db_session, new_batch(cat.location, cat.item))
    add(db_session, new_movement(batch, qty_delta=Decimal("-100")))
    # The balance matches the ledger, so only the negative batch is wrong.
    add(db_session, new_balance(batch, qty_on_hand=Decimal("-100"), last_movement_at=OCCURRED_AT))

    problems = reconcile(db_session, cat.business.id)

    assert [p.kind for p in problems] == ["negative_batch"]
    assert cat.item.sku in problems[0].message


def test_an_item_with_batches_but_no_default_batch(db_session: Session, cat: Catalogue):
    batch = add(db_session, new_batch(cat.location, cat.item))
    add(db_session, new_balance(batch))

    problems = reconcile(db_session, cat.business.id)

    assert [p.kind for p in problems] == ["default_batch_count"]
    assert "has 0 default batches" in problems[0].message


def build_receipt_line(db: Session, cat: Catalogue):
    """A default batch and a received batch, with balances, and a line with no movement yet."""
    add(db, new_balance(add(db, new_default_batch(cat.location, cat.item))))
    batch = add(db, new_batch(cat.location, cat.item))
    receipt = add(db, new_receipt(cat.location, cat.supplier))
    line = add(db, new_receipt_line(receipt, cat.unit, batch))
    return batch, line


def test_a_receipt_line_without_its_receive_movement(db_session: Session, cat: Catalogue):
    batch, _ = build_receipt_line(db_session, cat)
    add(db_session, new_balance(batch))

    assert kinds(db_session, cat) == ["receipt_line_unposted"]


def test_a_receive_movement_with_the_wrong_quantity(db_session: Session, cat: Catalogue):
    batch, line = build_receipt_line(db_session, cat)
    quantity = line.qty_base - 1
    add(db_session, new_receive(batch, line, qty_delta=quantity))
    add(db_session, new_balance(batch, qty_on_hand=quantity, last_movement_at=OCCURRED_AT))

    problems = reconcile(db_session, cat.business.id)

    assert [p.kind for p in problems] == ["receipt_line_mismatch"]
    assert "is 2000.000 at" in problems[0].message and "is 1999.000 at" in problems[0].message


def test_a_receive_movement_with_the_wrong_cost(db_session: Session, cat: Catalogue):
    batch, line = build_receipt_line(db_session, cat)
    add(db_session, new_receive(batch, line, unit_cost=Decimal("0.000900")))
    add(db_session, new_balance(batch, qty_on_hand=line.qty_base, last_movement_at=OCCURRED_AT))

    problems = reconcile(db_session, cat.business.id)

    assert [p.kind for p in problems] == ["receipt_line_mismatch"]
    assert "0.000900" in problems[0].message


def test_problems_are_scoped_to_the_business(db_session: Session, cat: Catalogue, other: Catalogue):
    # The other business has an item with batches but no default batch.
    batch = add(db_session, new_batch(other.location, other.item))
    add(db_session, new_balance(batch))

    assert kinds(db_session, cat) == []
    assert kinds(db_session, other) == ["default_batch_count"]
