"""Documents: goods receipts and their lines."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.columns import CreatedAt, UnitCost, UuidPk


class Receipt(Base):
    """Goods in from one supplier at one location."""

    __tablename__ = "receipts"
    __table_args__ = (
        # NULL means no invoice number; an empty or blank one is a mistake.
        CheckConstraint(
            "invoice_ref IS NULL OR btrim(invoice_ref) <> ''", name="invoice_ref_not_blank"
        ),
        ForeignKeyConstraint(
            ["business_id", "location_id"], ["locations.business_id", "locations.id"]
        ),
        ForeignKeyConstraint(
            ["business_id", "supplier_id"], ["suppliers.business_id", "suppliers.id"]
        ),
        # One invoice can't be entered twice. NULLs are distinct (the Postgres
        # default), so any number of receipts can have no invoice number.
        UniqueConstraint("supplier_id", "invoice_ref"),
        UniqueConstraint("business_id", "id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    supplier_id: Mapped[uuid.UUID]
    # No default: the simulator replays past days, so the caller always sets it.
    received_at: Mapped[datetime]
    invoice_ref: Mapped[str | None]
    created_at: Mapped[CreatedAt]


class ReceiptLine(Base):
    """One item on a receipt, as invoiced and in base units, and the batch it created."""

    __tablename__ = "receipt_lines"
    __table_args__ = (
        CheckConstraint("qty_packs > 0", name="qty_packs_positive"),
        CheckConstraint("qty_base > 0", name="qty_base_positive"),
        CheckConstraint("pack_price >= 0", name="pack_price_non_negative"),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
        ForeignKeyConstraint(
            ["business_id", "receipt_id"], ["receipts.business_id", "receipts.id"]
        ),
        # The pack unit must be one of this item's units.
        ForeignKeyConstraint(["item_id", "item_unit_id"], ["item_units.item_id", "item_units.id"]),
        # The batch is this item's, in this business. This also covers the items
        # key, so there is none.
        ForeignKeyConstraint(
            ["business_id", "item_id", "batch_id"],
            ["batches.business_id", "batches.item_id", "batches.id"],
        ),
        # Target for the movement's receipt-line key, so a receive movement posts
        # to this line's batch.
        UniqueConstraint("business_id", "id", "batch_id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    receipt_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    # NOT NULL: a NULL would switch off the unit foreign key.
    item_unit_id: Mapped[uuid.UUID]
    qty_packs: Mapped[Decimal]
    # qty_packs times the unit's factor_to_base, in the item's base unit.
    qty_base: Mapped[Decimal]
    # Price of one pack, as invoiced.
    pack_price: Mapped[Decimal]
    # Per base unit, copied to the batch and its movements.
    unit_cost: Mapped[UnitCost]
    expires_on: Mapped[date | None]
    lot_code: Mapped[str | None]
    batch_id: Mapped[uuid.UUID]
    created_at: Mapped[CreatedAt]
