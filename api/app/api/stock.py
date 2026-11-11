"""Stock endpoints: receive, use, waste and reverse, and GET /stock.

Each write calls one domain function on the request's session and returns
what it wrote. get_db commits after the response is serialized (D42), so the
response models are built from the ORM rows the domain function returns.
Movements are manual and have no user until demo login exists. A DomainError
is a 400 (the handler is in app.main).
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.deps import CurrentBusiness, DbSession, Now
from app.api.schemas import (
    MovementsResponse,
    ReceiptIn,
    ReceiptResponse,
    ReversalIn,
    StockBatchOut,
    StockEntryOut,
    UsageIn,
    UsageResponse,
    WasteIn,
)
from app.domain.stock import ReceiptLineInput, receive, reverse, use, waste
from app.models import Batch, Item, Location, StockBalance

router = APIRouter(tags=["stock"])

SOURCE = "manual"
# D46: how far ahead of the server clock a client's timestamp may be.
MAX_AHEAD = timedelta(minutes=5)


def check_not_future(value: datetime, now: datetime, name: str) -> None:
    """Refuse a timestamp more than MAX_AHEAD ahead of now (D46); any past time is fine."""
    if value > now + MAX_AHEAD:
        raise HTTPException(
            status_code=400, detail=f"{name} is more than 5 minutes ahead of the server clock"
        )


def set_status(response: Response, replayed: bool) -> None:
    """201 when the request wrote something, 200 for a replay that wrote nothing."""
    response.status_code = 200 if replayed else 201


def replay_response(model: type) -> dict:
    return {
        200: {
            "model": model,
            "description": "A replay: the client_id was used already, so nothing new was"
            " written and these are the movements the first request wrote.",
        }
    }


@router.post("/receipts", status_code=201, responses=replay_response(ReceiptResponse))
def post_receipt(
    body: ReceiptIn, db: DbSession, business: CurrentBusiness, now: Now, response: Response
) -> ReceiptResponse:
    """Record a delivery: one receive movement per line, plus any settle pairs."""
    check_not_future(body.received_at, now, "received_at")
    result = receive(
        db,
        business.id,
        location_id=body.location_id,
        supplier_id=body.supplier_id,
        received_at=body.received_at,
        invoice_ref=body.invoice_ref,
        lines=[
            ReceiptLineInput(
                item_id=line.item_id,
                item_unit_id=line.item_unit_id,
                qty_packs=line.qty_packs,
                pack_price=line.pack_price,
                expires_on=line.expires_on,
                lot_code=line.lot_code,
            )
            for line in body.lines
        ],
        client_id=body.client_id,
        source=SOURCE,
    )
    set_status(response, result.replayed)
    return ReceiptResponse.model_validate(result)


@router.post("/usage", status_code=201, responses=replay_response(UsageResponse))
def post_usage(
    body: UsageIn, db: DbSession, business: CurrentBusiness, now: Now, response: Response
) -> UsageResponse:
    """Use a quantity of an item, drawing batches earliest expiry first (FEFO)."""
    check_not_future(body.occurred_at, now, "occurred_at")
    result = use(
        db,
        business.id,
        location_id=body.location_id,
        item_id=body.item_id,
        quantity=body.quantity,
        occurred_at=body.occurred_at,
        client_id=body.client_id,
        source=SOURCE,
    )
    set_status(response, result.replayed)
    return UsageResponse.model_validate(result)


@router.post("/waste", status_code=201, responses=replay_response(MovementsResponse))
def post_waste(
    body: WasteIn, db: DbSession, business: CurrentBusiness, now: Now, response: Response
) -> MovementsResponse:
    """Waste a quantity from one batch, with a reason."""
    check_not_future(body.occurred_at, now, "occurred_at")
    result = waste(
        db,
        business.id,
        location_id=body.location_id,
        item_id=body.item_id,
        batch_id=body.batch_id,
        quantity=body.quantity,
        reason=body.reason,
        occurred_at=body.occurred_at,
        client_id=body.client_id,
        source=SOURCE,
    )
    set_status(response, result.replayed)
    return MovementsResponse.model_validate(result)


@router.post("/reversals", status_code=201, responses=replay_response(MovementsResponse))
def post_reversal(
    body: ReversalIn, db: DbSession, business: CurrentBusiness, response: Response
) -> MovementsResponse:
    """Reverse every movement of one usage or waste request."""
    result = reverse(
        db,
        business.id,
        original_client_id=body.original_client_id,
        client_id=body.client_id,
        source=SOURCE,
    )
    set_status(response, result.replayed)
    return MovementsResponse.model_validate(result)


# --- GET /stock -----------------------------------------------------------------


def local_date(db: Session, business_id: UUID, moment: datetime) -> date:
    """moment's date in the business's timezone, computed by Postgres, as use() does (D32)."""
    return db.scalar(
        text(
            "SELECT (CAST(:moment AS timestamptz) AT TIME ZONE timezone)::date"
            " FROM businesses WHERE id = :business_id"
        ),
        {"moment": moment, "business_id": business_id},
    )


@router.get("/stock")
def get_stock(
    db: DbSession,
    business: CurrentBusiness,
    now: Now,
    location_id: UUID | None = None,
    item_id: UUID | None = None,
) -> list[StockEntryOut]:
    """On-hand per active item at each location, with its batches (D44).

    Sorted by location name, then item SKU. A batch is expired when its expiry
    date is before today in the business's timezone. A location_id or item_id
    that isn't the business's matches nothing, so the list is empty.
    """
    business_id = business.id
    today = local_date(db, business_id, now)

    # Every active item at every location, including items never received.
    entries = (
        select(
            Location.id.label("location_id"),
            Location.name.label("location_name"),
            Item.id.label("item_id"),
            Item.sku,
            Item.name.label("item_name"),
            Item.category,
            Item.base_unit,
        )
        .select_from(Location)
        .join(Item, Item.business_id == Location.business_id)
        .where(Location.business_id == business_id, Item.business_id == business_id, Item.is_active)
        .order_by(Location.name, Location.id, Item.sku)
    )
    # Batches with stock, in FEFO order (domain rule 2). is_default sorts first
    # because a batch-tracked item without expiry also has no expires_on, and
    # its default batch must still come after its other batches.
    batches = (
        select(
            Batch.location_id,
            Batch.item_id,
            Batch.id,
            Batch.is_default,
            Batch.expires_on,
            Batch.lot_code,
            Batch.received_at,
            Batch.unit_cost,
            StockBalance.qty_on_hand,
        )
        .join(
            StockBalance,
            (StockBalance.business_id == Batch.business_id) & (StockBalance.batch_id == Batch.id),
        )
        .where(
            Batch.business_id == business_id,
            StockBalance.business_id == business_id,
            StockBalance.qty_on_hand != 0,
        )
        .order_by(
            Batch.is_default, Batch.expires_on.asc().nulls_last(), Batch.received_at, Batch.id
        )
    )
    if location_id is not None:
        entries = entries.where(Location.id == location_id)
        batches = batches.where(Batch.location_id == location_id)
    if item_id is not None:
        entries = entries.where(Item.id == item_id)
        batches = batches.where(Batch.item_id == item_id)

    # Group the batches by (location, item); they stay in FEFO order.
    by_entry: dict[tuple[UUID, UUID], list[StockBatchOut]] = defaultdict(list)
    for row in db.execute(batches):
        by_entry[(row.location_id, row.item_id)].append(
            StockBatchOut(
                id=row.id,
                is_default=row.is_default,
                expires_on=row.expires_on,
                lot_code=row.lot_code,
                received_at=row.received_at,
                on_hand=row.qty_on_hand,
                unit_cost=row.unit_cost,
                # Usable on its expiry date, expired from the next day (rule 3).
                expired=row.expires_on is not None and row.expires_on < today,
            )
        )

    result = []
    for row in db.execute(entries):
        entry_batches = by_entry.get((row.location_id, row.item_id), [])
        result.append(
            StockEntryOut(
                item_id=row.item_id,
                sku=row.sku,
                item_name=row.item_name,
                category=row.category,
                base_unit=row.base_unit,
                location_id=row.location_id,
                location_name=row.location_name,
                # Summed exactly in Decimal. Every batch with stock is listed, so
                # this is the sum of all of them, default batch included.
                on_hand=sum((b.on_hand for b in entry_batches), Decimal("0.000")),
                expired=sum((b.on_hand for b in entry_batches if b.expired), Decimal("0.000")),
                batches=entry_batches,
            )
        )
    return result
