"""Item setup endpoints (U10, D90): items, their units, supplier prices and par levels.

Each write calls one domain function in app/domain/items.py on the request's
session, which locks the business row and re-evaluates the item's alerts and
draft lines when an input of theirs changes (D92); get_db commits after the
response is serialized (D42). Every write returns the item's whole setup. An
item that isn't the business's is a 404; any other refusal is the domain's 400
(the handler is in app.main). Writes take no client_id (D94).

GET /catalogue (catalogue.py) still lists the active items for the stock
writes; GET /items lists every item, inactive ones included, with its par
levels.
"""

from collections import defaultdict
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentBusiness, DbSession, Now
from app.api.schemas import (
    ItemIn,
    ItemPatch,
    ItemSetupOut,
    ItemSupplierPriceOut,
    ItemUnitOut,
    ParLevelIn,
    ParLevelOut,
    SupplierPriceIn,
    UnitIn,
    UnitPatch,
)
from app.domain import items as setup
from app.models import Item, ItemUnit, Location, ParLevel, Supplier, SupplierItem

router = APIRouter(tags=["items"])


@router.get("/items")
def get_items(db: DbSession, business: CurrentBusiness) -> list[ItemSetupOut]:
    """Every item of the business, inactive ones included, by SKU."""
    ids = db.scalars(select(Item.id).where(Item.business_id == business.id).order_by(Item.sku))
    return load_items(db, business.id, list(ids))


@router.get("/items/{item_id}")
def get_item(item_id: UUID, db: DbSession, business: CurrentBusiness) -> ItemSetupOut:
    return load_item(db, business.id, item_id)


@router.post("/items", status_code=201)
def post_item(body: ItemIn, db: DbSession, business: CurrentBusiness) -> ItemSetupOut:
    """A new active item, with any units. A SKU the business already uses is a 400."""
    item = setup.create_item(
        db,
        business.id,
        sku=body.sku,
        name=body.name,
        base_unit=body.base_unit,
        category=body.category,
        tracks_batches=body.tracks_batches,
        tracks_expiry=body.tracks_expiry,
        shelf_life_days=body.shelf_life_days,
        units=[setup.UnitInput(unit.name, unit.factor_to_base) for unit in body.units],
    )
    return load_item(db, business.id, item.id)


@router.patch("/items/{item_id}")
def patch_item(
    item_id: UUID, body: ItemPatch, db: DbSession, business: CurrentBusiness, now: Now
) -> ItemSetupOut:
    """Change the fields sent. Deactivating keeps the item's stock and its
    expiry and negative-stock alerts; its low-stock alerts and draft lines go
    (D93)."""
    require_item(db, business.id, item_id)
    setup.update_item(db, business.id, item_id, at=now, **body.model_dump(exclude_unset=True))
    return load_item(db, business.id, item_id)


@router.post("/items/{item_id}/units", status_code=201)
def post_unit(
    item_id: UUID, body: UnitIn, db: DbSession, business: CurrentBusiness
) -> ItemSetupOut:
    """A new pack unit; its factor_to_base can never change (D91)."""
    require_item(db, business.id, item_id)
    setup.add_unit(db, business.id, item_id, name=body.name, factor_to_base=body.factor_to_base)
    return load_item(db, business.id, item_id)


@router.patch("/items/{item_id}/units/{unit_id}")
def patch_unit(
    item_id: UUID, unit_id: UUID, body: UnitPatch, db: DbSession, business: CurrentBusiness
) -> ItemSetupOut:
    require_item(db, business.id, item_id)
    setup.rename_unit(db, business.id, item_id, unit_id, name=body.name)
    return load_item(db, business.id, item_id)


@router.put("/items/{item_id}/supplier-prices")
def put_supplier_price(
    item_id: UUID, body: SupplierPriceIn, db: DbSession, business: CurrentBusiness, now: Now
) -> ItemSetupOut:
    """Set the supplier's price for one of the item's packs, creating it if
    needed; is_preferred true makes it the item's preferred supplier (D94)."""
    require_item(db, business.id, item_id)
    setup.set_supplier_price(
        db,
        business.id,
        item_id,
        supplier_id=body.supplier_id,
        item_unit_id=body.item_unit_id,
        pack_price=body.pack_price,
        is_preferred=body.is_preferred,
        at=now,
    )
    return load_item(db, business.id, item_id)


