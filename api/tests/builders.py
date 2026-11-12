"""Row builders, API request bodies and helpers shared by the tests.

Each row builder returns a valid, unsaved row; keyword arguments override any
column. The cat and other fixtures in conftest.py build a whole catalogue with
them. The *_json builders return API request bodies.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Executable, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    Batch,
    Business,
    Item,
    ItemUnit,
    Location,
    Receipt,
    ReceiptLine,
    StockBalance,
    StockCount,
    StockCountLine,
    StockMovement,
    Supplier,
    SupplierItem,
    User,
)

# Fixed past times: received_at and occurred_at have no server default.
RECEIVED_AT = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)
OCCURRED_AT = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
COUNTED_AT = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)
EXPIRES_ON = date(2026, 11, 9)

# --- Tenancy and catalogue ----------------------------------------------------


def new_business(**overrides: Any) -> Business:
    values = {"name": "Test Café", "currency": "JOD", "timezone": "Asia/Amman"}
    return Business(**(values | overrides))


def new_location(business_id: uuid.UUID, **overrides: Any) -> Location:
    values = {"business_id": business_id, "name": "Main branch", "kind": "branch"}
    return Location(**(values | overrides))


def new_user(business_id: uuid.UUID, **overrides: Any) -> User:
    values = {
        "business_id": business_id,
        "email": f"{uuid.uuid4().hex}@example.com",
        "name": "Test Owner",
        "role": "owner",
        "password_hash": "not-a-real-hash",
    }
    return User(**(values | overrides))


def new_item(business_id: uuid.UUID, **overrides: Any) -> Item:
    values = {
        "business_id": business_id,
        "sku": f"SKU-{uuid.uuid4().hex[:8]}",
        "name": "Whole milk",
        "base_unit": "ml",
    }
    return Item(**(values | overrides))


def new_unit(item: Item, **overrides: Any) -> ItemUnit:
    values = {
        "business_id": item.business_id,
        "item_id": item.id,
        "name": "bottle",
        "factor_to_base": Decimal("1000"),
    }
    return ItemUnit(**(values | overrides))


def new_supplier(business_id: uuid.UUID, **overrides: Any) -> Supplier:
    values = {"business_id": business_id, "name": "Dairy Co"}
    return Supplier(**(values | overrides))


def new_supplier_item(
    supplier: Supplier, item: Item, unit: ItemUnit, **overrides: Any
) -> SupplierItem:
    values = {
        "business_id": supplier.business_id,
        "supplier_id": supplier.id,
        "item_id": item.id,
        "item_unit_id": unit.id,
        "pack_price": Decimal("0.850"),
    }
    return SupplierItem(**(values | overrides))


# --- Stock and documents ------------------------------------------------------


def new_batch(location: Location, item: Item, **overrides: Any) -> Batch:
    values = {
        "business_id": location.business_id,
        "location_id": location.id,
        "item_id": item.id,
        "expires_on": EXPIRES_ON,
        "lot_code": "LOT-1",
        "unit_cost": Decimal("0.000850"),
        "received_at": RECEIVED_AT,
    }
    return Batch(**(values | overrides))


def new_default_batch(location: Location, item: Item, **overrides: Any) -> Batch:
    """The item's default batch at the location.

    received_at is now(), the transaction's start time, which is also what
    created_at defaults to, so the two are equal as the check requires.
    """
    values = {
        "expires_on": None,
        "lot_code": None,
        "unit_cost": None,
        "received_at": func.now(),
        "is_default": True,
    }
    return new_batch(location, item, **(values | overrides))


def new_balance(batch: Batch, **overrides: Any) -> StockBalance:
    values = {
        "business_id": batch.business_id,
        "location_id": batch.location_id,
        "item_id": batch.item_id,
        "batch_id": batch.id,
    }
    return StockBalance(**(values | overrides))


def new_movement(batch: Batch, **overrides: Any) -> StockMovement:
    """A manual usage of 100 base units from the batch, at the batch's cost."""
    values = {
        "business_id": batch.business_id,
        "location_id": batch.location_id,
        "item_id": batch.item_id,
        "batch_id": batch.id,
        "qty_delta": Decimal("-100"),
        "type": "usage",
        "source": "manual",
        "unit_cost": batch.unit_cost,
        "occurred_at": OCCURRED_AT,
        "client_id": uuid.uuid4(),
        "client_seq": 0,
    }
    return StockMovement(**(values | overrides))


