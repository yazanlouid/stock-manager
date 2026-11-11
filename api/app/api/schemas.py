"""Request and response models.

Request bodies forbid unknown fields, so a client that sends source or user_id
gets a 422 rather than having it ignored. They don't repeat the domain's
checks: quantities and prices are plain Decimals, and check_quantity or
check_money gives the 400. Every endpoint returns one of these models, never
a dict, so Decimals are written as strings ("0.000850"), not floats.
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseModel(BaseModel):
    # Built from the ORM rows the domain functions and queries return.
    model_config = ConfigDict(from_attributes=True)


# --- Write requests ------------------------------------------------------------


class ReceiptLineIn(RequestModel):
    item_id: UUID
    item_unit_id: UUID
    qty_packs: Decimal
    # Price of one pack, as invoiced.
    pack_price: Decimal
    # Required for an item that tracks expiry; empty for any other item.
    expires_on: date | None = None
    lot_code: str | None = None


class ReceiptIn(RequestModel):
    client_id: UUID
    location_id: UUID
    supplier_id: UUID
    received_at: AwareDatetime
    invoice_ref: str | None = None
    lines: list[ReceiptLineIn]


class UsageIn(RequestModel):
    client_id: UUID
    location_id: UUID
    item_id: UUID
    # In the item's base unit.
    quantity: Decimal
    occurred_at: AwareDatetime


class WasteIn(RequestModel):
    client_id: UUID
    location_id: UUID
    item_id: UUID
    # Required for an item that tracks batches (D33).
    batch_id: UUID | None = None
    # In the item's base unit.
    quantity: Decimal
    # expired, spoiled, damaged, prep_error, overproduction or other.
    reason: str
    occurred_at: AwareDatetime


class ReversalIn(RequestModel):
    client_id: UUID
    # The client_id of the usage or waste request to reverse (D34).
    original_client_id: UUID


# --- Write responses -----------------------------------------------------------


class MovementOut(ResponseModel):
    """A ledger row: every column except business_id and user_id."""

    id: UUID
    location_id: UUID
    item_id: UUID
    batch_id: UUID
    qty_delta: Decimal
    type: str
    source: str
    reason: str | None
    unit_cost: Decimal | None
    receipt_line_id: UUID | None
    reverses_id: UUID | None
    occurred_at: datetime
    client_id: UUID
    client_seq: int
    created_at: datetime


class ReceiptOut(ResponseModel):
    id: UUID
    location_id: UUID
    supplier_id: UUID
    received_at: datetime
    invoice_ref: str | None
    created_at: datetime


class MovementsResponse(ResponseModel):
    """What a waste or a reversal wrote, in client_seq order.

    replayed is true when the client_id had been used already: nothing new was
    written, and these are the movements the first request wrote.
    """

    movements: list[MovementOut]
    replayed: bool


class ReceiptResponse(MovementsResponse):
    receipt: ReceiptOut


class UsageResponse(MovementsResponse):
    # How far the default batch went negative; null for a replay.
    shortfall: Decimal | None


# --- GET /stock ----------------------------------------------------------------


class StockBatchOut(BaseModel):
    """A batch with stock on hand. Quantities are in the item's base unit."""

    id: UUID
    is_default: bool
    expires_on: date | None
    lot_code: str | None
    received_at: datetime
    # Negative only for a default batch with an unsettled shortfall.
    on_hand: Decimal
    # Per base unit; null for a default batch that has never received anything.
    unit_cost: Decimal | None
    # Past its expiry date in the business's timezone; usable on the date itself.
    expired: bool


class StockEntryOut(BaseModel):
    """One active item at one location."""

    item_id: UUID
    sku: str
    item_name: str
    category: str | None
    base_unit: str
    location_id: UUID
    location_name: str
    # Sum of every batch, default included, so negative while a shortfall is unsettled.
    on_hand: Decimal
    # On-hand in batches past their expiry date.
    expired: Decimal
    # Batches with non-zero on-hand, in FEFO order with the default batch last.
    batches: list[StockBatchOut]
