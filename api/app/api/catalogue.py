"""GET /catalogue: the ids the write endpoints take (D45). Read-only."""

from collections import defaultdict
from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentBusiness, DbSession
from app.api.schemas import (
    BusinessOut,
    CatalogueItemOut,
    CatalogueOut,
    ItemUnitOut,
    LocationOut,
    SupplierOut,
    SupplierPriceOut,
)
from app.models import Item, ItemUnit, Location, Supplier, SupplierItem

router = APIRouter(tags=["catalogue"])


@router.get("/catalogue")
def get_catalogue(db: DbSession, business: CurrentBusiness) -> CatalogueOut:
    """The business, its locations and suppliers, and its active items.

    Each item lists its pack units and what each supplier charges for it.
    Locations and suppliers are sorted by name, items by SKU, units by size,
    and supplier prices by supplier name, then unit name; ids break ties.
    """
    business_id = business.id

    locations = db.scalars(
        select(Location)
        .where(Location.business_id == business_id)
        .order_by(Location.name, Location.id)
    )
    suppliers = db.scalars(
        select(Supplier)
        .where(Supplier.business_id == business_id)
        .order_by(Supplier.name, Supplier.id)
    )
    items = db.scalars(
        select(Item).where(Item.business_id == business_id, Item.is_active).order_by(Item.sku)
    )

    # Units and prices of every item; an inactive item's are never looked up.
    units: dict[UUID, list[ItemUnitOut]] = defaultdict(list)
    for unit in db.scalars(
        select(ItemUnit)
        .where(ItemUnit.business_id == business_id)
        .order_by(ItemUnit.factor_to_base, ItemUnit.name, ItemUnit.id)
    ):
        units[unit.item_id].append(ItemUnitOut.model_validate(unit))

    prices: dict[UUID, list[SupplierPriceOut]] = defaultdict(list)
    for price in db.scalars(
        select(SupplierItem)
        .join(
            Supplier,
            (Supplier.business_id == SupplierItem.business_id)
            & (Supplier.id == SupplierItem.supplier_id),
        )
        .join(ItemUnit, ItemUnit.id == SupplierItem.item_unit_id)
        .where(
            SupplierItem.business_id == business_id,
            Supplier.business_id == business_id,
            ItemUnit.business_id == business_id,
        )
        # Supplier names aren't unique, so the supplier id keeps the order fixed.
        .order_by(Supplier.name, Supplier.id, ItemUnit.name, SupplierItem.id)
    ):
        prices[price.item_id].append(SupplierPriceOut.model_validate(price))

    return CatalogueOut(
        business=BusinessOut.model_validate(business),
        locations=[LocationOut.model_validate(location) for location in locations],
        suppliers=[SupplierOut.model_validate(supplier) for supplier in suppliers],
        items=[
            CatalogueItemOut(
                id=item.id,
                sku=item.sku,
                name=item.name,
                category=item.category,
                base_unit=item.base_unit,
                tracks_batches=item.tracks_batches,
                tracks_expiry=item.tracks_expiry,
                shelf_life_days=item.shelf_life_days,
                units=units.get(item.id, []),
                supplier_prices=prices.get(item.id, []),
            )
            for item in items
        ],
    )
