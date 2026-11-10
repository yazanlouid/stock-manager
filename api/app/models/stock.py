"""Stock: batches, the stock_movements ledger and the stock_balances projection."""

import uuid
from datetime import date, datetime
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
from app.models.columns import CreatedAt, UnitCost, UuidPk


class Batch(Base):
    """A received lot of one item at one location, or that item's default batch.

    Every item has one default batch per location (domain rule 5). It has no
    expiry or lot code and takes any shortfall, so it may go negative (rule 4).
    """

    __tablename__ = "batches"
    __table_args__ = (
        CheckConstraint(
            "NOT is_default OR (expires_on IS NULL AND lot_code IS NULL)",
            name="default_has_no_expiry_or_lot",
        ),
        # A default batch isn't received, so it takes the time it was created. The
        # caller passes now(), which is the transaction's start time, like created_at.
        CheckConstraint(
            "NOT is_default OR received_at = created_at",
            name="default_received_at_is_created_at",
        ),
        # Only a default batch may have no cost yet (nothing received into it).
        CheckConstraint(
            "is_default OR unit_cost IS NOT NULL", name="unit_cost_required_unless_default"
        ),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
        ForeignKeyConstraint(
            ["business_id", "location_id"], ["locations.business_id", "locations.id"]
        ),
        ForeignKeyConstraint(["business_id", "item_id"], ["items.business_id", "items.id"]),
        # Target for the movement and balance batch keys: their location and item
        # must be the batch's.
        UniqueConstraint("business_id", "location_id", "item_id", "id"),
        # Target for the receipt-line batch key: the batch must be the line's item.
        UniqueConstraint("business_id", "item_id", "id"),
        Index(
            "uq_batches_one_default_per_location_item",
            "location_id",
            "item_id",
            unique=True,
            postgresql_where=text("is_default"),
        ),
        # FEFO order (domain rule 2). Ascending order already puts NULL expiry
        # (the default batch, items without expiry) last.
        Index("ix_batches_fefo", "location_id", "item_id", "expires_on", "received_at"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    expires_on: Mapped[date | None]
    lot_code: Mapped[str | None]
    unit_cost: Mapped[UnitCost | None]
    # No default: the simulator replays past days, so the caller always sets it.
    received_at: Mapped[datetime]
    is_default: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[CreatedAt]


class StockMovement(Base):
    """One row of the ledger: a signed quantity in or out of one batch.

    Append-only (hard rule 1): a trigger in the migration rejects UPDATE, DELETE
    and TRUNCATE. Mistakes are fixed with a reversal movement.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint(
            "type IN ('receive', 'usage', 'waste', 'count_adjust', 'settle', 'reversal')",
            name="type",
        ),
        CheckConstraint("source IN ('manual', 'count', 'simulator')", name="source"),
        CheckConstraint("qty_delta <> 0", name="qty_delta_non_zero"),
        # Receive adds stock; usage and waste remove it. count_adjust, settle and
        # reversal take either sign.
        CheckConstraint(
            "(type <> 'receive' OR qty_delta > 0)"
            " AND (type NOT IN ('usage', 'waste') OR qty_delta < 0)",
            name="qty_delta_sign",
        ),
        # NULL passes; reason_iff_waste decides when a reason is required.
        CheckConstraint(
            "reason IN ('expired', 'spoiled', 'damaged', 'prep_error', 'overproduction', 'other')",
            name="reason",
        ),
        CheckConstraint("(type = 'waste') = (reason IS NOT NULL)", name="reason_iff_waste"),
        CheckConstraint(
            "(type = 'reversal') = (reverses_id IS NOT NULL)", name="reverses_id_iff_reversal"
        ),
        # The self-referencing foreign key would accept a row that reverses itself.
        CheckConstraint("reverses_id <> id", name="not_self_reversal"),
        CheckConstraint(
            "(type = 'receive') = (receipt_line_id IS NOT NULL)",
            name="receipt_line_id_iff_receive",
        ),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
        CheckConstraint("client_seq >= 0", name="client_seq_non_negative"),
        # The movement's location and item are its batch's, in the same business.
        # This also covers the locations and items keys, so there are none.
        # Named by hand: the convention's name is longer than Postgres's 63 characters.
        ForeignKeyConstraint(
            ["business_id", "location_id", "item_id", "batch_id"],
            ["batches.business_id", "batches.location_id", "batches.item_id", "batches.id"],
            name="fk_stock_movements_batch",
        ),
        # A receive posts to its receipt line's own batch, in the same business.
        ForeignKeyConstraint(
            ["business_id", "receipt_line_id", "batch_id"],
            ["receipt_lines.business_id", "receipt_lines.id", "receipt_lines.batch_id"],
            name="fk_stock_movements_receipt_line",
        ),
        ForeignKeyConstraint(
            ["business_id", "reverses_id"],
            ["stock_movements.business_id", "stock_movements.id"],
        ),
        ForeignKeyConstraint(["business_id", "user_id"], ["users.business_id", "users.id"]),
        # Target for the reverses_id key.
        UniqueConstraint("business_id", "id"),
        # A retried request writes the same (client_id, client_seq) pairs, so the
        # retry fails instead of posting twice.
        UniqueConstraint("business_id", "client_id", "client_seq"),
        # History and reports for one item.
        Index("ix_stock_movements_item_history", "business_id", "item_id", "occurred_at"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    batch_id: Mapped[uuid.UUID]
    # In the item's base unit (hard rule 4).
    qty_delta: Mapped[Decimal]
    type: Mapped[str]
    source: Mapped[str]
    reason: Mapped[str | None]
    # Copied from the batch (domain rule 7), so NULL while the batch has no cost.
    unit_cost: Mapped[UnitCost | None]
    # Unique: a receipt line gets at most one receive movement.
    receipt_line_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    # Unique: a movement can be reversed once (domain rule 6).
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    # When it happened, set by the caller; created_at is when the server wrote it.
    occurred_at: Mapped[datetime]
    # NULL for simulator movements.
    user_id: Mapped[uuid.UUID | None]
    # Identifies the request; every movement it writes shares it.
    client_id: Mapped[uuid.UUID]
    # Orders the request's movements: 0, 1, 2... No default, so it can't be forgotten.
    client_seq: Mapped[int]
    created_at: Mapped[CreatedAt]


class StockBalance(Base):
    """On-hand for one batch: a projection of its movements (domain rule 1).

    Written in the same transaction as each movement, and rebuildable from the ledger.
    """

    __tablename__ = "stock_balances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["business_id", "location_id", "item_id", "batch_id"],
            ["batches.business_id", "batches.location_id", "batches.item_id", "batches.id"],
            name="fk_stock_balances_batch",
        ),
        Index("ix_stock_balances_location_item", "location_id", "item_id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    location_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    # Unique: one balance row per batch.
    batch_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    # No sign check: the default batch may go negative (domain rule 4).
    qty_on_hand: Mapped[Decimal] = mapped_column(server_default=text("0"))
    last_movement_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
