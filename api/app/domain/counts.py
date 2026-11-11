"""Stock counts: set items' stock at a location to what was counted (domain rule 9).

count_stock() follows the same contract as the functions in stock.py: it works
on the caller's Session and never commits, scopes every lookup by business_id,
raises DomainError for a request it refuses, numbers its movements by
client_seq, replays on client_id, and keeps stock_balances in step. It uses
stock.py's locking, ledger and lookup helpers.

A count covers every batch of each counted item at the location (D54). Each
batch it covers gets a line with expected_qty (the batch's balance when the
count is posted) and counted_qty, and each difference posts one count_adjust
at the batch's current unit_cost. A count changes no batch's cost.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.domain.checks import ZERO, check_counted_quantity
from app.domain.errors import DomainError
from app.domain.stock import (
    _BatchState,
    _check_source,
    _check_timestamp,
    _check_user,
    _get,
    _Ledger,
    _lock_batches,
    _lock_defaults,
    iso_utc,
)
from app.models import (
    Batch,
    Item,
    Location,
    StockBalance,
    StockCount,
    StockCountLine,
    StockMovement,
)

# --- Inputs and results ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchCountInput:
    """What was counted of one received batch, in the item's base unit."""

    batch_id: UUID
    counted_qty: Decimal


@dataclass(frozen=True, slots=True)
class CountItemInput:
    """One counted item.

    An item without batch tracking gives counted_qty (its default batch). A
    batch-tracked item gives batches instead: the batches found, where every
    other batch counts as zero, so an empty list means none was found.
    """

    item_id: UUID
    counted_qty: Decimal | None = None
    batches: Sequence[BatchCountInput] | None = None


@dataclass(frozen=True, slots=True)
class CountResult:
    """The count, its lines and the count_adjust movements its differences posted.

    Lines are sorted by item SKU, then in FEFO order with the default batch
    last; movements are in client_seq order, which follows the lines.
    """

    count: StockCount
    lines: tuple[StockCountLine, ...]
    movements: tuple[StockMovement, ...]
    # True when the client_id had been used already, so nothing new was written.
    replayed: bool


# --- Count ----------------------------------------------------------------------


def count_stock(
    session: Session,
    business_id: UUID,
    *,
    location_id: UUID,
    counted_at: datetime,
    items: Sequence[CountItemInput],
    client_id: UUID,
    source: str,
    user_id: UUID | None = None,
) -> CountResult:
    """Count items at a location and post each difference as a count_adjust (D54).

    An item without batch tracking counts its default batch. For a
    batch-tracked item, each named batch takes its counted quantity, every
    other batch with stock counts as zero, and the default batch is set to
    zero (stock without a batch comes in through a receipt). Every counted
    item gets a line for its default batch, so counting an item always leaves
    a line. Inactive items can be counted, so a retired item's leftover stock
    can be zeroed.
    """
    _check_timestamp(counted_at, "counted_at")
    _check_source(source)
    _check_input(items)

    # A count is found by its client_id even when it posted no movements.
    replayed = _replay(session, business_id, client_id)
    if replayed is not None:
        return replayed

    _get(session, Location, business_id, location_id, "location")
    _check_user(session, business_id, user_id)
    catalogue = _load_items(session, business_id, items)
    named = _load_named_batches(session, business_id, location_id, counted_at, items, catalogue)

    # Lock each counted item's default batch first (the item's lock, see
    # Locking in stock.py), then its other batches in a second statement:
    # those with stock, which count as zero unless named, and the named ones.
    defaults = _lock_defaults(session, business_id, ((location_id, i.item_id) for i in items))
    tracked_ids = [i.item_id for i in items if catalogue[i.item_id].tracks_batches]
    others = []
    if tracked_ids:
        others = _lock_batches(
            session,
            business_id,
            Batch.location_id == location_id,
            Batch.item_id.in_(tracked_ids),
            ~Batch.is_default,
            (StockBalance.qty_on_hand != 0) | Batch.id.in_(list(named)),
        )

    # What each covered batch was counted at.
    counted: dict[UUID, tuple[_BatchState, Decimal]] = {}
    for item in items:
        default = defaults[(location_id, item.item_id)]
        counted[default.id] = (default, item.counted_qty if item.batches is None else ZERO)
    for batch in others:
        counted[batch.id] = (batch, named.get(batch.id, ZERO))

    count = StockCount(
        business_id=business_id,
        location_id=location_id,
        counted_at=counted_at,
        client_id=client_id,
        user_id=user_id,
    )
    session.add(count)
    session.flush()
    for batch, counted_qty in counted.values():
        session.add(
            StockCountLine(
                business_id=business_id,
                count_id=count.id,
                location_id=location_id,
                item_id=batch.item_id,
                batch_id=batch.id,
                expected_qty=batch.on_hand,
                counted_qty=counted_qty,
            )
        )
    session.flush()

    # Read back in the order the response lists them, and post in that order,
    # so client_seq follows the lines and a replay lists both the same way.
    lines = _load_lines(session, business_id, count.id)
    ledger = _Ledger(session, business_id, client_id, source, user_id)
    for line in lines:
        difference = line.counted_qty - line.expected_qty
        if difference != 0:
            batch, _ = counted[line.batch_id]
            ledger.post(
                batch,
                difference,
                "count_adjust",
                batch.unit_cost,
                counted_at,
                stock_count_line_id=line.id,
            )
    return CountResult(count, lines, tuple(ledger.movements), replayed=False)


