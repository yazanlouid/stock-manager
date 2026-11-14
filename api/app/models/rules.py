"""Rules: par levels, and the alerts raised from stock and par levels."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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

    When usable on-hand falls to reorder_point the item is low, and a draft
    order brings it back up to order_up_to (domain rule 8).
    """

    __tablename__ = "par_levels"
    __table_args__ = (
        CheckConstraint("reorder_point >= 0", name="reorder_point_non_negative"),
        # Ordering up to nothing would never order anything.
        CheckConstraint("order_up_to > 0", name="order_up_to_positive"),
        CheckConstraint("reorder_point <= order_up_to", name="reorder_point_at_most_order_up_to"),
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
