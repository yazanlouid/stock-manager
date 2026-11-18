"""Item setup: items, their units, supplier prices and par levels (U10, D90 to D94).

Like the other domain functions, these work on the caller's Session, never
commit (hard rule 3), scope every lookup by business_id, and raise DomainError
for a request they refuse. They take no client_id (D94): updates are
idempotent, and a retried create meets the unique key it created.

Locking (D92). Every setup write first locks the business row exclusively,
as an order action does (lock_business in orders.py): stock writes, sweeps and
order actions in progress finish first, and new ones wait. With stock and
orders standing still, a setup write can check "has this item moved?" and
re-evaluate the item's rules without locking its batches. Setup is rare, so
the coarse lock costs nothing that matters.

Rules follow setup (D92). Alerts and draft lines are projections of balances,
par levels, items and the preferred supplier. A write here that changes one
of those inputs re-evaluates the item through evaluate_rules, in the same
transaction, at the time it is given: a par level at its location, and
anything about the item or its supplier at every location of the business.

What can't change once used (D91): an item's base_unit and tracking, once it
has a movement; a unit's factor_to_base, ever. Nothing here deletes an item
or a unit; an item is deactivated instead (D93).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.domain.checks import (
    check_counted_quantity,
    check_money,
    check_quantity,
    check_timestamp,
)
from app.domain.errors import DomainError
from app.domain.orders import lock_business
from app.domain.rules import evaluate_rules
from app.models import (
    Item,
    ItemUnit,
    Location,
    ParLevel,
    StockMovement,
    Supplier,
    SupplierItem,
)

BASE_UNITS = ("g", "ml", "piece")


@dataclass(frozen=True, slots=True)
class UnitInput:
    name: str
    factor_to_base: Decimal


# Marks an update field the caller didn't send, so None can mean "no shelf
# life" or "no category".
class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


# --- Items ---------------------------------------------------------------------


def create_item(
    session: Session,
    business_id: UUID,
    *,
    sku: str,
    name: str,
    base_unit: str,
    category: str | None = None,
    tracks_batches: bool = False,
    tracks_expiry: bool = False,
    shelf_life_days: int | None = None,
    units: Sequence[UnitInput] = (),
) -> Item:
    """A new active item, with any pack units. It has no par level and no
    stock, so there is nothing to evaluate yet."""
    lock_business(session, business_id)
    sku = _text(sku, "sku")
    name = _text(name, "name")
    _check_base_unit(base_unit)
    _check_tracking(tracks_batches, tracks_expiry)
    _check_shelf_life(shelf_life_days)
    _check_sku_free(session, business_id, sku, None)
    unit_names = [_text(unit.name, "unit name") for unit in units]
    if len(set(unit_names)) != len(unit_names):
        raise DomainError("two units have the same name")
    for unit in units:
        check_quantity(unit.factor_to_base, "factor_to_base")

    item = Item(
        business_id=business_id,
        sku=sku,
        name=name,
        category=_optional_text(category),
        base_unit=base_unit,
        tracks_batches=tracks_batches,
        tracks_expiry=tracks_expiry,
        shelf_life_days=shelf_life_days,
        is_active=True,
    )
    session.add(item)
    session.flush()
    for unit_name, unit in zip(unit_names, units, strict=True):
        session.add(
            ItemUnit(
                business_id=business_id,
                item_id=item.id,
                name=unit_name,
                factor_to_base=unit.factor_to_base,
            )
        )
    session.flush()
    return item


def update_item(
    session: Session,
    business_id: UUID,
    item_id: UUID,
    *,
    at: datetime,
    sku: str | _Unset = UNSET,
    name: str | _Unset = UNSET,
    category: str | None | _Unset = UNSET,
    shelf_life_days: int | None | _Unset = UNSET,
    is_active: bool | _Unset = UNSET,
    base_unit: str | _Unset = UNSET,
    tracks_batches: bool | _Unset = UNSET,
    tracks_expiry: bool | _Unset = UNSET,
) -> Item:
    """Change the fields given. base_unit and tracking only while the item has
    no movements (D91). A change of is_active or shelf_life_days re-evaluates
    the item at every location (D92, D93)."""
    check_timestamp(at, "at")
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)

    if not isinstance(sku, _Unset):
        sku = _text(sku, "sku")
        _check_sku_free(session, business_id, sku, item.id)
        item.sku = sku
    if not isinstance(name, _Unset):
        item.name = _text(name, "name")
    if not isinstance(category, _Unset):
        item.category = _optional_text(category)

    new_base = item.base_unit if isinstance(base_unit, _Unset) else base_unit
    new_batches = item.tracks_batches if isinstance(tracks_batches, _Unset) else tracks_batches
    new_expiry = item.tracks_expiry if isinstance(tracks_expiry, _Unset) else tracks_expiry
    if (new_base, new_batches, new_expiry) != (
        item.base_unit,
        item.tracks_batches,
        item.tracks_expiry,
    ):
        _check_base_unit(new_base)
        _check_tracking(new_batches, new_expiry)
        if _has_moved(session, business_id, item.id):
            raise DomainError(
                f"item {item.sku} has movements, so its base unit and tracking can't change"
            )
        item.base_unit, item.tracks_batches, item.tracks_expiry = (
            new_base,
            new_batches,
            new_expiry,
        )

    rules_change = False
    if not isinstance(shelf_life_days, _Unset) and shelf_life_days != item.shelf_life_days:
        _check_shelf_life(shelf_life_days)
        item.shelf_life_days = shelf_life_days
        rules_change = True
    if not isinstance(is_active, _Unset) and is_active != item.is_active:
        if not isinstance(is_active, bool):
            raise TypeError(f"is_active must be a bool, not {type(is_active).__name__}")
        item.is_active = is_active
        rules_change = True

    session.flush()
    if rules_change:
        _evaluate_everywhere(session, business_id, item.id, at)
    return item


# --- Units ---------------------------------------------------------------------


def add_unit(
    session: Session, business_id: UUID, item_id: UUID, *, name: str, factor_to_base: Decimal
) -> ItemUnit:
    """A new pack unit for the item. Its factor is fixed from now on (D91)."""
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)
    name = _text(name, "unit name")
    check_quantity(factor_to_base, "factor_to_base")
    _check_unit_name_free(session, business_id, item.id, name, None)
    unit = ItemUnit(
        business_id=business_id, item_id=item.id, name=name, factor_to_base=factor_to_base
    )
    session.add(unit)
    session.flush()
    return unit


def rename_unit(
    session: Session, business_id: UUID, item_id: UUID, unit_id: UUID, *, name: str
) -> ItemUnit:
    """Rename one of the item's units; its factor never changes (D91)."""
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)
    unit = _get_unit(session, business_id, item.id, unit_id)
    name = _text(name, "unit name")
    _check_unit_name_free(session, business_id, item.id, name, unit.id)
    unit.name = name
    session.flush()
    return unit


