"""Stock endpoints: receive, use, waste and reverse.

Each write calls one domain function on the request's session and returns
what it wrote. get_db commits after the response is serialized (D42), so the
response models are built from the ORM rows the domain function returns.
Movements are manual and have no user until demo login exists. A DomainError
is a 400 (the handler is in app.main).
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Response

from app.api.deps import CurrentBusiness, DbSession, Now
from app.api.schemas import (
    MovementsResponse,
    ReceiptIn,
    ReceiptResponse,
    ReversalIn,
    UsageIn,
    UsageResponse,
    WasteIn,
)
from app.domain.stock import ReceiptLineInput, receive, reverse, use, waste

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
