"""Database constraints on the stock and document tables, and the append-only trigger.

Same approach as test_catalogue_schema.py: each rejected row runs in a savepoint,
and we check the name of the constraint Postgres reports. Every bad row breaks
exactly one constraint, so the test doesn't depend on the order Postgres checks
them in. The stock fixture (conftest.py) holds a valid row in every table.
"""

import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.orm import Session

from app.db import Base
from app.models import StockMovement
from tests.builders import (
    Catalogue,
    Stock,
    add,
    assert_rejected,
    assert_statement_rejected,
    new_balance,
    new_batch,
    new_count_adjust,
    new_count_line,
    new_default_batch,
    new_location,
    new_movement,
    new_receipt,
    new_receipt_line,
    new_receive,
    new_stock_count,
    new_unit,
)

APPEND_ONLY = "stock_movements_append_only"

# --- Valid rows ---------------------------------------------------------------


def test_valid_stock_rows_get_server_defaults(db_session: Session, stock: Stock) -> None:
    assert stock.batch.id.version == 7  # uuidv7()
    assert stock.batch.is_default is False
    assert stock.balance.qty_on_hand == Decimal("0.000")
    assert stock.balance.last_movement_at is None
    assert stock.usage.created_at.tzinfo is not None  # timestamptz

    # A default batch's received_at is its created_at, and it may have no cost yet.
    db_session.refresh(stock.default_batch)
    assert stock.default_batch.received_at == stock.default_batch.created_at
    assert stock.default_batch.unit_cost is None

    assert stock.count.id.version == 7
    assert stock.count.user_id is None  # simulator counts have no user
    assert stock.count_line.created_at.tzinfo is not None


def test_unit_cost_keeps_six_decimals(db_session: Session, stock: Stock) -> None:
    for row in (stock.batch, stock.line, stock.usage):
        db_session.refresh(row)
        assert row.unit_cost == Decimal("0.000850")
        assert row.unit_cost.as_tuple().exponent == -6  # numeric(18,6)