# --- Supplier prices -----------------------------------------------------------


def set_supplier_price(
    session: Session,
    business_id: UUID,
    item_id: UUID,
    *,
    supplier_id: UUID,
    item_unit_id: UUID,
    pack_price: Decimal,
    is_preferred: bool,
    at: datetime,
) -> SupplierItem:
    """Set what the supplier charges for one of the item's packs (D94).

    Creates the row if the supplier doesn't sell the item in that unit yet.
    is_preferred true makes it the item's one preferred supplier price,
    unsetting the other; false only unsets this one. The draft lines follow
    the preferred supplier and its pack unit, so a change of either
    re-evaluates the item everywhere (D92).
    """
    check_timestamp(at, "at")
    check_money(pack_price, "pack_price")
    if not isinstance(is_preferred, bool):
        raise TypeError(f"is_preferred must be a bool, not {type(is_preferred).__name__}")
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)
    _get_supplier(session, business_id, supplier_id)
    _get_unit(session, business_id, item.id, item_unit_id)
    before = _preferred(session, business_id, item.id)

    price = session.scalar(
        select(SupplierItem).where(
            SupplierItem.business_id == business_id,
            SupplierItem.item_id == item.id,
            SupplierItem.supplier_id == supplier_id,
            SupplierItem.item_unit_id == item_unit_id,
        )
    )
    if is_preferred and before is not None and before.id != getattr(price, "id", None):
        # The partial unique index allows one preferred row per item, so the
        # old one is unset, and flushed, before this one is set.
        before.is_preferred = False
        session.flush()
    if price is None:
        price = SupplierItem(
            business_id=business_id,
            supplier_id=supplier_id,
            item_id=item.id,
            item_unit_id=item_unit_id,
            pack_price=pack_price,
            is_preferred=is_preferred,
        )
        session.add(price)
    else:
        price.pack_price = pack_price
        price.is_preferred = is_preferred
    session.flush()

    after = _preferred(session, business_id, item.id)
    if _preference(before) != _preference(after):
        _evaluate_everywhere(session, business_id, item.id, at)
    return price


