"""Rules: par levels, the alerts and the purchase orders raised from them."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.columns import CreatedAt, UuidPk


class ParLevel(Base):
    """The reorder rule for one item at one location, in the item's base unit.

    When usable on-hand falls to reorder_point, a draft order brings it back up
    to order_up_to (domain rule 8). When it falls further, to low_stock_level,
    the item is short and has a low_stock alert (D83).
    """

    __tablename__ = "par_levels"
    __table_args__ = (
        CheckConstraint("reorder_point >= 0", name="reorder_point_non_negative"),
        # Ordering up to nothing would never order anything.
        CheckConstraint("order_up_to > 0", name="order_up_to_positive"),
        CheckConstraint("reorder_point <= order_up_to", name="reorder_point_at_most_order_up_to"),
        CheckConstraint("low_stock_level >= 0", name="low_stock_level_non_negative"),
        CheckConstraint(
            "low_stock_level <= reorder_point", name="low_stock_level_at_most_reorder_point"
        ),
        ForeignKeyConstraint(
            ["business_id", "location_id"], ["locations.business_id", "locations.id"]
        ),
        ForeignKeyConstraint(["business_id", "item_id"], ["items.business_id", "items.id"]),
        # One rule per item and location.
        UniqueConstraint("location_id", "item_id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    reorder_point: Mapped[Decimal]
    order_up_to: Mapped[Decimal]
    low_stock_level: Mapped[Decimal]
    created_at: Mapped[CreatedAt]


class Alert(Base):
    """One episode of something that needs attention (D60).

    expiring, expired and negative_stock are about one batch (negative_stock
    about the item's default batch); low_stock is about the item at the
    location and has no batch. Rows are written only by app/domain/alerts.py:
    an alert is raised open, resolved once, and never reopened, so resolved
    rows are history.
    """

    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(
            "type IN ('low_stock', 'expiring', 'expired', 'negative_stock')", name="type"
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="status"),
        CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)", name="resolved_at_iff_resolved"
        ),
        # NULL (still open) passes, as CHECK only rejects false.
        CheckConstraint("resolved_at >= raised_at", name="resolved_at_not_before_raised_at"),
        CheckConstraint(
            "(type = 'low_stock') = (batch_id IS NULL)", name="batch_id_unless_low_stock"
        ),
        ForeignKeyConstraint(
            ["business_id", "location_id"], ["locations.business_id", "locations.id"]
        ),
        ForeignKeyConstraint(["business_id", "item_id"], ["items.business_id", "items.id"]),
        # The batch is the alert's item at the alert's location, in this
        # business. A low_stock alert has no batch, and a foreign key with a
        # NULL column isn't checked, which is why the location and item have
        # keys of their own above.
        ForeignKeyConstraint(
            ["business_id", "location_id", "item_id", "batch_id"],
            ["batches.business_id", "batches.location_id", "batches.item_id", "batches.id"],
        ),
        # At most one open alert per location, item, type and batch.
        # NULLS NOT DISTINCT makes two low_stock alerts (no batch) collide too.
        # Location and item come first, so the index also finds an item's open
        # alerts when a write re-evaluates them.
        Index(
            "uq_alerts_one_open",
            "location_id",
            "item_id",
            "type",
            "batch_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            postgresql_nulls_not_distinct=True,
        ),
        # GET /alerts lists a business's alerts newest first.
        Index("ix_alerts_business_raised_at", "business_id", "raised_at"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    batch_id: Mapped[uuid.UUID | None]
    type: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    # Business time, with no default: the write's occurred_at, or the time the
    # sweep ran as of, so simulated days get simulated times.
    raised_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]


class PurchaseOrder(Base):
    """An order to one supplier for one location: a draft, then sent, then closed (D69).

    Each supplier has at most one draft per location, which the par-level rule
    fills (domain rule 8). Nothing is sent to the supplier: sent means the
    owner placed the order, and from then its lines count as on order until a
    receipt names it (received) or the owner gives up on it (cancelled).
    """

    __tablename__ = "purchase_orders"
    __table_args__ = (
        CheckConstraint("status IN ('draft', 'sent', 'received', 'cancelled')", name="status"),
        # Each status has its time, and only its own: a draft has none, and an
        # order that was sent keeps sent_at when it is received or cancelled.
        CheckConstraint("(status = 'draft') = (sent_at IS NULL)", name="sent_at_unless_draft"),
        CheckConstraint(
            "(status = 'received') = (received_at IS NOT NULL)", name="received_at_iff_received"
        ),
        CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL)", name="cancelled_at_iff_cancelled"
        ),
        ForeignKeyConstraint(
            ["business_id", "location_id"], ["locations.business_id", "locations.id"]
        ),
        ForeignKeyConstraint(
            ["business_id", "supplier_id"], ["suppliers.business_id", "suppliers.id"]
        ),
        # Target for the lines' order key.
        UniqueConstraint("business_id", "id"),
        # Target for the receipts' order key, so a receipt against an order is
        # from the order's supplier at the order's location.
        UniqueConstraint("business_id", "id", "location_id", "supplier_id"),
        # One draft per supplier and location; any number of sent and closed orders.
        Index(
            "uq_purchase_orders_one_draft_per_location_supplier",
            "location_id",
            "supplier_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    supplier_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(server_default=text("'draft'"))
    # Business time, set by the caller: the simulator replays past days.
    sent_at: Mapped[datetime | None]
    received_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]


class PurchaseOrderLine(Base):
    """One item on an order, in whole packs of one of the item's units (D70)."""

    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        CheckConstraint("suggested_packs > 0", name="suggested_packs_positive"),
        # NULL (as suggested) passes, as CHECK only rejects false.
        CheckConstraint("qty_packs >= 0", name="qty_packs_non_negative"),
        ForeignKeyConstraint(
            ["business_id", "po_id"], ["purchase_orders.business_id", "purchase_orders.id"]
        ),
        ForeignKeyConstraint(["business_id", "item_id"], ["items.business_id", "items.id"]),
        # The pack unit must be one of this item's units.
        ForeignKeyConstraint(["item_id", "item_unit_id"], ["item_units.item_id", "item_units.id"]),
        # One line per item per order.
        UniqueConstraint("po_id", "item_id"),
        # What is on order for an item: its lines on sent orders.
        Index("ix_purchase_order_lines_item", "business_id", "item_id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    po_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    # NOT NULL: a NULL would switch off the unit foreign key.
    item_unit_id: Mapped[uuid.UUID]
    # Pack counts are bigint: the largest order_up_to over the smallest
    # factor_to_base (both numeric(14,3)) is about 10^14 packs, past integer's
    # two billion, and a suggestion that didn't fit would fail the stock
    # write that computed it.
    # The rule's number: what brings the item back to order_up_to, in whole packs.
    suggested_packs: Mapped[int] = mapped_column(BigInteger)
    # The owner's number. On a draft, NULL means "as suggested" and 0 means
    # "don't order this"; sending fills it in, so a sent order's lines all have it.
    qty_packs: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[CreatedAt]