def new_receive(batch: Batch, line: ReceiptLine, **overrides: Any) -> StockMovement:
    """The receive movement for a receipt line, into the given batch."""
    values = {
        "type": "receive",
        "qty_delta": line.qty_base,
        "receipt_line_id": line.id,
        "unit_cost": line.unit_cost,
    }
    return new_movement(batch, **(values | overrides))


def new_receipt(location: Location, supplier: Supplier, **overrides: Any) -> Receipt:
    values = {
        "business_id": location.business_id,
        "location_id": location.id,
        "supplier_id": supplier.id,
        "received_at": RECEIVED_AT,
        "invoice_ref": f"INV-{uuid.uuid4().hex[:8]}",
    }
    return Receipt(**(values | overrides))


def new_receipt_line(
    receipt: Receipt, unit: ItemUnit, batch: Batch, **overrides: Any
) -> ReceiptLine:
    """Two packs of the unit, received into the batch."""
    values = {
        "business_id": receipt.business_id,
        "receipt_id": receipt.id,
        "item_id": batch.item_id,
        "item_unit_id": unit.id,
        "qty_packs": Decimal("2"),
        "qty_base": Decimal("2") * unit.factor_to_base,
        "pack_price": Decimal("0.850"),
        "unit_cost": Decimal("0.000850"),
        "expires_on": batch.expires_on,
        "lot_code": batch.lot_code,
        "batch_id": batch.id,
    }
    return ReceiptLine(**(values | overrides))


def new_stock_count(location: Location, **overrides: Any) -> StockCount:
    values = {
        "business_id": location.business_id,
        "location_id": location.id,
        "counted_at": COUNTED_AT,
        "client_id": uuid.uuid4(),
    }
    return StockCount(**(values | overrides))


def new_count_line(count: StockCount, batch: Batch, **overrides: Any) -> StockCountLine:
    """A line of the count for the batch, counted at what was expected (2000)."""
    values = {
        "business_id": count.business_id,
        "count_id": count.id,
        "location_id": count.location_id,
        "item_id": batch.item_id,
        "batch_id": batch.id,
        "expected_qty": Decimal("2000"),
        "counted_qty": Decimal("2000"),
    }
    return StockCountLine(**(values | overrides))


def new_count_adjust(batch: Batch, line: StockCountLine, **overrides: Any) -> StockMovement:
    """A count_adjust for a count line, from the given batch."""
    values = {
        "type": "count_adjust",
        "source": "count",
        "stock_count_line_id": line.id,
        "occurred_at": COUNTED_AT,
    }
    return new_movement(batch, **(values | overrides))


# --- Whole datasets -----------------------------------------------------------


@dataclass
class Catalogue:
    """One business with one row in every tenancy and catalogue table."""

    business: Business
    location: Location
    user: User
    item: Item
    unit: ItemUnit
    supplier: Supplier
    supplier_item: SupplierItem


@dataclass
class Stock:
    """A catalogue plus stock rows for its item, and a second item with none."""

    cat: Catalogue
    # cat.item's default batch at cat.location.
    default_batch: Batch
    # A received batch of cat.item, its receipt and receipt line. The line has no
    # receive movement yet, so tests can post one.
    batch: Batch
    receipt: Receipt
    line: ReceiptLine
    # A usage from batch, and batch's balance.
    usage: StockMovement
    balance: StockBalance
    # An item with no batches, so default-batch tests can't collide with default_batch.
    flour: Item
    # A count at cat.location with a line for batch, counted at what was
    # expected, so the line needs no count_adjust.
    count: StockCount
    count_line: StockCountLine


def add(session: Session, row: Base) -> Any:
    session.add(row)
    session.flush()  # Sends the INSERT, so the server-generated id is available.
    return row


def build_catalogue(session: Session) -> Catalogue:
    business = add(session, new_business())
    item = add(session, new_item(business.id, tracks_batches=True, tracks_expiry=True))
    unit = add(session, new_unit(item))
    supplier = add(session, new_supplier(business.id))
    return Catalogue(
        business=business,
        location=add(session, new_location(business.id)),
        user=add(session, new_user(business.id)),
        item=item,
        unit=unit,
        supplier=supplier,
        supplier_item=add(session, new_supplier_item(supplier, item, unit, is_preferred=True)),
    )