def remove_supplier_price(
    session: Session, business_id: UUID, item_id: UUID, supplier_price_id: UUID, *, at: datetime
) -> None:
    """Stop listing the item from that supplier in that unit. Removing the
    preferred one leaves the item with no preferred supplier, so its draft
    lines go until another is chosen (D94)."""
    check_timestamp(at, "at")
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)
    price = session.scalar(
        select(SupplierItem).where(
            SupplierItem.business_id == business_id,
            SupplierItem.item_id == item.id,
            SupplierItem.id == supplier_price_id,
        )
    )
    if price is None:
        raise DomainError(f"supplier price {supplier_price_id} not found for item {item.sku}")
    was_preferred = price.is_preferred
    session.delete(price)
    session.flush()
    if was_preferred:
        _evaluate_everywhere(session, business_id, item.id, at)


# --- Par levels ----------------------------------------------------------------


def set_par_level(
    session: Session,
    business_id: UUID,
    item_id: UUID,
    location_id: UUID,
    *,
    low_stock_level: Decimal,
    reorder_point: Decimal,
    order_up_to: Decimal,
    at: datetime,
) -> ParLevel:
    """Set the item's par level at the location, creating it if needed, and
    re-evaluate the item there (D92): it may become low, or go onto a draft."""
    check_timestamp(at, "at")
    check_counted_quantity(low_stock_level, "low_stock_level")
    check_counted_quantity(reorder_point, "reorder_point")
    check_quantity(order_up_to, "order_up_to")
    if not low_stock_level <= reorder_point <= order_up_to:
        raise DomainError(
            "the levels must be in order: low_stock_level <= reorder_point <= order_up_to,"
            f" got {low_stock_level}, {reorder_point} and {order_up_to}"
        )
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)
    _get_location(session, business_id, location_id)
    par = _get_par_level(session, business_id, item.id, location_id)
    if par is None:
        par = ParLevel(business_id=business_id, location_id=location_id, item_id=item.id)
        session.add(par)
    par.low_stock_level = low_stock_level
    par.reorder_point = reorder_point
    par.order_up_to = order_up_to
    session.flush()
    evaluate_rules(session, business_id, [(location_id, item.id)], at)
    return par


def remove_par_level(
    session: Session, business_id: UUID, item_id: UUID, location_id: UUID, *, at: datetime
) -> None:
    """Remove the item's par level at the location: its low_stock alert there
    resolves and its draft line goes (D92)."""
    check_timestamp(at, "at")
    lock_business(session, business_id)
    item = _get_item(session, business_id, item_id)
    _get_location(session, business_id, location_id)
    par = _get_par_level(session, business_id, item.id, location_id)
    if par is None:
        raise DomainError(f"item {item.sku} has no par level at location {location_id}")
    session.delete(par)
    session.flush()
    evaluate_rules(session, business_id, [(location_id, item.id)], at)


# --- Helpers -------------------------------------------------------------------


def _evaluate_everywhere(session: Session, business_id: UUID, item_id: UUID, at: datetime) -> None:
    """Re-evaluate the item's alerts and draft lines at every location (D92)."""
    locations = session.scalars(
        select(Location.id).where(Location.business_id == business_id).order_by(Location.id)
    )
    evaluate_rules(session, business_id, [(location_id, item_id) for location_id in locations], at)


