"""Tenancy: businesses, their locations and their users."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.columns import CreatedAt, UuidPk


class Business(Base):
    """The tenant. Every other table carries its id as business_id."""

    __tablename__ = "businesses"
    __table_args__ = (
        # ISO 4217 code, for example JOD.
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_3_letters"),
        CheckConstraint("expiry_warn_days > 0", name="expiry_warn_days_positive"),
    )

    id: Mapped[UuidPk]
    name: Mapped[str]
    currency: Mapped[str]
    # IANA name, for example Asia/Amman.
    timezone: Mapped[str]
    # How many days before expiry a batch counts as expiring.
    expiry_warn_days: Mapped[int] = mapped_column(server_default=text("3"))
    created_at: Mapped[CreatedAt]


class Location(Base):
    """A branch or storeroom. Stock rows carry location_id (D10)."""

    __tablename__ = "locations"
    __table_args__ = (
        CheckConstraint("kind IN ('branch', 'storeroom')", name="kind"),
        # Target for children's composite (business_id, location_id) foreign keys.
        UniqueConstraint("business_id", "id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    name: Mapped[str]
    kind: Mapped[str]
    created_at: Mapped[CreatedAt]


class User(Base):
    """A demo login. Each user belongs to one business."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'staff')", name="role"),
        UniqueConstraint("business_id", "id"),
    )

    id: Mapped[UuidPk]
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id"))
    # Unique across all businesses, so a login finds exactly one user.
    email: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    role: Mapped[str]
    password_hash: Mapped[str]
    created_at: Mapped[CreatedAt]