def build_stock(session: Session, cat: Catalogue) -> Stock:
    batch = add(session, new_batch(cat.location, cat.item))
    receipt = add(session, new_receipt(cat.location, cat.supplier))
    count = add(session, new_stock_count(cat.location))
    return Stock(
        cat=cat,
        default_batch=add(session, new_default_batch(cat.location, cat.item)),
        batch=batch,
        receipt=receipt,
        line=add(session, new_receipt_line(receipt, cat.unit, batch)),
        usage=add(session, new_movement(batch)),
        balance=add(session, new_balance(batch)),
        flour=add(session, new_item(cat.business.id, name="Flour", base_unit="g")),
        count=count,
        count_line=add(session, new_count_line(count, batch)),
    )


# --- API request bodies -------------------------------------------------------
#
# Quantities and prices are passed as strings, as the README tells clients to.
# jsonable_encoder turns UUIDs, dates and datetimes into JSON strings.


def api_headers(business: Business | Catalogue) -> dict[str, str]:
    """The X-Business-Id header for a business, or for a catalogue's business."""
    if isinstance(business, Catalogue):
        business = business.business
    return {"X-Business-Id": str(business.id)}


def receipt_line_json(
    item: Item,
    unit: ItemUnit,
    qty_packs: str,
    pack_price: str,
    expires_on: date | None = None,
    lot_code: str | None = None,
) -> dict[str, Any]:
    values = {
        "item_id": item.id,
        "item_unit_id": unit.id,
        "qty_packs": qty_packs,
        "pack_price": pack_price,
        "expires_on": expires_on,
        "lot_code": lot_code,
    }
    return jsonable_encoder(values)


def receipt_json(cat: Catalogue, lines: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """A receipt from cat's supplier at cat's location, received at RECEIVED_AT."""
    values = {
        "client_id": uuid.uuid4(),
        "location_id": cat.location.id,
        "supplier_id": cat.supplier.id,
        "received_at": RECEIVED_AT,
        "invoice_ref": None,
        "lines": lines,
    }
    return jsonable_encoder(values | overrides)


def usage_json(cat: Catalogue, quantity: str, **overrides: Any) -> dict[str, Any]:
    """A usage of cat's item at cat's location, at OCCURRED_AT."""
    values = {
        "client_id": uuid.uuid4(),
        "location_id": cat.location.id,
        "item_id": cat.item.id,
        "quantity": quantity,
        "occurred_at": OCCURRED_AT,
    }
    return jsonable_encoder(values | overrides)


def waste_json(
    cat: Catalogue, quantity: str, batch_id: uuid.UUID | None, **overrides: Any
) -> dict[str, Any]:
    """Spoiled waste of cat's item at cat's location, at OCCURRED_AT."""
    values = {
        "client_id": uuid.uuid4(),
        "location_id": cat.location.id,
        "item_id": cat.item.id,
        "batch_id": batch_id,
        "quantity": quantity,
        "reason": "spoiled",
        "occurred_at": OCCURRED_AT,
    }
    return jsonable_encoder(values | overrides)


def count_item_json(
    item: Item,
    counted_qty: str | None = None,
    batches: list[tuple[uuid.UUID | str, str]] | None = None,
) -> dict[str, Any]:
    """An item counted by quantity (no batch tracking) or by its batches found."""
    values: dict[str, Any] = {"item_id": item.id}
    if counted_qty is not None:
        values["counted_qty"] = counted_qty
    if batches is not None:
        values["batches"] = [{"batch_id": b, "counted_qty": qty} for b, qty in batches]
    return jsonable_encoder(values)


def count_json(cat: Catalogue, items: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """A count at cat's location, at COUNTED_AT."""
    values = {
        "client_id": uuid.uuid4(),
        "location_id": cat.location.id,
        "counted_at": COUNTED_AT,
        "items": items,
    }
    return jsonable_encoder(values | overrides)


def reversal_json(original_client_id: uuid.UUID | str, **overrides: Any) -> dict[str, Any]:
    values = {"client_id": uuid.uuid4(), "original_client_id": original_client_id}
    return jsonable_encoder(values | overrides)


# --- Assertions ---------------------------------------------------------------


def assert_rejected(session: Session, row: Base, constraint: str) -> None:
    """Inserting row fails on the named constraint."""
    with pytest.raises(IntegrityError) as exc_info:
        with session.begin_nested():
            add(session, row)
    assert exc_info.value.orig.diag.constraint_name == constraint


def assert_statement_rejected(session: Session, statement: Executable, constraint: str) -> None:
    """Running statement fails on the named constraint."""
    with pytest.raises(IntegrityError) as exc_info:
        with session.begin_nested():
            session.execute(statement)
    assert exc_info.value.orig.diag.constraint_name == constraint