@router.delete("/items/{item_id}/supplier-prices/{supplier_price_id}")
def delete_supplier_price(
    item_id: UUID, supplier_price_id: UUID, db: DbSession, business: CurrentBusiness, now: Now
) -> ItemSetupOut:
    require_item(db, business.id, item_id)
    setup.remove_supplier_price(db, business.id, item_id, supplier_price_id, at=now)
    return load_item(db, business.id, item_id)


@router.put("/items/{item_id}/par-levels/{location_id}")
def put_par_level(
    item_id: UUID,
    location_id: UUID,
    body: ParLevelIn,
    db: DbSession,
    business: CurrentBusiness,
    now: Now,
) -> ItemSetupOut:
    """Set the item's par level at the location; its alerts and draft line
    there follow at once (D92)."""
    require_item(db, business.id, item_id)
    setup.set_par_level(
        db,
        business.id,
        item_id,
        location_id,
        low_stock_level=body.low_stock_level,
        reorder_point=body.reorder_point,
        order_up_to=body.order_up_to,
        at=now,
    )
    return load_item(db, business.id, item_id)


@router.delete("/items/{item_id}/par-levels/{location_id}")
def delete_par_level(
    item_id: UUID, location_id: UUID, db: DbSession, business: CurrentBusiness, now: Now
) -> ItemSetupOut:
    require_item(db, business.id, item_id)
    setup.remove_par_level(db, business.id, item_id, location_id, at=now)
    return load_item(db, business.id, item_id)


# --- Building the response ------------------------------------------------------


def require_item(db: Session, business_id: UUID, item_id: UUID) -> None:
    """A 404 for an item that isn't the business's, before the domain is called."""
    found = db.scalar(select(Item.id).where(Item.business_id == business_id, Item.id == item_id))
    if found is None:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")


def load_item(db: Session, business_id: UUID, item_id: UUID) -> ItemSetupOut:
    items = load_items(db, business_id, [item_id])
    if not items:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    return items[0]


def load_items(db: Session, business_id: UUID, item_ids: list[UUID]) -> list[ItemSetupOut]:
    """The business's items with these ids, in the order given, with their units
    (by size), supplier prices (by supplier name) and par levels (by location
    name); ids break ties. Four queries, whatever the number of items."""
    if not item_ids:
        return []
    items = {
        item.id: item
        for item in db.scalars(
            select(Item).where(Item.business_id == business_id, Item.id.in_(item_ids))
        )
    }

    units: dict[UUID, list[ItemUnitOut]] = defaultdict(list)
    for unit in db.scalars(
        select(ItemUnit)
        .where(ItemUnit.business_id == business_id, ItemUnit.item_id.in_(item_ids))
        .order_by(ItemUnit.factor_to_base, ItemUnit.name, ItemUnit.id)
    ):
        units[unit.item_id].append(ItemUnitOut.model_validate(unit))

    prices: dict[UUID, list[ItemSupplierPriceOut]] = defaultdict(list)
    for price in db.scalars(
        select(SupplierItem)
        .join(
            Supplier,
            (Supplier.business_id == SupplierItem.business_id)
            & (Supplier.id == SupplierItem.supplier_id),
        )
        .where(SupplierItem.business_id == business_id, SupplierItem.item_id.in_(item_ids))
        .order_by(Supplier.name, Supplier.id, SupplierItem.id)
    ):
        prices[price.item_id].append(ItemSupplierPriceOut.model_validate(price))

    levels: dict[UUID, list[ParLevelOut]] = defaultdict(list)
    for level in db.scalars(
        select(ParLevel)
        .join(
            Location,
            (Location.business_id == ParLevel.business_id) & (Location.id == ParLevel.location_id),
        )
        .where(ParLevel.business_id == business_id, ParLevel.item_id.in_(item_ids))
        .order_by(Location.name, Location.id)
    ):
        levels[level.item_id].append(ParLevelOut.model_validate(level))

    return [
        ItemSetupOut(
            id=item.id,
            sku=item.sku,
            name=item.name,
            category=item.category,
            base_unit=item.base_unit,
            tracks_batches=item.tracks_batches,
            tracks_expiry=item.tracks_expiry,
            shelf_life_days=item.shelf_life_days,
            is_active=item.is_active,
            units=units.get(item.id, []),
            supplier_prices=prices.get(item.id, []),
            par_levels=levels.get(item.id, []),
        )
        for item_id in item_ids
        if (item := items.get(item_id)) is not None
    ]
