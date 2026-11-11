"""Stock domain functions: the only code that writes the ledger.

Each function works on the caller's Session and never commits (hard rule 3):
the API request, the simulator or the test owns the transaction. Each one
- scopes every lookup by business_id, so another business's rows are "not found";
- raises DomainError, with a message a user can act on, for a request it refuses;
- writes movements that share the request's client_id, numbered by client_seq
  0, 1, 2... in the order written, and is idempotent on client_id: a retry finds
  the movements already written and returns them without writing anything;
- updates each touched batch's stock_balances row in the same transaction, so
  qty_on_hand is always the sum of the batch's movements (domain rule 1).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select, text, tuple_, update
from sqlalchemy.orm import Session

from app.domain.checks import check_money, check_quantity
from app.domain.errors import DomainError
from app.domain.fefo import BatchSnapshot, allocate_usage
from app.models import (
    Batch,
    Item,
    ItemUnit,
    Location,
    Receipt,
    ReceiptLine,
    StockBalance,
    StockMovement,
    Supplier,
    User,
)

SOURCES = frozenset({"manual", "count", "simulator"})
WASTE_REASONS = frozenset(
    {"expired", "spoiled", "damaged", "prep_error", "overproduction", "other"}
)

# unit_cost is numeric(18,6) (the UnitCost column type).
UNIT_COST_STEP = Decimal("0.000001")
# Enough digits for the exact product of two numeric(14,3) or numeric(18,6)
# values, so only the final quantize rounds.
EXACT_PRECISION = 50


# --- Inputs and results ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiptLineInput:
    """One line of a delivery, as invoiced: qty_packs of a pack unit at pack_price each."""

    item_id: UUID
    item_unit_id: UUID
    qty_packs: Decimal
    pack_price: Decimal
    expires_on: date | None = None
    lot_code: str | None = None


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """The receipt and every movement it wrote: per line, its receive, then any settle pair."""

    receipt: Receipt
    movements: tuple[StockMovement, ...]
    # True when the client_id had been used already, so nothing new was written.
    replayed: bool


@dataclass(frozen=True, slots=True)
class UseResult:
    """One usage movement per batch drawn, in FEFO order, and the shortfall.

    shortfall > 0 means the default batch went (further) negative by that much.
    It is None for a replay, which returns the movements without recomputing it.
    """

    movements: tuple[StockMovement, ...]
    shortfall: Decimal | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class MovementsResult:
    """The movements a waste or a reversal wrote, in client_seq order."""

    movements: tuple[StockMovement, ...]
    replayed: bool


# --- Batches, locks and the ledger writer ---------------------------------------


@dataclass(slots=True)
class _BatchState:
    """A locked batch and its balance, kept current as this request posts to it."""

    id: UUID
    location_id: UUID
    item_id: UUID
    expires_on: date | None
    received_at: datetime
    is_default: bool
    unit_cost: Decimal | None
    on_hand: Decimal

    def snapshot(self) -> BatchSnapshot:
        return BatchSnapshot(
            self.id, self.expires_on, self.received_at, self.on_hand, self.is_default
        )


def _ensure_default_batch(
    session: Session, business_id: UUID, location_id: UUID, item_id: UUID
) -> None:
    """Create the item's default batch at the location, with its balance row, if missing.

    ON CONFLICT targets the partial unique index (one default batch per location
    and item), so a concurrent request creating the same default batch makes
    this wait for it and then do nothing. The balance row is inserted from the
    first statement's RETURNING, so it exists exactly when the batch was created
    here. received_at = now() is the transaction's start time, which is also
    what created_at defaults to, as the batches check requires. A default batch
    has no cost until something is received into it.
    """
    session.execute(
        text(
            """
            WITH new_batch AS (
                INSERT INTO batches (business_id, location_id, item_id, received_at, is_default)
                VALUES (:business_id, :location_id, :item_id, now(), true)
                ON CONFLICT (location_id, item_id) WHERE is_default DO NOTHING
                RETURNING business_id, location_id, item_id, id
            )
            INSERT INTO stock_balances (business_id, location_id, item_id, batch_id)
            SELECT business_id, location_id, item_id, id FROM new_batch
            """
        ),
        {"business_id": business_id, "location_id": location_id, "item_id": item_id},
    )


# Locking. Every function locks the default batch of each item it touches first,
# with one statement (_lock_defaults), and only then loads and locks the item's
# other batches with a second statement (_lock_batches). The default batch is
# the item's lock at that location, so requests for the same item take turns.
#
# Why two statements: under READ COMMITTED, each statement reads from a
# snapshot taken when the statement starts. When SELECT ... FOR UPDATE has to
# wait for another transaction's row lock, it re-reads the rows it waited for
# once that transaction commits, but it can't see rows that transaction
# inserted: its snapshot was taken before it started waiting. So a single
# statement that loads and locks all of an item's batches could wait for a
# concurrent receive, miss the batch that receive inserted, and post the whole
# usage to the default batch, taking it negative while stock exists. Locking
# the default batch first makes the second statement start after the other
# transaction has committed, so its snapshot includes the new batch.
#
# Both statements lock the batch rows and their balance rows together (plain
# FOR UPDATE on a join locks every joined row), so the unit_cost read with the
# balance is current too. Rows within a statement are locked in batch id order.


def _batch_query(business_id: UUID):
    return (
        select(
            Batch.id,
            Batch.location_id,
            Batch.item_id,
            Batch.expires_on,
            Batch.received_at,
            Batch.is_default,
            Batch.unit_cost,
            StockBalance.qty_on_hand,
        )
        .join(
            StockBalance,
            (StockBalance.business_id == Batch.business_id) & (StockBalance.batch_id == Batch.id),
        )
        .where(Batch.business_id == business_id)
    )


def _lock_batches(session: Session, business_id: UUID, *where) -> list[_BatchState]:
    statement = _batch_query(business_id).where(*where).order_by(Batch.id).with_for_update()
    return [_BatchState(*row) for row in session.execute(statement)]


def _lock_defaults(
    session: Session, business_id: UUID, pairs: Iterable[tuple[UUID, UUID]]
) -> dict[tuple[UUID, UUID], _BatchState]:
    """Create if missing, then lock, the default batch of each (location_id, item_id)."""
    pairs = sorted(set(pairs))
    for location_id, item_id in pairs:
        _ensure_default_batch(session, business_id, location_id, item_id)
    batches = _lock_batches(
        session,
        business_id,
        Batch.is_default,
        tuple_(Batch.location_id, Batch.item_id).in_(pairs),
    )
    return {(b.location_id, b.item_id): b for b in batches}


class _Ledger:
    """Writes one request's movements and keeps their batches' balances in step."""

    def __init__(
        self,
        session: Session,
        business_id: UUID,
        client_id: UUID,
        source: str,
        user_id: UUID | None,
    ) -> None:
        self.session = session
        self.business_id = business_id
        self.client_id = client_id
        self.source = source
        self.user_id = user_id
        self.movements: list[StockMovement] = []

    def post(
        self,
        batch: _BatchState,
        qty_delta: Decimal,
        type: str,
        unit_cost: Decimal | None,
        occurred_at: datetime,
        *,
        reason: str | None = None,
        receipt_line_id: UUID | None = None,
        reverses_id: UUID | None = None,
    ) -> StockMovement:
        movement = StockMovement(
            business_id=self.business_id,
            location_id=batch.location_id,
            item_id=batch.item_id,
            batch_id=batch.id,
            qty_delta=qty_delta,
            type=type,
            source=self.source,
            reason=reason,
            unit_cost=unit_cost,
            receipt_line_id=receipt_line_id,
            reverses_id=reverses_id,
            occurred_at=occurred_at,
            user_id=self.user_id,
            client_id=self.client_id,
            client_seq=len(self.movements),
        )
        self.session.add(movement)
        self.session.flush()
        # last_movement_at is the latest occurred_at, not the last one written:
        # a reversal copies its original's occurred_at, and the simulator may
        # post out of order. GREATEST ignores NULL, so the first movement sets it.
        self.session.execute(
            update(StockBalance)
            .where(StockBalance.business_id == self.business_id, StockBalance.batch_id == batch.id)
            .values(
                qty_on_hand=StockBalance.qty_on_hand + qty_delta,
                last_movement_at=func.greatest(StockBalance.last_movement_at, occurred_at),
            )
            # Refresh any StockBalance this session already holds.
            .execution_options(synchronize_session="fetch")
        )
        batch.on_hand += qty_delta
        self.movements.append(movement)
        return movement


# --- Checks and lookups ---------------------------------------------------------


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise DomainError(f"source must be one of {', '.join(sorted(SOURCES))}, got {source!r}")


def _check_timestamp(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime, not {type(value).__name__}")
    if value.utcoffset() is None:
        raise DomainError(f"{name} must include a timezone")


def iso_utc(value: datetime) -> str:
    """A timestamp for an error message, in UTC: 2026-11-02T07:30:00Z."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _blank_to_none(value: str | None) -> str | None:
    """Trim a free-text field; a blank one becomes NULL."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _get[M](session: Session, model: type[M], business_id: UUID, id: UUID, label: str) -> M:
    """The business's row with this id; another business's row is not found."""
    row = session.scalar(select(model).where(model.business_id == business_id, model.id == id))
    if row is None:
        raise DomainError(f"{label} {id} not found")
    return row


def _check_user(session: Session, business_id: UUID, user_id: UUID | None) -> None:
    if user_id is not None:
        _get(session, User, business_id, user_id, "user")


def _replay(
    session: Session, business_id: UUID, client_id: UUID, types: set[str]
) -> tuple[StockMovement, ...] | None:
    """The movements already written for client_id, or None if there are none.

    The unique key on (business_id, client_id, client_seq) is the backstop for
    two retries racing each other; this check makes the common retry a no-op.
    """
    movements = tuple(
        session.scalars(
            select(StockMovement)
            .where(StockMovement.business_id == business_id, StockMovement.client_id == client_id)
            .order_by(StockMovement.client_seq)
        )
    )
    if not movements:
        return None
    if not {m.type for m in movements} <= types:
        raise DomainError(f"client_id {client_id} was already used for a different request")
    return movements


# --- Receive --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LinePlan:
    """A validated receipt line, converted to base units and cost per base unit."""

    item: Item
    unit: ItemUnit
    qty_packs: Decimal
    qty_base: Decimal
    pack_price: Decimal
    unit_cost: Decimal
    expires_on: date | None
    lot_code: str | None


def _plan_line(session: Session, business_id: UUID, line: ReceiptLineInput) -> _LinePlan:
    check_quantity(line.qty_packs, "qty_packs")
    check_money(line.pack_price, "pack_price")
    item = _get(session, Item, business_id, line.item_id, "item")
    unit = session.scalar(
        select(ItemUnit).where(
            ItemUnit.business_id == business_id,
            ItemUnit.item_id == item.id,
            ItemUnit.id == line.item_unit_id,
        )
    )
    if unit is None:
        raise DomainError(f"unit {line.item_unit_id} not found for item {item.sku}")

    lot_code = _blank_to_none(line.lot_code)
    if not item.tracks_batches:
        if line.expires_on is not None or lot_code is not None:
            raise DomainError(
                f"item {item.sku} doesn't track batches, so it takes no expiry date or lot code"
            )
    elif item.tracks_expiry and line.expires_on is None:
        raise DomainError(f"item {item.sku} tracks expiry, so expires_on is required")
    elif not item.tracks_expiry and line.expires_on is not None:
        raise DomainError(f"item {item.sku} doesn't track expiry, so expires_on must be empty")

    with localcontext(prec=EXACT_PRECISION):
        qty_base = line.qty_packs * unit.factor_to_base
        unit_cost = (line.pack_price / unit.factor_to_base).quantize(UNIT_COST_STEP, ROUND_HALF_UP)
    check_quantity(qty_base, "qty_base (qty_packs × the unit's factor_to_base)")

    return _LinePlan(
        item=item,
        unit=unit,
        qty_packs=line.qty_packs,
        qty_base=qty_base,
        pack_price=line.pack_price,
        unit_cost=unit_cost,
        expires_on=line.expires_on,
        lot_code=lot_code,
    )


def _weighted_average_cost(
    on_hand: Decimal, cost: Decimal | None, qty: Decimal, line_cost: Decimal
) -> Decimal:
    """The default batch's cost after receiving qty at line_cost (D35).

    With nothing on hand (or a shortfall) or no cost yet, there is nothing to
    average with, so the line's cost replaces it.
    """
    if on_hand <= 0 or cost is None:
        return line_cost
    with localcontext(prec=EXACT_PRECISION):
        average = (on_hand * cost + qty * line_cost) / (on_hand + qty)
        return average.quantize(UNIT_COST_STEP, ROUND_HALF_UP)


def _set_cost(session: Session, business_id: UUID, batch: _BatchState, cost: Decimal) -> None:
    if cost != batch.unit_cost:
        session.execute(
            update(Batch)
            .where(Batch.business_id == business_id, Batch.id == batch.id)
            .values(unit_cost=cost)
        )
        batch.unit_cost = cost


def _new_batch(
    session: Session,
    business_id: UUID,
    location_id: UUID,
    plan: _LinePlan,
    received_at: datetime,
) -> _BatchState:
    """A batch for one receipt line of a batch-tracked item, with its balance row."""
    batch = Batch(
        business_id=business_id,
        location_id=location_id,
        item_id=plan.item.id,
        expires_on=plan.expires_on,
        lot_code=plan.lot_code,
        unit_cost=plan.unit_cost,
        received_at=received_at,
        is_default=False,
    )
    session.add(batch)
    session.flush()
    session.add(
        StockBalance(
            business_id=business_id,
            location_id=location_id,
            item_id=plan.item.id,
            batch_id=batch.id,
            qty_on_hand=Decimal(0),
        )
    )
    session.flush()
    return _BatchState(
        id=batch.id,
        location_id=location_id,
        item_id=plan.item.id,
        expires_on=plan.expires_on,
        received_at=received_at,
        is_default=False,
        unit_cost=plan.unit_cost,
        on_hand=Decimal(0),
    )


def receive(
    session: Session,
    business_id: UUID,
    *,
    location_id: UUID,
    supplier_id: UUID,
    received_at: datetime,
    invoice_ref: str | None,
    lines: Sequence[ReceiptLineInput],
    client_id: UUID,
    source: str,
    user_id: UUID | None = None,
) -> ReceiveResult:
    """Record a delivery: a receipt, its lines, and one receive movement per line.

    A batch-tracked item gets a new batch per line; any other item receives into
    its default batch, whose cost becomes the weighted average (D35). If a
    batch-tracked item's default batch is negative, each line settles it right
    after its receive (domain rule 4): a settle pair moves min(qty, shortfall)
    out of the new batch and into the default batch, at the new batch's cost.
    """
    _check_timestamp(received_at, "received_at")
    _check_source(source)
    if not lines:
        raise DomainError("a receipt needs at least one line")
    invoice_ref = _blank_to_none(invoice_ref)

    replayed = _replay(session, business_id, client_id, {"receive", "settle"})
    if replayed is not None:
        receipt = session.scalars(
            select(Receipt)
            .join(ReceiptLine, ReceiptLine.receipt_id == Receipt.id)
            .where(
                Receipt.business_id == business_id, ReceiptLine.id == replayed[0].receipt_line_id
            )
        ).one()
        return ReceiveResult(receipt, replayed, replayed=True)

    _get(session, Location, business_id, location_id, "location")
    _get(session, Supplier, business_id, supplier_id, "supplier")
    _check_user(session, business_id, user_id)
    if invoice_ref is not None:
        duplicate = session.scalar(
            select(Receipt.id).where(
                Receipt.business_id == business_id,
                Receipt.supplier_id == supplier_id,
                Receipt.invoice_ref == invoice_ref,
            )
        )
        if duplicate is not None:
            raise DomainError(f"invoice {invoice_ref} from this supplier was already received")
    plans = [_plan_line(session, business_id, line) for line in lines]

    # Every item on the receipt gets its default batch, even batch-tracked ones:
    # it is the item's lock (see Locking above), and settling needs its balance.
    defaults = _lock_defaults(session, business_id, ((location_id, p.item.id) for p in plans))

    receipt = Receipt(
        business_id=business_id,
        location_id=location_id,
        supplier_id=supplier_id,
        received_at=received_at,
        invoice_ref=invoice_ref,
    )
    session.add(receipt)
    session.flush()

    ledger = _Ledger(session, business_id, client_id, source, user_id)
    for plan in plans:
        default = defaults[(location_id, plan.item.id)]
        if plan.item.tracks_batches:
            batch = _new_batch(session, business_id, location_id, plan, received_at)
        else:
            batch = default
            cost = _weighted_average_cost(
                default.on_hand, default.unit_cost, plan.qty_base, plan.unit_cost
            )
            _set_cost(session, business_id, default, cost)

        line = ReceiptLine(
            business_id=business_id,
            receipt_id=receipt.id,
            item_id=plan.item.id,
            item_unit_id=plan.unit.id,
            qty_packs=plan.qty_packs,
            qty_base=plan.qty_base,
            pack_price=plan.pack_price,
            unit_cost=plan.unit_cost,
            expires_on=plan.expires_on,
            lot_code=plan.lot_code,
            batch_id=batch.id,
        )
        session.add(line)
        session.flush()
        ledger.post(
            batch, plan.qty_base, "receive", plan.unit_cost, received_at, receipt_line_id=line.id
        )

        if plan.item.tracks_batches and default.on_hand < 0:
            settle_qty = min(plan.qty_base, -default.on_hand)
            # The default batch is negative, so the new batch's cost replaces its
            # cost (D35), and both halves of the pair carry it.
            _set_cost(session, business_id, default, plan.unit_cost)
            ledger.post(batch, -settle_qty, "settle", plan.unit_cost, received_at)
            ledger.post(default, settle_qty, "settle", plan.unit_cost, received_at)

    return ReceiveResult(receipt, tuple(ledger.movements), replayed=False)


# --- Use ------------------------------------------------------------------------


def _local_date(session: Session, business_id: UUID, occurred_at: datetime) -> date:
    """occurred_at's date in the business's timezone, computed by Postgres (D32).

    Postgres has its own timezone database, so the date doesn't depend on the
    machine the API runs on.
    """
    local_date = session.scalar(
        text(
            "SELECT (CAST(:occurred_at AS timestamptz) AT TIME ZONE timezone)::date"
            " FROM businesses WHERE id = :business_id"
        ),
        {"occurred_at": occurred_at, "business_id": business_id},
    )
    if local_date is None:
        raise DomainError(f"business {business_id} not found")
    return local_date


def use(
    session: Session,
    business_id: UUID,
    *,
    location_id: UUID,
    item_id: UUID,
    quantity: Decimal,
    occurred_at: datetime,
    client_id: UUID,
    source: str,
    user_id: UUID | None = None,
) -> UseResult:
    """Use quantity of an item at a location, drawing batches earliest expiry first.

    allocate_usage (fefo.py) decides the split (domain rules 2 to 5), skipping
    batches received after occurred_at (D50) and batches that expired before
    occurred_at's local date (rule 3). Each usage
    movement copies its batch's current unit_cost (rule 7), which is NULL for a
    default batch that has never received anything.
    """
    check_quantity(quantity)
    _check_timestamp(occurred_at, "occurred_at")
    _check_source(source)

    replayed = _replay(session, business_id, client_id, {"usage"})
    if replayed is not None:
        return UseResult(replayed, shortfall=None, replayed=True)

    as_of = _local_date(session, business_id, occurred_at)
    _get(session, Location, business_id, location_id, "location")
    _get(session, Item, business_id, item_id, "item")
    _check_user(session, business_id, user_id)

    default = _lock_defaults(session, business_id, [(location_id, item_id)])[(location_id, item_id)]
    # Batches with nothing on hand can't be drawn, so they needn't be locked.
    # Non-default batches never go negative, so on_hand > 0 skips only empty ones.
    batches = [
        default,
        *_lock_batches(
            session,
            business_id,
            Batch.location_id == location_id,
            Batch.item_id == item_id,
            ~Batch.is_default,
            StockBalance.qty_on_hand > 0,
        ),
    ]
    result = allocate_usage([b.snapshot() for b in batches], quantity, as_of, occurred_at)

    by_id = {b.id: b for b in batches}
    ledger = _Ledger(session, business_id, client_id, source, user_id)
    for allocation in result.allocations:
        batch = by_id[allocation.batch_id]
        ledger.post(batch, -allocation.quantity, "usage", batch.unit_cost, occurred_at)
    return UseResult(tuple(ledger.movements), result.shortfall, replayed=False)


# --- Waste ----------------------------------------------------------------------


def waste(
    session: Session,
    business_id: UUID,
    *,
    location_id: UUID,
    item_id: UUID,
    batch_id: UUID | None,
    quantity: Decimal,
    reason: str,
    occurred_at: datetime,
    client_id: UUID,
    source: str,
    user_id: UUID | None = None,
) -> MovementsResult:
    """Waste quantity from one named batch, at that batch's cost (D33).

    A batch-tracked item must name the batch. An item without batch tracking
    wastes from its default batch, so batch_id is None or that batch's id.
    Expired batches can be wasted (that is how they leave stock), but waste
    can't exceed the batch's on-hand, so it never takes a batch negative, and
    can't name a batch received after occurred_at (D50). A default batch's
    received_at is when it was created, so that rule never applies to it.
    """
    check_quantity(quantity)
    _check_timestamp(occurred_at, "occurred_at")
    _check_source(source)
    if reason not in WASTE_REASONS:
        raise DomainError(
            f"reason must be one of {', '.join(sorted(WASTE_REASONS))}, got {reason!r}"
        )

    replayed = _replay(session, business_id, client_id, {"waste"})
    if replayed is not None:
        return MovementsResult(replayed, replayed=True)

    _get(session, Location, business_id, location_id, "location")
    item = _get(session, Item, business_id, item_id, "item")
    _check_user(session, business_id, user_id)

    default = _lock_defaults(session, business_id, [(location_id, item_id)])[(location_id, item_id)]
    if batch_id is None or batch_id == default.id:
        if batch_id is None and item.tracks_batches:
            raise DomainError(f"item {item.sku} tracks batches, so name the batch to waste")
        batch = default
    elif not item.tracks_batches:
        raise DomainError(
            f"item {item.sku} doesn't track batches, so waste comes from its default batch"
        )
    else:
        found = _lock_batches(
            session,
            business_id,
            Batch.location_id == location_id,
            Batch.item_id == item_id,
            Batch.id == batch_id,
        )
        if not found:
            raise DomainError(f"batch {batch_id} not found for item {item.sku} at this location")
        batch = found[0]
        if batch.received_at > occurred_at:
            raise DomainError(
                f"batch {batch_id} was received at {iso_utc(batch.received_at)},"
                f" after the waste's occurred_at {iso_utc(occurred_at)}"
            )

    if quantity > batch.on_hand:
        raise DomainError(f"can't waste {quantity}: the batch has {batch.on_hand} on hand")

    ledger = _Ledger(session, business_id, client_id, source, user_id)
    ledger.post(batch, -quantity, "waste", batch.unit_cost, occurred_at, reason=reason)
    return MovementsResult(tuple(ledger.movements), replayed=False)


# --- Reverse --------------------------------------------------------------------

REVERSIBLE_TYPES = frozenset({"usage", "waste"})


def reverse(
    session: Session,
    business_id: UUID,
    *,
    original_client_id: UUID,
    client_id: UUID,
    source: str,
    user_id: UUID | None = None,
) -> MovementsResult:
    """Reverse every movement of one usage or waste request (D34, domain rule 6).

    Each reversal posts the negated quantity to the same batch, with reverses_id
    set and the original's unit_cost and occurred_at, so each pair nets to zero
    in quantity and value, and period reports net out. created_at still shows
    when the correction was made. Receipts, settles and reversals can't be
    reversed, and a request can be reversed only once.
    """
    _check_source(source)

    # A retried reversal returns what it wrote, rather than "already reversed".
    replayed = _replay(session, business_id, client_id, {"reversal"})
    if replayed is not None:
        return MovementsResult(replayed, replayed=True)

    _check_user(session, business_id, user_id)
    originals = tuple(
        session.scalars(
            select(StockMovement)
            .where(
                StockMovement.business_id == business_id,
                StockMovement.client_id == original_client_id,
            )
            .order_by(StockMovement.client_seq)
        )
    )
    if not originals:
        raise DomainError(f"no request with client_id {original_client_id}")
    types = {m.type for m in originals}
    if not types <= REVERSIBLE_TYPES:
        raise DomainError(f"only usage and waste can be reversed, not {', '.join(sorted(types))}")

    defaults = _lock_defaults(session, business_id, ((m.location_id, m.item_id) for m in originals))
    batches = {b.id: b for b in defaults.values()}
    other_ids = {m.batch_id for m in originals} - batches.keys()
    if other_ids:
        for batch in _lock_batches(session, business_id, Batch.id.in_(other_ids)):
            batches[batch.id] = batch

    # Checked after locking, so two reversals of the same request take turns and
    # the second sees the first. The unique key on reverses_id is the backstop.
    already = session.scalar(
        select(StockMovement.id)
        .where(
            StockMovement.business_id == business_id,
            StockMovement.reverses_id.in_([m.id for m in originals]),
        )
        .limit(1)
    )
    if already is not None:
        raise DomainError(f"request {original_client_id} was already reversed")

    ledger = _Ledger(session, business_id, client_id, source, user_id)
    for original in originals:
        ledger.post(
            batches[original.batch_id],
            -original.qty_delta,
            "reversal",
            original.unit_cost,
            original.occurred_at,
            reverses_id=original.id,
        )
    return MovementsResult(tuple(ledger.movements), replayed=False)


# --- Rebuild balances -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BalanceDifference:
    """A batch whose stock_balances row doesn't match its movements.

    expected_* come from the ledger. actual_* are None when the batch has no
    balance row at all.
    """

    batch_id: UUID
    expected_qty: Decimal
    actual_qty: Decimal | None
    expected_last_movement_at: datetime | None
    actual_last_movement_at: datetime | None


def rebuild_balances(
    session: Session, business_id: UUID, *, fix: bool = False
) -> list[BalanceDifference]:
    """Recompute every batch's balance from the ledger and return the differences.

    qty_on_hand is the sum of the batch's movements and last_movement_at their
    latest occurred_at (domain rule 1); a batch with no movements has 0 and
    NULL. With fix=True the balance rows are overwritten (or created) to match.
    A fix locks every balance row of the business, so run it while nothing else
    is writing stock.
    """
    if fix:
        session.execute(
            select(StockBalance.id)
            .where(StockBalance.business_id == business_id)
            .order_by(StockBalance.batch_id)
            .with_for_update()
        )

    ledger = (
        select(
            StockMovement.batch_id,
            func.sum(StockMovement.qty_delta).label("qty"),
            func.max(StockMovement.occurred_at).label("last_movement_at"),
        )
        .where(StockMovement.business_id == business_id)
        .group_by(StockMovement.batch_id)
        .subquery()
    )
    expected_qty = func.coalesce(ledger.c.qty, 0)
    rows = session.execute(
        select(
            Batch.id,
            Batch.location_id,
            Batch.item_id,
            expected_qty,
            StockBalance.qty_on_hand,
            ledger.c.last_movement_at,
            StockBalance.last_movement_at,
            StockBalance.id,
        )
        .select_from(Batch)
        .outerjoin(ledger, ledger.c.batch_id == Batch.id)
        .outerjoin(
            StockBalance,
            (StockBalance.business_id == Batch.business_id) & (StockBalance.batch_id == Batch.id),
        )
        .where(
            Batch.business_id == business_id,
            # IS DISTINCT FROM treats NULLs as values, so a missing balance row
            # or a NULL last_movement_at on one side only is a difference.
            StockBalance.qty_on_hand.is_distinct_from(expected_qty)
            | StockBalance.last_movement_at.is_distinct_from(ledger.c.last_movement_at),
        )
        .order_by(Batch.id)
    ).all()

    differences = []
    for batch_id, location_id, item_id, qty, actual_qty, last, actual_last, balance_id in rows:
        differences.append(BalanceDifference(batch_id, qty, actual_qty, last, actual_last))
        if not fix:
            continue
        if balance_id is None:
            session.add(
                StockBalance(
                    business_id=business_id,
                    location_id=location_id,
                    item_id=item_id,
                    batch_id=batch_id,
                    qty_on_hand=qty,
                    last_movement_at=last,
                )
            )
        else:
            session.execute(
                update(StockBalance)
                .where(StockBalance.id == balance_id)
                .values(qty_on_hand=qty, last_movement_at=last)
                .execution_options(synchronize_session="fetch")
            )
    if fix:
        session.flush()
    return differences
