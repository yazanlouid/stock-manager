"""Seed a venue's business, location, users, catalogue and par levels.

It writes with the ORM and flushes, so ids exist, but never writes stock and
never commits: the caller owns the transaction (hard rule 3). Stock comes from
the simulator, through the domain functions.

The par levels are written as rows, without evaluating the alerts and draft
orders that follow from them (D59, D67). The simulator's first step is a sweep
at the start of day 0, which evaluates every item, so they take effect there
(D76). Between seed and that sweep, reconcile would report them as missing.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import (
    Business,
    Item,
    ItemUnit,
    Location,
    ParLevel,
    Supplier,
    SupplierItem,
    User,
)
from app.sim.cafe import CAFE, Venue

# Starts with "!", which no password hash format produces, so nobody can log in
# with it. Demo login replaces it later.
PLACEHOLDER_PASSWORD_HASH = "!demo-login-not-set"


def seed(session: Session, venue: Venue = CAFE) -> UUID:
    """Write the venue's tenancy and catalogue rows and return the business id."""
    business = Business(name=venue.name, currency=venue.currency, timezone=venue.timezone)
    session.add(business)
    session.flush()
    business_id = business.id

    location = Location(business_id=business_id, name=venue.location_name, kind=venue.location_kind)
    session.add(location)
    for user in venue.users:
        session.add(
            User(
                business_id=business_id,
                email=user.email.lower(),
                name=user.name,
                role=user.role,
                password_hash=PLACEHOLDER_PASSWORD_HASH,
            )
        )

    suppliers: dict[str, Supplier] = {}
    for spec in venue.suppliers:
        supplier = Supplier(
            business_id=business_id, name=spec.name, lead_time_days=spec.lead_time_days
        )
        session.add(supplier)
        suppliers[spec.key] = supplier
    session.flush()

    for spec in venue.items:
        item = Item(
            business_id=business_id,
            sku=spec.sku,
            name=spec.name,
            category=spec.category,
            base_unit=spec.base_unit,
            tracks_batches=spec.tracks_batches,
            tracks_expiry=spec.tracks_expiry,
            shelf_life_days=spec.shelf_life_days,
        )
        session.add(item)
        session.flush()
        unit = ItemUnit(
            business_id=business_id,
            item_id=item.id,
            name=spec.unit_name,
            factor_to_base=Decimal(spec.factor_to_base),
        )
        session.add(unit)
        session.flush()
        session.add(
            SupplierItem(
                business_id=business_id,
                supplier_id=suppliers[spec.supplier].id,
                item_id=item.id,
                item_unit_id=unit.id,
                pack_price=spec.pack_price,
                is_preferred=True,
            )
        )
        session.add(
            ParLevel(
                business_id=business_id,
                location_id=location.id,
                item_id=item.id,
                reorder_point=Decimal(spec.reorder_point),
                order_up_to=Decimal(spec.order_up_to),
            )
        )
    session.flush()
    return business_id
