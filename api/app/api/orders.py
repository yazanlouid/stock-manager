"""Order endpoints: read orders, set a draft line's quantity, send and cancel (D73).

The par-level rule fills each supplier's draft; these let the owner see the
orders and act on them. Receiving against a sent order is POST /receipts with
a po_id (app/api/stock.py). Each action calls one domain function on the
request's session, and get_db commits after the response is serialized (D42).
A DomainError is a 400 (the handler is in app.main).

Like GET /alerts, reading orders first runs the sweep when the business's
local date has moved past its alerts date (D62): stock that expired overnight
is no longer usable, which can put an item on a draft.
"""

from collections import defaultdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentBusiness, DbSession, Now
from app.api.schemas import OrderLineIn, OrderLineOut, OrderOut
from app.domain.orders import cancel_order, order_packs, send_order, set_line_quantity
from app.domain.rules import sweep_if_due
from app.models import (
    Item,
    ItemUnit,
    Location,
    PurchaseOrder,
    PurchaseOrderLine,
    Receipt,
    Supplier,
    SupplierItem,
)

router = APIRouter(tags=["orders"])

OrderStatus = Literal["draft", "sent", "received", "cancelled"]


@router.get("/orders")
def get_orders(
    db: DbSession,
    business: CurrentBusiness,
    now: Now,
    status: OrderStatus | None = None,
    supplier_id: UUID | None = None,
    location_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[OrderOut]:
    """The business's orders with their lines, newest first.

    Newest first is by id: ids are UUIDv7, so that is the order they were
    created in. A draft with no lines is left out: it is only the row the
    rule keeps to put the supplier's next lines on (D67). A supplier_id or
    location_id that isn't the business's matches nothing.
    """
    business_id = business.id
    sweep_if_due(db, business_id, now)

    has_lines = exists().where(
        PurchaseOrderLine.business_id == PurchaseOrder.business_id,
        PurchaseOrderLine.po_id == PurchaseOrder.id,
    )
    order_ids = (
        select(PurchaseOrder.id)
        .where(PurchaseOrder.business_id == business_id, has_lines)
        .order_by(PurchaseOrder.id.desc())
        .limit(limit)
    )
    if status is not None:
        order_ids = order_ids.where(PurchaseOrder.status == status)
    if supplier_id is not None:
        order_ids = order_ids.where(PurchaseOrder.supplier_id == supplier_id)
    if location_id is not None:
        order_ids = order_ids.where(PurchaseOrder.location_id == location_id)
    return load_orders(db, business_id, list(db.scalars(order_ids)))


@router.get("/orders/{order_id}")
def get_order(order_id: UUID, db: DbSession, business: CurrentBusiness, now: Now) -> OrderOut:
    """One order with its lines; a 404 if it isn't the business's."""
    sweep_if_due(db, business.id, now)
    return load_order(db, business.id, order_id)


@router.put("/orders/{order_id}/lines/{line_id}")
def put_order_line(
    order_id: UUID, line_id: UUID, body: OrderLineIn, db: DbSession, business: CurrentBusiness
) -> OrderOut:
    """Set how many packs a draft's line orders; null goes back to the suggestion.

    0 leaves the item out when the draft is sent. Only a draft's lines can
    be changed.
    """
    set_line_quantity(db, business.id, order_id=order_id, line_id=line_id, qty_packs=body.qty_packs)
    return load_order(db, business.id, order_id)


@router.post("/orders/{order_id}/send")
def post_order_send(order_id: UUID, db: DbSession, business: CurrentBusiness, now: Now) -> OrderOut:
    """Mark a draft as sent, at the server's clock: its lines now count as on order.

    Nothing goes to the supplier; the owner places the order themselves.
    Sending again is a 400, because the order is no longer a draft.
    """
    send_order(db, business.id, order_id=order_id, sent_at=now)
    return load_order(db, business.id, order_id)


@router.post("/orders/{order_id}/cancel")
def post_order_cancel(
    order_id: UUID, db: DbSession, business: CurrentBusiness, now: Now
) -> OrderOut:
    """Cancel a sent order that won't be delivered, at the server's clock.

    Whatever is still low goes onto the supplier's next draft.
    """
    cancel_order(db, business.id, order_id=order_id, cancelled_at=now)
    return load_order(db, business.id, order_id)


# --- Building the response ------------------------------------------------------


def load_order(db: Session, business_id: UUID, order_id: UUID) -> OrderOut:
    orders = load_orders(db, business_id, [order_id])
    if not orders:
        raise HTTPException(status_code=404, detail=f"order {order_id} not found")
    return orders[0]


def load_orders(db: Session, business_id: UUID, order_ids: list[UUID]) -> list[OrderOut]:
    """The business's orders with these ids, in the order given, each with its lines.

    Two queries: the orders with their supplier, location and receipt, and
    every line with its item, its unit and what the supplier charges now.
    """
    if not order_ids:
        return []

    lines: dict[UUID, list[OrderLineOut]] = defaultdict(list)
    line_rows = db.execute(
        select(
            PurchaseOrderLine,
            Item.sku,
            Item.name.label("item_name"),
            Item.base_unit,
            ItemUnit.name.label("unit_name"),
            ItemUnit.factor_to_base,
            SupplierItem.pack_price,
        )
        .join(
            PurchaseOrder,
            and_(
                PurchaseOrder.business_id == PurchaseOrderLine.business_id,
                PurchaseOrder.id == PurchaseOrderLine.po_id,
            ),
        )
        .join(
            Item,
            and_(
                Item.business_id == PurchaseOrderLine.business_id,
                Item.id == PurchaseOrderLine.item_id,
            ),
        )
        .join(
            ItemUnit,
            and_(
                ItemUnit.business_id == PurchaseOrderLine.business_id,
                ItemUnit.id == PurchaseOrderLine.item_unit_id,
            ),
        )
        # The supplier may have stopped listing the item in this unit since.
        .outerjoin(
            SupplierItem,
            and_(
                SupplierItem.business_id == PurchaseOrderLine.business_id,
                SupplierItem.supplier_id == PurchaseOrder.supplier_id,
                SupplierItem.item_id == PurchaseOrderLine.item_id,
                SupplierItem.item_unit_id == PurchaseOrderLine.item_unit_id,
            ),
        )
        .where(
            PurchaseOrderLine.business_id == business_id,
            PurchaseOrderLine.po_id.in_(order_ids),
        )
        .order_by(Item.sku)
    )
    for row in line_rows:
        line = row.PurchaseOrderLine
        lines[line.po_id].append(
            OrderLineOut(
                id=line.id,
                item_id=line.item_id,
                sku=row.sku,
                item_name=row.item_name,
                base_unit=row.base_unit,
                item_unit_id=line.item_unit_id,
                unit_name=row.unit_name,
                factor_to_base=row.factor_to_base,
                suggested_packs=line.suggested_packs,
                qty_packs=line.qty_packs,
                order_packs=order_packs(line),
                pack_price=row.pack_price,
            )
        )

    order_rows = db.execute(
        select(
            PurchaseOrder,
            Supplier.name.label("supplier_name"),
            Location.name.label("location_name"),
            Receipt.id.label("receipt_id"),
        )
        .join(
            Supplier,
            and_(
                Supplier.business_id == PurchaseOrder.business_id,
                Supplier.id == PurchaseOrder.supplier_id,
            ),
        )
        .join(
            Location,
            and_(
                Location.business_id == PurchaseOrder.business_id,
                Location.id == PurchaseOrder.location_id,
            ),
        )
        # Only a received order has a receipt.
        .outerjoin(
            Receipt,
            and_(
                Receipt.business_id == PurchaseOrder.business_id, Receipt.po_id == PurchaseOrder.id
            ),
        )
        .where(PurchaseOrder.business_id == business_id, PurchaseOrder.id.in_(order_ids))
        # An action just changed the order; read it as stored now.
        .execution_options(populate_existing=True)
    )
    by_id = {}
    for row in order_rows:
        order = row.PurchaseOrder
        by_id[order.id] = OrderOut(
            id=order.id,
            status=order.status,
            location_id=order.location_id,
            location_name=row.location_name,
            supplier_id=order.supplier_id,
            supplier_name=row.supplier_name,
            sent_at=order.sent_at,
            received_at=order.received_at,
            cancelled_at=order.cancelled_at,
            created_at=order.created_at,
            receipt_id=row.receipt_id,
            lines=lines.get(order.id, []),
        )
    return [by_id[order_id] for order_id in order_ids if order_id in by_id]