# --- Checks and lookups ---------------------------------------------------------


def _check_input(items: Sequence[CountItemInput]) -> None:
    """Checks that need no database: something to count, nothing twice, valid quantities."""
    if not items:
        raise DomainError("a count needs at least one item")
    for item_id, times in Counter(i.item_id for i in items).items():
        if times > 1:
            raise DomainError(f"item {item_id} is listed twice")
    batch_ids = Counter(b.batch_id for i in items for b in i.batches or ())
    for batch_id, times in batch_ids.items():
        if times > 1:
            raise DomainError(f"batch {batch_id} is listed twice")
    for item in items:
        if item.counted_qty is not None:
            check_counted_quantity(item.counted_qty)
        for batch in item.batches or ():
            check_counted_quantity(batch.counted_qty)


def _replay(session: Session, business_id: UUID, client_id: UUID) -> CountResult | None:
    """The count already saved for client_id, or None if there is none.

    A client_id that another kind of request already used is refused, as
    stock.py's _replay refuses a count's client_id.
    """
    count = session.scalar(
        select(StockCount).where(
            StockCount.business_id == business_id, StockCount.client_id == client_id
        )
    )
    if count is None:
        used = session.scalar(
            select(StockMovement.id)
            .where(StockMovement.business_id == business_id, StockMovement.client_id == client_id)
            .limit(1)
        )
        if used is not None:
            raise DomainError(f"client_id {client_id} was already used for a different request")
        return None
    movements = tuple(
        session.scalars(
            select(StockMovement)
            .where(StockMovement.business_id == business_id, StockMovement.client_id == client_id)
            .order_by(StockMovement.client_seq)
        )
    )
    return CountResult(count, _load_lines(session, business_id, count.id), movements, True)


def _load_items(
    session: Session, business_id: UUID, items: Sequence[CountItemInput]
) -> dict[UUID, Item]:
    """The counted items, checking each one's input against its batch tracking."""
    ids = [i.item_id for i in items]
    found = {
        item.id: item
        for item in session.scalars(
            select(Item).where(Item.business_id == business_id, Item.id.in_(ids))
        )
    }
    for counted in items:
        item = found.get(counted.item_id)
        if item is None:
            raise DomainError(f"item {counted.item_id} not found")
        if item.tracks_batches and (counted.counted_qty is not None or counted.batches is None):
            raise DomainError(
                f"item {item.sku} tracks batches, so count it by batch: give batches"
                " (an empty list if none was found), not counted_qty"
            )
        if not item.tracks_batches and (counted.batches is not None or counted.counted_qty is None):
            raise DomainError(
                f"item {item.sku} doesn't track batches, so give counted_qty, not batches"
            )
    return found


def _load_named_batches(
    session: Session,
    business_id: UUID,
    location_id: UUID,
    counted_at: datetime,
    items: Sequence[CountItemInput],
    catalogue: dict[UUID, Item],
) -> dict[UUID, Decimal]:
    """Check every named batch; return each one's counted quantity by batch id.

    Looked up by id within the business, before any lock, so each refusal can
    say what is wrong with the batch.
    """
    item_of = {b.batch_id: i.item_id for i in items for b in i.batches or ()}
    batches = {
        batch.id: batch
        for batch in session.scalars(
            select(Batch).where(Batch.business_id == business_id, Batch.id.in_(list(item_of)))
        )
    }
    for batch_id, item_id in item_of.items():
        batch = batches.get(batch_id)
        sku = catalogue[item_id].sku
        if batch is None:
            raise DomainError(f"batch {batch_id} not found")
        if batch.is_default:
            raise DomainError(
                f"batch {batch_id} is a default batch, which a count sets to zero;"
                " name received batches only"
            )
        if batch.item_id != item_id:
            raise DomainError(f"batch {batch_id} is not a batch of item {sku}")
        if batch.location_id != location_id:
            raise DomainError(f"batch {batch_id} of item {sku} is not at this location")
        if batch.received_at > counted_at:
            raise DomainError(
                f"batch {batch_id} of item {sku} was received at {iso_utc(batch.received_at)},"
                f" after counted_at {iso_utc(counted_at)}"
            )
    return {b.batch_id: b.counted_qty for i in items for b in i.batches or ()}


def _load_lines(session: Session, business_id: UUID, count_id: UUID) -> tuple[StockCountLine, ...]:
    """A count's lines, by item SKU, then in FEFO order with the default batch last.

    The same order as GET /stock lists batches. Both a new count and a replay
    read their lines through here, so they list them identically.
    """
    return tuple(
        session.scalars(
            select(StockCountLine)
            .join(
                Item,
                and_(
                    Item.business_id == StockCountLine.business_id,
                    Item.id == StockCountLine.item_id,
                ),
            )
            .join(
                Batch,
                and_(
                    Batch.business_id == StockCountLine.business_id,
                    Batch.id == StockCountLine.batch_id,
                ),
            )
            .where(StockCountLine.business_id == business_id, StockCountLine.count_id == count_id)
            .order_by(
                Item.sku,
                Batch.is_default,
                Batch.expires_on.asc().nulls_last(),
                Batch.received_at,
                Batch.id,
            )
        )
    )
