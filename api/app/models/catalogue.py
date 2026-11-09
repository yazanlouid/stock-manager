"""Catalogue: items, their pack units, suppliers and what each supplier sells."""

import uuid
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


class Item(Base):
    """Anything stocked. Quantities are always in its base_unit."""

    __tablename__ = "items"
    __table_args__ = (
        CheckConstraint("base_unit IN ('g', 'ml', 'piece')", name="base_unit"),
        # Expiry lives on the batch, so an item can't track expiry without batches.
        CheckConstraint("NOT tracks_expiry OR tracks_batches", name="expiry_requires_batches"),
        # NULL (no shelf life) passes, as CHECK only rejects false.
        CheckConstraint("shelf_life_days > 0", name="shelf_life_days_positive"),
        UniqueConstraint("business_id", "sku"),
        UniqueConstraint("business_id", "id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    sku: Mapped[str]
    name: Mapped[str]
    category: Mapped[str | None]
    base_unit: Mapped[str]
    tracks_batches: Mapped[bool] = mapped_column(server_default=text("false"))
    tracks_expiry: Mapped[bool] = mapped_column(server_default=text("false"))
    shelf_life_days: Mapped[int | None]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[CreatedAt]


class ItemUnit(Base):
    """A pack conversion for one item, for example crate = 12000 g."""

    __tablename__ = "item_units"
    __table_args__ = (
        CheckConstraint("factor_to_base > 0", name="factor_to_base_positive"),
        ForeignKeyConstraint(["business_id", "item_id"], ["items.business_id", "items.id"]),
        UniqueConstraint("item_id", "name"),
        # Target for (item_id, item_unit_id) foreign keys: the unit must belong to that item.
        UniqueConstraint("item_id", "id"),
        UniqueConstraint("business_id", "id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    item_id: Mapped[uuid.UUID]
    name: Mapped[str]
    # Base units in one of this unit.
    factor_to_base: Mapped[Decimal]
    created_at: Mapped[CreatedAt]


class Supplier(Base):
    """Someone the business buys from."""

    __tablename__ = "suppliers"
    __table_args__ = (
        CheckConstraint("lead_time_days >= 0", name="lead_time_days_non_negative"),
        UniqueConstraint("business_id", "id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    name: Mapped[str]
    phone: Mapped[str | None]
    lead_time_days: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[CreatedAt]


class SupplierItem(Base):
    """An item a supplier sells, in one pack unit, at one pack price."""

    __tablename__ = "supplier_items"
    __table_args__ = (
        CheckConstraint("pack_price >= 0", name="pack_price_non_negative"),
        ForeignKeyConstraint(
            ["business_id", "supplier_id"], ["suppliers.business_id", "suppliers.id"]
        ),
        ForeignKeyConstraint(["business_id", "item_id"], ["items.business_id", "items.id"]),
        # The pack unit must be one of this item's units. Since the item belongs to
        # this business, so does the unit.
        ForeignKeyConstraint(["item_id", "item_unit_id"], ["item_units.item_id", "item_units.id"]),
        UniqueConstraint("supplier_id", "item_id", "item_unit_id"),
        # At most one preferred supplier per item.
        Index(
            "uq_supplier_items_one_preferred_per_item",
            "item_id",
            unique=True,
            postgresql_where=text("is_preferred"),
        ),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    supplier_id: Mapped[uuid.UUID]
    item_id: Mapped[uuid.UUID]
    # NOT NULL: a NULL would switch off both the unit foreign key and the unique constraint.
    item_unit_id: Mapped[uuid.UUID]
    # Price of one pack, in the business's currency.
    pack_price: Mapped[Decimal]
    is_preferred: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[CreatedAt]