def _has_moved(session: Session, business_id: UUID, item_id: UUID) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    StockMovement.business_id == business_id, StockMovement.item_id == item_id
                )
            )
        )
    )


def _preferred(session: Session, business_id: UUID, item_id: UUID) -> SupplierItem | None:
    return session.scalar(
        select(SupplierItem).where(
            SupplierItem.business_id == business_id,
            SupplierItem.item_id == item_id,
            SupplierItem.is_preferred,
        )
    )


def _preference(price: SupplierItem | None) -> tuple[UUID, UUID] | None:
    """What the draft lines read from the preferred supplier price."""
    return None if price is None else (price.supplier_id, price.item_unit_id)


def _get_item(session: Session, business_id: UUID, item_id: UUID) -> Item:
    item = session.scalar(select(Item).where(Item.business_id == business_id, Item.id == item_id))
    if item is None:
        raise DomainError(f"item {item_id} not found")
    return item


def _get_unit(session: Session, business_id: UUID, item_id: UUID, unit_id: UUID) -> ItemUnit:
    unit = session.scalar(
        select(ItemUnit).where(
            ItemUnit.business_id == business_id,
            ItemUnit.item_id == item_id,
            ItemUnit.id == unit_id,
        )
    )
    if unit is None:
        raise DomainError(f"unit {unit_id} not found for this item")
    return unit


def _get_supplier(session: Session, business_id: UUID, supplier_id: UUID) -> Supplier:
    supplier = session.scalar(
        select(Supplier).where(Supplier.business_id == business_id, Supplier.id == supplier_id)
    )
    if supplier is None:
        raise DomainError(f"supplier {supplier_id} not found")
    return supplier


def _get_location(session: Session, business_id: UUID, location_id: UUID) -> Location:
    location = session.scalar(
        select(Location).where(Location.business_id == business_id, Location.id == location_id)
    )
    if location is None:
        raise DomainError(f"location {location_id} not found")
    return location


def _get_par_level(
    session: Session, business_id: UUID, item_id: UUID, location_id: UUID
) -> ParLevel | None:
    return session.scalar(
        select(ParLevel).where(
            ParLevel.business_id == business_id,
            ParLevel.item_id == item_id,
            ParLevel.location_id == location_id,
        )
    )


def _check_sku_free(session: Session, business_id: UUID, sku: str, item_id: UUID | None) -> None:
    taken = session.scalar(select(Item.id).where(Item.business_id == business_id, Item.sku == sku))
    if taken is not None and taken != item_id:
        raise DomainError(f"SKU {sku} is already used by another item")


def _check_unit_name_free(
    session: Session, business_id: UUID, item_id: UUID, name: str, unit_id: UUID | None
) -> None:
    taken = session.scalar(
        select(ItemUnit.id).where(
            ItemUnit.business_id == business_id,
            ItemUnit.item_id == item_id,
            ItemUnit.name == name,
        )
    )
    if taken is not None and taken != unit_id:
        raise DomainError(f"the item already has a unit named {name}")


def _check_base_unit(base_unit: str) -> None:
    if base_unit not in BASE_UNITS:
        raise DomainError(f"base_unit must be one of {', '.join(BASE_UNITS)}, got {base_unit!r}")


def _check_tracking(tracks_batches: bool, tracks_expiry: bool) -> None:
    for name, value in (("tracks_batches", tracks_batches), ("tracks_expiry", tracks_expiry)):
        if not isinstance(value, bool):
            raise TypeError(f"{name} must be a bool, not {type(value).__name__}")
    if tracks_expiry and not tracks_batches:
        raise DomainError("an item that tracks expiry must track batches: expiry is per batch")


def _check_shelf_life(shelf_life_days: int | None) -> None:
    if shelf_life_days is None:
        return
    if isinstance(shelf_life_days, bool) or not isinstance(shelf_life_days, int):
        raise TypeError(f"shelf_life_days must be an int, not {type(shelf_life_days).__name__}")
    if shelf_life_days <= 0:
        raise DomainError(f"shelf_life_days must be positive, got {shelf_life_days}")


def _text(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise DomainError(f"{name} must not be empty")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None