def test_boundary_values_are_accepted(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    free = add(db_session, new_batch(c.location, c.item, unit_cost=Decimal("0")))
    add(db_session, new_receipt_line(stock.receipt, c.unit, free, pack_price=Decimal("0")))
    add(db_session, new_movement(free, unit_cost=Decimal("0")))
    # A default batch may be costed (after a receipt), and a balance may go negative.
    add(db_session, new_default_batch(c.location, stock.flour, unit_cost=Decimal("0.4")))
    add(db_session, new_balance(stock.default_batch, qty_on_hand=Decimal("-250")))
    # A count line may expect a negative default batch and find nothing.
    add(
        db_session,
        new_count_line(
            stock.count, stock.default_batch, expected_qty=Decimal("-250"), counted_qty=Decimal("0")
        ),
    )


@pytest.mark.parametrize("qty", [Decimal("5"), Decimal("-5")])
def test_count_adjust_and_settle_take_either_sign(
    db_session: Session, stock: Stock, qty: Decimal
) -> None:
    add(db_session, new_count_adjust(stock.batch, stock.count_line, qty_delta=qty))
    add(db_session, new_movement(stock.batch, type="settle", qty_delta=qty))


def test_movement_without_user_or_cost_is_accepted(db_session: Session, stock: Stock) -> None:
    # Simulator movements have no user; a default batch may have no cost yet.
    add(db_session, new_movement(stock.default_batch, source="simulator", user_id=None))


def test_movement_by_a_user_of_the_business_is_accepted(db_session: Session, stock: Stock) -> None:
    add(db_session, new_movement(stock.batch, user_id=stock.cat.user.id))


# --- Each constraint rejects a bad row ----------------------------------------

BadRow = Callable[[Stock], Base]


def self_reversal(s: Stock) -> StockMovement:
    """A reversal whose reverses_id is its own id."""
    movement_id = uuid.uuid4()
    return new_movement(s.batch, id=movement_id, reverses_id=movement_id, type="reversal")


BAD_ROWS: list[Any] = [
    # batches
    pytest.param(
        lambda s: new_default_batch(s.cat.location, s.flour, expires_on=s.batch.expires_on),
        "ck_batches_default_has_no_expiry_or_lot",
        id="default-batch-with-expiry",
    ),
    pytest.param(
        lambda s: new_default_batch(s.cat.location, s.flour, lot_code="LOT-9"),
        "ck_batches_default_has_no_expiry_or_lot",
        id="default-batch-with-lot-code",
    ),
    pytest.param(
        lambda s: new_default_batch(s.cat.location, s.flour, received_at=s.batch.received_at),
        "ck_batches_default_received_at_is_created_at",
        id="default-batch-received-at-not-created-at",
    ),
    pytest.param(
        lambda s: new_batch(s.cat.location, s.cat.item, unit_cost=None),
        "ck_batches_unit_cost_required_unless_default",
        id="received-batch-without-cost",
    ),
    pytest.param(
        lambda s: new_batch(s.cat.location, s.cat.item, unit_cost=Decimal("-0.000001")),
        "ck_batches_unit_cost_non_negative",
        id="batch-cost-negative",
    ),
    pytest.param(
        lambda s: new_default_batch(s.cat.location, s.cat.item),
        "uq_batches_one_default_per_location_item",
        id="second-default-batch",
    ),
    # stock_movements
    pytest.param(
        lambda s: new_movement(s.batch, type="transfer"),
        "ck_stock_movements_type",
        id="movement-type",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, source="pos"),
        "ck_stock_movements_source",
        id="movement-source",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, type="settle", qty_delta=Decimal("0")),
        "ck_stock_movements_qty_delta_non_zero",
        id="qty-delta-zero",
    ),
    pytest.param(
        lambda s: new_receive(s.batch, s.line, qty_delta=Decimal("-1")),
        "ck_stock_movements_qty_delta_sign",
        id="receive-negative",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, qty_delta=Decimal("1")),
        "ck_stock_movements_qty_delta_sign",
        id="usage-positive",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, type="waste", reason="expired", qty_delta=Decimal("1")),
        "ck_stock_movements_qty_delta_sign",
        id="waste-positive",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, type="waste", reason="stolen"),
        "ck_stock_movements_reason",
        id="waste-reason",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, type="waste"),
        "ck_stock_movements_reason_iff_waste",
        id="waste-without-reason",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, reason="spoiled"),
        "ck_stock_movements_reason_iff_waste",
        id="usage-with-reason",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, type="reversal", qty_delta=Decimal("100")),
        "ck_stock_movements_reverses_id_iff_reversal",
        id="reversal-without-reverses-id",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, reverses_id=s.usage.id),
        "ck_stock_movements_reverses_id_iff_reversal",
        id="usage-with-reverses-id",
    ),
    pytest.param(
        lambda s: self_reversal(s),
        "ck_stock_movements_not_self_reversal",
        id="reverses-itself",
    ),
    pytest.param(
        lambda s: new_receive(s.batch, s.line, receipt_line_id=None),
        "ck_stock_movements_receipt_line_id_iff_receive",
        id="receive-without-receipt-line",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, receipt_line_id=s.line.id),
        "ck_stock_movements_receipt_line_id_iff_receive",
        id="usage-with-receipt-line",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, type="count_adjust", qty_delta=Decimal("5")),
        "ck_stock_movements_stock_count_line_id_iff_count_adjust",
        id="count-adjust-without-count-line",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, stock_count_line_id=s.count_line.id),
        "ck_stock_movements_stock_count_line_id_iff_count_adjust",
        id="usage-with-count-line",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, unit_cost=Decimal("-0.000001")),
        "ck_stock_movements_unit_cost_non_negative",
        id="movement-cost-negative",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, client_seq=-1),
        "ck_stock_movements_client_seq_non_negative",
        id="client-seq-negative",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, client_id=s.usage.client_id, client_seq=0),
        "uq_stock_movements_business_id_client_id_client_seq",
        id="repeated-client-id-and-seq",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, item_id=s.flour.id),
        "fk_stock_movements_batch",
        id="movement-item-not-batch-item",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, batch_id=uuid.uuid4()),
        "fk_stock_movements_batch",
        id="movement-unknown-batch",
    ),
    pytest.param(
        lambda s: new_movement(s.batch, user_id=uuid.uuid4()),
        "fk_stock_movements_business_id_user_id_users",
        id="movement-unknown-user",
    ),
    # stock_balances
    pytest.param(
        lambda s: new_balance(s.batch),
        "uq_stock_balances_batch_id",
        id="second-balance-for-batch",
    ),
    pytest.param(
        lambda s: new_balance(s.default_batch, item_id=s.flour.id),
        "fk_stock_balances_batch",
        id="balance-item-not-batch-item",
    ),
    # receipts
    pytest.param(
        lambda s: new_receipt(s.cat.location, s.cat.supplier, invoice_ref=""),
        "ck_receipts_invoice_ref_not_blank",
        id="invoice-ref-empty",
    ),
    pytest.param(
        lambda s: new_receipt(s.cat.location, s.cat.supplier, invoice_ref="   "),
        "ck_receipts_invoice_ref_not_blank",
        id="invoice-ref-blank",
    ),
    pytest.param(
        lambda s: new_receipt(s.cat.location, s.cat.supplier, invoice_ref=s.receipt.invoice_ref),
        "uq_receipts_supplier_id_invoice_ref",
        id="repeated-invoice-ref",
    ),
    # receipt_lines
    pytest.param(
        lambda s: new_receipt_line(s.receipt, s.cat.unit, s.batch, qty_packs=Decimal("0")),
        "ck_receipt_lines_qty_packs_positive",
        id="qty-packs-zero",
    ),
    pytest.param(
        lambda s: new_receipt_line(s.receipt, s.cat.unit, s.batch, qty_base=Decimal("0")),
        "ck_receipt_lines_qty_base_positive",
        id="qty-base-zero",
    ),
    pytest.param(
        lambda s: new_receipt_line(s.receipt, s.cat.unit, s.batch, pack_price=Decimal("-0.001")),
        "ck_receipt_lines_pack_price_non_negative",
        id="pack-price-negative",
    ),
    pytest.param(
        lambda s: new_receipt_line(s.receipt, s.cat.unit, s.batch, unit_cost=Decimal("-0.000001")),
        "ck_receipt_lines_unit_cost_non_negative",
        id="line-cost-negative",
    ),
    pytest.param(
        lambda s: new_receipt_line(s.receipt, s.cat.unit, s.batch, receipt_id=uuid.uuid4()),
        "fk_receipt_lines_business_id_receipt_id_receipts",
        id="line-unknown-receipt",
    ),
    # stock_counts
    pytest.param(
        lambda s: new_stock_count(s.cat.location, client_id=s.count.client_id),
        "uq_stock_counts_business_id_client_id",
        id="repeated-count-client-id",
    ),
    pytest.param(
        lambda s: new_stock_count(s.cat.location, location_id=uuid.uuid4()),
        "fk_stock_counts_business_id_location_id_locations",
        id="count-unknown-location",
    ),
    pytest.param(
        lambda s: new_stock_count(s.cat.location, user_id=uuid.uuid4()),
        "fk_stock_counts_business_id_user_id_users",
        id="count-unknown-user",
    ),
    # stock_count_lines
    pytest.param(
        lambda s: new_count_line(s.count, s.default_batch, counted_qty=Decimal("-0.001")),
        "ck_stock_count_lines_counted_qty_non_negative",
        id="counted-qty-negative",
    ),
    pytest.param(
        lambda s: new_count_line(s.count, s.batch),
        "uq_stock_count_lines_count_id_batch_id",
        id="second-line-for-batch",
    ),
    pytest.param(
        lambda s: new_count_line(s.count, s.default_batch, count_id=uuid.uuid4()),
        "fk_stock_count_lines_count",
        id="count-line-unknown-count",
    ),
    pytest.param(
        lambda s: new_count_line(s.count, s.default_batch, item_id=s.flour.id),
        "fk_stock_count_lines_batch",
        id="count-line-item-not-batch-item",
    ),
]


