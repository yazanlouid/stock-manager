"""POST /counts: submit a stock count (U3).

The endpoint calls count_stock on the request's session with source count
(D56) and returns what it wrote; get_db commits after the response is
serialized (D42). Counts have no user until demo login exists. A DomainError
is a 400 (the handler is in app.main).
"""

from fastapi import APIRouter, Response

from app.api.deps import CurrentBusiness, DbSession, Now
from app.api.schemas import CountIn, CountResponse
from app.api.stock import check_not_future, replay_response, set_status
from app.domain.counts import BatchCountInput, CountItemInput, count_stock

router = APIRouter(tags=["counts"])

# D56: a count_adjust takes its channel as source.
SOURCE = "count"


@router.post("/counts", status_code=201, responses=replay_response(CountResponse))
def post_count(
    body: CountIn, db: DbSession, business: CurrentBusiness, now: Now, response: Response
) -> CountResponse:
    """Count items at a location; each difference posts one count_adjust (D54).

    An item without batch tracking sends counted_qty. A batch-tracked item
    sends batches: the batches found, where every other batch and the default
    batch count as zero. counted_at can't be at or before the item's latest
    count there, or before its latest movement (D51).
    """
    check_not_future(body.counted_at, now, "counted_at")
    result = count_stock(
        db,
        business.id,
        location_id=body.location_id,
        counted_at=body.counted_at,
        items=[
            CountItemInput(
                item_id=item.item_id,
                counted_qty=item.counted_qty,
                batches=None
                if item.batches is None
                else [BatchCountInput(b.batch_id, b.counted_qty) for b in item.batches],
            )
            for item in body.items
        ],
        client_id=body.client_id,
        source=SOURCE,
    )
    set_status(response, result.replayed)
    return CountResponse.model_validate(result)
