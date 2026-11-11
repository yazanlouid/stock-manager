"""Request and response models.

Request bodies forbid unknown fields, so a client that sends source or user_id
gets a 422 rather than having it ignored. They don't repeat the domain's
checks: quantities and prices are plain Decimals, and check_quantity or
check_money gives the 400. Every endpoint returns one of these models, never
a dict, so Decimals are written as strings ("0.000850"), not floats.

Response Decimals are written at their column's scale: quantities and money
with three decimals ("-300.000"), unit costs with six. A response built from
rows the request just wrote holds the Decimals Python computed ("-300"),
while a replay reads them back from Postgres ("-300.000"); fixing the scale
makes both the same. Every value already fits its column, so nothing should
round; at_scale raises rather than round one that doesn't.

Response timestamps are written in UTC ("2026-11-02T12:00:00Z"), for the same
reason: a first response holds the client's datetime with its offset
("+03:00"), while a replay or a reversal reads it back from Postgres.
"""

from datetime import UTC, date, datetime
from decimal import Context, Decimal, Inexact, InvalidOperation
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, PlainSerializer

# Traps instead of rounding: Inexact when a non-zero digit would be dropped,
# InvalidOperation when the result needs more digits than the precision.
_EXACT = Context(traps=[Inexact, InvalidOperation])


def at_scale(value: Decimal, places: int) -> str:
    """value as a string with exactly that many decimals; raises if that would round it."""
    return str(value.quantize(Decimal(1).scaleb(-places), context=_EXACT))


def _at_scale(places: int) -> PlainSerializer:
    return PlainSerializer(lambda value: at_scale(value, places), return_type=str)


# numeric(14,3): quantities in base units, pack prices and factor_to_base.
Quantity = Annotated[Decimal, _at_scale(3)]
Money = Annotated[Decimal, _at_scale(3)]
# numeric(18,6): cost per base unit.
UnitCost = Annotated[Decimal, _at_scale(6)]

# A timestamp converted to UTC, which pydantic writes with a Z.
Timestamp = Annotated[datetime, AfterValidator(lambda value: value.astimezone(UTC))]


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
    qty_delta: Quantity
    type: str
    source: str
    reason: str | None
    unit_cost: UnitCost | None
    receipt_line_id: UUID | None
    reverses_id: UUID | None
    occurred_at: Timestamp
    client_id: UUID
    client_seq: int
    created_at: Timestamp


class ReceiptOut(ResponseModel):
    id: UUID
    location_id: UUID
    supplier_id: UUID
    received_at: Timestamp
    invoice_ref: str | None
    created_at: Timestamp


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
    shortfall: Quantity | None


# --- GET /stock ----------------------------------------------------------------


class StockBatchOut(BaseModel):
    """A batch with stock on hand. Quantities are in the item's base unit."""

    id: UUID
    is_default: bool
    expires_on: date | None
    lot_code: str | None
    received_at: Timestamp
    # Negative only for a default batch with an unsettled shortfall.
    on_hand: Quantity
    # Per base unit; null for a default batch that has never received anything.
    unit_cost: UnitCost | None
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
    on_hand: Quantity
    # On-hand in batches past their expiry date.
    expired: Quantity
    # Batches with non-zero on-hand, in FEFO order with the default batch last.
    batches: list[StockBatchOut]


# --- GET /catalogue ------------------------------------------------------------


class BusinessOut(ResponseModel):
    id: UUID
    name: str
    # ISO 4217 code, for example JOD.
    currency: str
    # IANA name, for example Asia/Amman.
    timezone: str


class LocationOut(ResponseModel):
    id: UUID
    name: str
    kind: str


class SupplierOut(ResponseModel):
    id: UUID
    name: str
    phone: str | None
    lead_time_days: int


class ItemUnitOut(ResponseModel):
    id: UUID
    name: str
    # Base units in one of this unit.
    factor_to_base: Quantity


class SupplierPriceOut(ResponseModel):
    supplier_id: UUID
    item_unit_id: UUID
    # Price of one pack of item_unit_id.
    pack_price: Money
    is_preferred: bool


class CatalogueItemOut(ResponseModel):
    id: UUID
    sku: str
    name: str
    category: str | None
    base_unit: str
    # Whether a receipt line creates a batch, and whether waste names one.
    tracks_batches: bool
    # Whether a receipt line needs expires_on.
    tracks_expiry: bool
    shelf_life_days: int | None
    units: list[ItemUnitOut]
    supplier_prices: list[SupplierPriceOut]


class CatalogueOut(BaseModel):
    business: BusinessOut
    locations: list[LocationOut]
    suppliers: list[SupplierOut]
    items: list[CatalogueItemOut]