@pytest.mark.parametrize(("bad_row", "constraint"), BAD_ROWS)
def test_constraint_rejects_bad_row(
    db_session: Session, stock: Stock, bad_row: BadRow, constraint: str
) -> None:
    assert_rejected(db_session, bad_row(stock), constraint)


# --- Append-only ledger -------------------------------------------------------


def test_movements_can_be_inserted(db_session: Session, stock: Stock) -> None:
    movement = add(db_session, new_movement(stock.batch, type="waste", reason="spoiled"))
    found = db_session.scalar(select(StockMovement).where(StockMovement.id == movement.id))
    assert found is movement


def test_update_of_a_movement_is_rejected(db_session: Session, stock: Stock) -> None:
    statement = (
        update(StockMovement)
        .where(StockMovement.id == stock.usage.id)
        .values(qty_delta=Decimal("-1"))
    )
    assert_statement_rejected(db_session, statement, APPEND_ONLY)


def test_delete_of_a_movement_is_rejected(db_session: Session, stock: Stock) -> None:
    statement = delete(StockMovement).where(StockMovement.id == stock.usage.id)
    assert_statement_rejected(db_session, statement, APPEND_ONLY)


def test_truncate_of_movements_is_rejected(db_session: Session, stock: Stock) -> None:
    assert_statement_rejected(db_session, text("TRUNCATE stock_movements"), APPEND_ONLY)


# --- Batches ------------------------------------------------------------------


def test_default_batches_for_other_items_and_locations_are_accepted(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    store = add(db_session, new_location(c.business.id, name="Storeroom", kind="storeroom"))
    add(db_session, new_default_batch(c.location, stock.flour))
    add(db_session, new_default_batch(store, c.item))


def test_batch_cannot_use_another_business_location(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_batch(other.location, stock.cat.item, business_id=stock.cat.business.id)
    assert_rejected(db_session, row, "fk_batches_business_id_location_id_locations")


def test_batch_cannot_use_another_business_item(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_batch(stock.cat.location, other.item)
    assert_rejected(db_session, row, "fk_batches_business_id_item_id_items")


# --- Movements and their batch ------------------------------------------------


def test_movement_cannot_post_to_a_batch_at_another_location(
    db_session: Session, stock: Stock
) -> None:
    c = stock.cat
    store = add(db_session, new_location(c.business.id, name="Storeroom", kind="storeroom"))
    row = new_movement(stock.batch, location_id=store.id)
    assert_rejected(db_session, row, "fk_stock_movements_batch")


def test_movement_cannot_post_to_another_business_batch(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    row = new_movement(other_stock.batch, business_id=stock.cat.business.id)
    assert_rejected(db_session, row, "fk_stock_movements_batch")


def test_balance_cannot_belong_to_another_business_batch(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    row = new_balance(other_stock.default_batch, business_id=stock.cat.business.id)
    assert_rejected(db_session, row, "fk_stock_balances_batch")


def test_movement_cannot_name_another_business_user(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_movement(stock.batch, user_id=other.user.id)
    assert_rejected(db_session, row, "fk_stock_movements_business_id_user_id_users")


# --- Receive movements and receipt lines --------------------------------------


def test_receive_movement_for_its_line_is_accepted(db_session: Session, stock: Stock) -> None:
    add(db_session, new_receive(stock.batch, stock.line))


def test_receipt_line_cannot_get_a_second_receive(db_session: Session, stock: Stock) -> None:
    add(db_session, new_receive(stock.batch, stock.line))
    row = new_receive(stock.batch, stock.line, client_seq=1)
    assert_rejected(db_session, row, "uq_stock_movements_receipt_line_id")


def test_receive_must_post_to_its_line_batch(db_session: Session, stock: Stock) -> None:
    # Same item and location, so only the receipt-line key fails.
    row = new_receive(stock.default_batch, stock.line)
    assert_rejected(db_session, row, "fk_stock_movements_receipt_line")


def test_receive_cannot_use_another_business_line(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    # A valid movement in the other business, pointing at this business's line.
    row = new_receive(other_stock.batch, stock.line)
    assert_rejected(db_session, row, "fk_stock_movements_receipt_line")


def test_receipt_line_unit_must_belong_to_the_line_item(db_session: Session, stock: Stock) -> None:
    # The line's item is its batch's, so only the unit key fails.
    sack = add(db_session, new_unit(stock.flour, name="sack", factor_to_base=Decimal("25000")))
    row = new_receipt_line(stock.receipt, sack, stock.batch)
    assert_rejected(db_session, row, "fk_receipt_lines_item_id_item_unit_id_item_units")


def test_receipt_line_batch_must_be_the_line_item(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    flour_batch = add(db_session, new_batch(c.location, stock.flour))
    # The unit belongs to the line's item, so only the batch key fails.
    row = new_receipt_line(stock.receipt, c.unit, flour_batch, item_id=c.item.id)
    assert_rejected(db_session, row, "fk_receipt_lines_business_id_item_id_batch_id_batches")


def test_receipt_line_cannot_use_another_business_batch(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    row = new_receipt_line(
        stock.receipt, stock.cat.unit, other_stock.batch, item_id=stock.cat.item.id
    )
    assert_rejected(db_session, row, "fk_receipt_lines_business_id_item_id_batch_id_batches")


def test_receipt_line_cannot_use_another_business_receipt(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    row = new_receipt_line(
        other_stock.receipt, stock.cat.unit, stock.batch, business_id=stock.cat.business.id
    )
    assert_rejected(db_session, row, "fk_receipt_lines_business_id_receipt_id_receipts")


# --- Stock counts, their lines and count adjustments ---------------------------


def test_count_adjust_for_its_line_is_accepted(db_session: Session, stock: Stock) -> None:
    add(db_session, new_count_adjust(stock.batch, stock.count_line))


def test_count_line_cannot_get_a_second_count_adjust(db_session: Session, stock: Stock) -> None:
    add(db_session, new_count_adjust(stock.batch, stock.count_line))
    row = new_count_adjust(stock.batch, stock.count_line, client_seq=1)
    assert_rejected(db_session, row, "uq_stock_movements_stock_count_line_id")


def test_count_adjust_must_post_to_its_line_batch(db_session: Session, stock: Stock) -> None:
    # Same item and location, so only the count-line key fails.
    row = new_count_adjust(stock.default_batch, stock.count_line)
    assert_rejected(db_session, row, "fk_stock_movements_stock_count_line")


def test_count_adjust_cannot_use_another_business_line(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    # A valid movement in the other business, pointing at this business's line.
    row = new_count_adjust(other_stock.batch, stock.count_line)
    assert_rejected(db_session, row, "fk_stock_movements_stock_count_line")


def test_count_line_must_be_at_its_count_location(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    store = add(db_session, new_location(c.business.id, name="Storeroom", kind="storeroom"))
    store_batch = add(db_session, new_batch(store, c.item))
    # The batch is at the line's location, so only the count key fails.
    row = new_count_line(stock.count, store_batch, location_id=store.id)
    assert_rejected(db_session, row, "fk_stock_count_lines_count")


def test_count_line_cannot_use_another_business_count(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    # The batch key holds (this business's batch at this location), so only
    # the count key fails.
    row = new_count_line(
        other_stock.count,
        stock.default_batch,
        business_id=stock.cat.business.id,
        location_id=stock.cat.location.id,
    )
    assert_rejected(db_session, row, "fk_stock_count_lines_count")


def test_count_line_cannot_use_another_business_batch(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    row = new_count_line(stock.count, other_stock.batch, item_id=stock.cat.item.id)
    assert_rejected(db_session, row, "fk_stock_count_lines_batch")


def test_count_cannot_use_another_business_location(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_stock_count(other.location, business_id=stock.cat.business.id)
    assert_rejected(db_session, row, "fk_stock_counts_business_id_location_id_locations")


def test_count_cannot_name_another_business_user(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_stock_count(stock.cat.location, user_id=other.user.id)
    assert_rejected(db_session, row, "fk_stock_counts_business_id_user_id_users")


def test_count_by_a_user_of_the_business_is_accepted(db_session: Session, stock: Stock) -> None:
    add(db_session, new_stock_count(stock.cat.location, user_id=stock.cat.user.id))


def test_same_count_client_id_in_another_business_is_accepted(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    add(db_session, new_stock_count(other.location, client_id=stock.count.client_id))


# --- Receipts -----------------------------------------------------------------


def test_receipts_without_invoice_ref_are_accepted(db_session: Session, stock: Stock) -> None:
    c = stock.cat
    add(db_session, new_receipt(c.location, c.supplier, invoice_ref=None))
    add(db_session, new_receipt(c.location, c.supplier, invoice_ref=None))


def test_same_invoice_ref_from_another_supplier_is_accepted(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    add(
        db_session,
        new_receipt(other.location, other.supplier, invoice_ref=stock.receipt.invoice_ref),
    )


def test_receipt_cannot_use_another_business_supplier(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_receipt(stock.cat.location, other.supplier)
    assert_rejected(db_session, row, "fk_receipts_business_id_supplier_id_suppliers")


def test_receipt_cannot_use_another_business_location(
    db_session: Session, stock: Stock, other: Catalogue
) -> None:
    row = new_receipt(other.location, stock.cat.supplier, business_id=stock.cat.business.id)
    assert_rejected(db_session, row, "fk_receipts_business_id_location_id_locations")


# --- Reversals ----------------------------------------------------------------


def new_reversal(stock: Stock, **overrides: Any) -> StockMovement:
    """A reversal of stock.usage."""
    values = {"type": "reversal", "qty_delta": Decimal("100"), "reverses_id": stock.usage.id}
    return new_movement(stock.batch, **(values | overrides))


def test_movement_can_be_reversed_once(db_session: Session, stock: Stock) -> None:
    add(db_session, new_reversal(stock))
    assert_rejected(db_session, new_reversal(stock), "uq_stock_movements_reverses_id")


def test_reversal_of_an_unknown_movement_is_rejected(db_session: Session, stock: Stock) -> None:
    row = new_reversal(stock, reverses_id=uuid.uuid4())
    assert_rejected(db_session, row, "fk_stock_movements_business_id_reverses_id_stock_movements")


def test_reversal_cannot_reverse_another_business_movement(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    row = new_reversal(stock, reverses_id=other_stock.usage.id)
    assert_rejected(db_session, row, "fk_stock_movements_business_id_reverses_id_stock_movements")


# --- Idempotency key ----------------------------------------------------------


def test_same_client_id_with_next_seq_is_accepted(db_session: Session, stock: Stock) -> None:
    add(db_session, new_movement(stock.batch, client_id=stock.usage.client_id, client_seq=1))


def test_same_client_id_and_seq_in_another_business_is_accepted(
    db_session: Session, stock: Stock, other_stock: Stock
) -> None:
    usage = stock.usage
    add(db_session, new_movement(other_stock.batch, client_id=usage.client_id, client_seq=0))


# --- Required times -----------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("stock_movements", "occurred_at"),
        ("stock_movements", "client_seq"),
        ("batches", "received_at"),
        ("receipts", "received_at"),
        ("stock_counts", "counted_at"),
    ],
)
def test_caller_set_columns_have_no_default(db_session: Session, table: str, column: str) -> None:
    """A caller that forgets these fails instead of getting wall-clock time or 0."""
    columns = {c["name"]: c for c in inspect(db_session.connection()).get_columns(table)}
    assert columns[column]["nullable"] is False
    assert columns[column]["default"] is None
