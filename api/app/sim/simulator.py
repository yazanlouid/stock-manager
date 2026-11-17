"""The day-by-day simulator: replays a venue's days through the domain functions.

It writes stock only through receive, use, waste, reverse and count_stock, and
orders only through set_line_quantity, send_order and cancel_order, with
source "simulator" and no user. It runs the daily sweep, and it reads
batches, balances, alerts and orders to decide what to do, but never writes a
table itself.

Days. days=30 means the 30 days ending on end_date (D38). Day 0, the day before
them, holds the opening stock: after the first sweep, one delivery from each
supplier brings every item up to its order-up-to level, because every receive
needs a receipt line. Each later day runs, in time order:
- the sweep, just after midnight, which moves the alerts date to the new day
  (D79), so batches become expiring and expired and stock that expired
  overnight stops counting as usable;
- a morning check that wastes the batches with an open expired alert (reason
  expired), usually the first morning and sometimes a day or two later, so
  FEFO has to skip expired stock that is still on hand;
- the deliveries planned for the day, each received against the order it
  fulfils, a few of them late or short (D77);
- each item's usage, in one to three entries across opening hours, a few of
  them mistakes that are reversed a few minutes later and logged again;
- random spoilage from a batch with stock;
- at closing, a check that wastes most of what expires today (the batches
  whose expiring alert says so): it won't be sold tomorrow, and it has to be
  off the books before tonight's orders, which would otherwise count it as
  stock for tomorrow;
- after closing, on a supplier's order days, the owner's ordering: the
  supplier's draft is sent as the par-level rule filled it, now and then
  with a line raised, and its delivery is planned for the delivery day;
- last, the day's count of the items whose count day it is (D78).

Orders (D52, D77). Nothing here decides how much to order: the draft lines
come from the par levels (domain rule 8). The simulator only plays the owner,
who sends drafts on order days, and the supplier, who delivers.

Shrinkage (D78). Over-portioning and theft are stock that leaves without a
movement. The simulator keeps the total per item in self.loss; what is
physically on the shelf is the ledger's on-hand minus that loss. A count
reports that, so the loss shows up as a count variance and starts again from
zero. Demand isn't capped by stock: usage is logged as demanded and runs short
when a delivery is late or short (domain rule 4).

Reversals never cross a count (D80). A reversal is dated at its original's
occurred_at, which a count of that item would refuse (D51). Mistakes are
reversed a few minutes later, entries end half an hour before closing, and
counts are taken after closing, so a reversal always comes before the count.

Transactions (D39). The simulator commits after each day, day 0 included. On
any error it rolls back the unfinished day and raises SimulationError naming
the day and the request; the days already committed stay.

Determinism. The same random seed and end date give an identical ledger:
- every random draw comes from one random.Random, as integers scaled into
  Decimal (no floats), and client_ids come from the same generator;
- everything is iterated in a fixed order (venue order, SKU order, or query
  results ordered by natural keys), never by server-generated ids;
- each item is on one line per receipt and each delivery has its own time, so
  FEFO never falls back to its id tie-break.

Statistics (D75). After day 0 and then every seventh day, except the last day,
the simulator runs ANALYZE. Postgres plans queries from table statistics, and a
database filled in one go has none, or worse, has statistics from when it was
empty: the planner then assumes empty tables and reads whole tables where it
should look up a few rows. The domain functions run thousands of times per
simulated month, so the run is much faster once the statistics are real.

Timezones. Dates are the venue's local dates. Local times become timestamptz
in Postgres, in one query per day, as use() does for usage dates (D32).
"""

import random
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import and_, exists, func, select, text
from sqlalchemy.orm import Session

from app.domain.counts import BatchCountInput, CountItemInput, count_stock
from app.domain.orders import cancel_order, send_order, set_line_quantity
from app.domain.rules import sweep
from app.domain.stock import ReceiptLineInput, receive, reverse, use, waste
from app.models import (
    Alert,
    Batch,
    Item,
    ItemUnit,
    Location,
    PurchaseOrder,
    PurchaseOrderLine,
    StockBalance,
    SupplierItem,
)
from app.sim.cafe import CAFE, ItemSpec, SupplierSpec, Venue

DEFAULT_RANDOM_SEED = 20261110
SOURCE = "simulator"

MILLE = Decimal(1000)
MONEY_STEP = Decimal("0.001")
ZERO = Decimal(0)
# The sweep runs first thing each day, before anything else is dated that day.
SWEEP_TIME = time(0, 1)
# Pack prices drift, but stay within this share of the seeded price.
PRICE_BAND_PER_MILLE = 150
# How far a day's usage of an item is from its average: -10% to +20%.
USAGE_NOISE_PER_MILLE = (-100, 200)
# A line the owner raises when ordering gets 30–70% more packs.
OVER_ORDER_PER_MILLE = (300, 700)
# A short delivery brings 40–80% of each line, rounded down to whole packs.
SHORT_DELIVERY_PER_MILLE = (400, 800)
# At closing on its expiry date, what is left of a batch is wasted with this
# chance. Otherwise it stays on the shelf, expired, for the morning check.
CLOSING_WASTE_PER_MILLE = 800
# The morning after its expiry date, an expired batch is wasted with this
# chance; from the third day it always is.
EXPIRED_WASTE_PER_MILLE = 700
EXPIRED_WASTE_BY_DAY = 3
# Usage entries end this long before closing, so a mistake's correction still
# falls on the same day, before the count (D80).
LAST_ENTRY_BEFORE_CLOSE = timedelta(minutes=30)
# ANALYZE after day 0 (the opening stock) and then every this many days (D75).
ANALYZE_EVERY_DAYS = 7


class SimulationError(Exception):
    """A simulated day failed. Days before it are committed; that day is rolled back."""


@dataclass
class SimulationSummary:
    """What a run wrote, counted from the domain functions' results.

    The last three are read from the alerts and orders tables when the run
    ends: alerts are raised and resolved inside the stock writes and the
    sweep, and draft orders by the par-level rule, not by a call made here.
    """

    opening_day: date
    end_date: date
    days: int
    random_seed: int
    receipts: int = 0
    late_deliveries: int = 0
    short_deliveries: int = 0
    orders_sent: int = 0
    orders_received: int = 0
    orders_cancelled: int = 0
    counts: int = 0
    # Count lines whose counted quantity differed from the ledger's.
    count_variances: int = 0
    movements_by_type: Counter[str] = field(default_factory=Counter)
    waste_by_reason: Counter[str] = field(default_factory=Counter)
    # Usage entries (not mistakes) that took a default batch (further) negative.
    shortfalls_by_sku: Counter[str] = field(default_factory=Counter)
    expired_by_sku: Counter[str] = field(default_factory=Counter)
    reversals: int = 0
    alerts_raised: Counter[str] = field(default_factory=Counter)
    alerts_open: Counter[str] = field(default_factory=Counter)
    # Orders that have lines, by status; a draft here is waiting for its order day.
    orders_by_status: Counter[str] = field(default_factory=Counter)

    @property
    def first_day(self) -> date:
        return self.opening_day + timedelta(days=1)

    @property
    def shortfalls(self) -> int:
        return self.shortfalls_by_sku.total()

    @property
    def settle_pairs(self) -> int:
        return self.movements_by_type["settle"] // 2


# --- The catalogue, as plain data ---------------------------------------------------
# Commits expire ORM objects, so the simulator keeps what it needs in dataclasses.


@dataclass(frozen=True, slots=True)
class _Supplier:
    id: UUID
    spec: SupplierSpec


@dataclass(frozen=True, slots=True)
class _Item:
    id: UUID
    unit_id: UUID
    factor_to_base: Decimal
    pack_price: Decimal  # the preferred supplier_item's seeded price
    supplier: _Supplier
    shelf_life_days: int | None
    spec: ItemSpec

    @property
    def sku(self) -> str:
        return self.spec.sku


# --- Events -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Sweep:
    def describe(self) -> str:
        return "sweep alerts and draft orders"


@dataclass(frozen=True, slots=True)
class _MorningCheck:
    day: date

    def describe(self) -> str:
        return "morning check for expired batches"


@dataclass(frozen=True, slots=True)
class _ClosingCheck:
    day: date

    def describe(self) -> str:
        return "closing check for batches that expire today"


@dataclass(frozen=True, slots=True)
class _Delivery:
    supplier: _Supplier
    # The order it fulfils; None for the opening stock.
    order_id: UUID | None
    invoice_ref: str
    lines: tuple[ReceiptLineInput, ...]
    late: bool
    short: bool
    client_id: UUID

    def describe(self) -> str:
        notes = "".join(
            f", {note}" for note, on in (("late", self.late), ("short", self.short)) if on
        )
        return f"receive {self.invoice_ref} ({len(self.lines)} lines{notes})"


@dataclass(frozen=True, slots=True)
class _Cancellation:
    """A delivery that brought nothing: the owner cancels the order (D77)."""

    supplier: _Supplier
    order_id: UUID

    def describe(self) -> str:
        return f"cancel the order to {self.supplier.spec.key}: nothing was delivered"


@dataclass(frozen=True, slots=True)
class _Usage:
    item: _Item
    quantity: Decimal
    client_id: UUID
    # A wrong quantity, reversed a few minutes later and logged again.
    mistake: bool

    def describe(self) -> str:
        mistake = " (a mistake, to be reversed)" if self.mistake else ""
        return f"use {self.quantity} of {self.item.sku}{mistake}"


@dataclass(frozen=True, slots=True)
class _Reverse:
    item: _Item
    original_client_id: UUID
    client_id: UUID

    def describe(self) -> str:
        return f"reverse usage {self.original_client_id} of {self.item.sku}"


@dataclass(frozen=True, slots=True)
class _Spoilage:
    item: _Item
    # Capped at the chosen batch's on-hand when it runs.
    quantity: int
    client_id: UUID

    def describe(self) -> str:
        return f"waste up to {self.quantity} of {self.item.sku} ({self.item.spec.spoil_reason})"


@dataclass(frozen=True, slots=True)
class _SendOrders:
    day: date

    def describe(self) -> str:
        return "send the day's orders"


@dataclass(frozen=True, slots=True)
class _Count:
    items: tuple[_Item, ...]
    client_id: UUID

    def describe(self) -> str:
        return f"count {len(self.items)} items"


type _Action = (
    _Sweep
    | _MorningCheck
    | _ClosingCheck
    | _Delivery
    | _Cancellation
    | _Usage
    | _Reverse
    | _Spoilage
    | _SendOrders
    | _Count
)


@dataclass(frozen=True, slots=True)
class _Event:
    local_time: datetime  # naive, in the venue's timezone
    action: _Action


# --- Entry point --------------------------------------------------------------------


def simulate(
    session: Session,
    business_id: UUID,
    *,
    venue: Venue = CAFE,
    days: int = 30,
    end_date: date | None = None,
    random_seed: int = DEFAULT_RANDOM_SEED,
) -> SimulationSummary:
    """Simulate the venue's `days` days ending on end_date, plus the opening day 0.

    The business must have been seeded from the same venue (seed.py). end_date
    defaults to today in the business's timezone. Commits after each day.
    """
    if days < 1:
        raise ValueError(f"days must be at least 1, got {days}")
    run = _Run(session, business_id, venue, random.Random(random_seed))
    if end_date is None:
        end_date = run.local_today()
    summary = SimulationSummary(
        opening_day=end_date - timedelta(days=days),
        end_date=end_date,
        days=days,
        random_seed=random_seed,
    )
    run.run(summary)
    return summary


class _Run:
    def __init__(self, session: Session, business_id: UUID, venue: Venue, rng: random.Random):
        self.session = session
        self.business_id = business_id
        self.venue = venue
        self.rng = rng
        self.location_id = self._load_location()
        self.suppliers, self.items = self._load_catalogue()
        self.by_id = {item.id: item for item in self.items}
        # Current pack prices; they drift a little per delivery.
        self.prices: dict[str, Decimal] = {item.sku: item.pack_price for item in self.items}
        # Shrinkage (D78): base units of each item that have left without a movement.
        self.loss: dict[str, Decimal] = {item.sku: ZERO for item in self.items}
        # Deliveries and cancellations planned when an order was sent, each
        # with the local time it happens at.
        self.arrivals: list[_Event] = []
        self.summary: SimulationSummary
        # The event being run and the request being made, for the error message.
        self.event = ""
        self.doing = ""

    # --- Loading ----------------------------------------------------------------

    def _load_location(self) -> UUID:
        ids = self.session.scalars(
            select(Location.id).where(Location.business_id == self.business_id)
        ).all()
        if len(ids) != 1:
            raise ValueError(f"expected the business to have one location, found {len(ids)}")
        return ids[0]

    def _load_catalogue(self) -> tuple[list[_Supplier], list[_Item]]:
        """The venue's items, matched by SKU, and each one's preferred supplier_item.

        A venue supplier is the supplier its items' preferred supplier_items name,
        so suppliers are found through their items, never by display name.
        """
        specs = {spec.sku: spec for spec in self.venue.items}
        rows = self.session.execute(
            select(
                Item.sku,
                Item.id,
                Item.tracks_batches,
                Item.tracks_expiry,
                Item.shelf_life_days,
                SupplierItem.supplier_id,
                SupplierItem.item_unit_id,
                SupplierItem.pack_price,
                ItemUnit.factor_to_base,
            )
            .join(
                SupplierItem,
                and_(
                    SupplierItem.business_id == Item.business_id,
                    SupplierItem.item_id == Item.id,
                    SupplierItem.is_preferred,
                ),
            )
            .join(
                ItemUnit,
                and_(
                    ItemUnit.business_id == SupplierItem.business_id,
                    ItemUnit.id == SupplierItem.item_unit_id,
                ),
            )
            .where(Item.business_id == self.business_id, Item.sku.in_(specs))
            .order_by(Item.sku)
        ).all()
        found = {row.sku: row for row in rows}
        missing = sorted(specs.keys() - found.keys())
        if missing:
            raise ValueError(
                f"the business has no item with a preferred supplier for: {', '.join(missing)}"
            )

        supplier_ids: dict[str, set[UUID]] = {}
        for sku, row in found.items():
            spec = specs[sku]
            if (row.tracks_batches, row.tracks_expiry) != (spec.tracks_batches, spec.tracks_expiry):
                raise ValueError(f"item {sku} tracks batches or expiry differently from the venue")
            supplier_ids.setdefault(spec.supplier, set()).add(row.supplier_id)

        suppliers = []
        by_key: dict[str, _Supplier] = {}
        for spec in self.venue.suppliers:
            ids = supplier_ids.get(spec.key, set())
            if not ids:
                continue
            if len(ids) != 1:
                raise ValueError(f"the items of supplier {spec.key} have different suppliers")
            if spec.lead_time_days < 1:
                # An order is sent after closing, so it can't arrive the same day.
                raise ValueError(f"supplier {spec.key} needs a lead time of at least one day")
            supplier = _Supplier(ids.pop(), spec)
            if any(s.id == supplier.id for s in suppliers):
                raise ValueError(f"supplier {spec.key} shares its supplier row with another")
            suppliers.append(supplier)
            by_key[spec.key] = supplier

        items = [
            _Item(
                id=row.id,
                unit_id=row.item_unit_id,
                factor_to_base=row.factor_to_base,
                pack_price=row.pack_price,
                supplier=by_key[specs[row.sku].supplier],
                shelf_life_days=row.shelf_life_days,
                spec=specs[row.sku],
            )
            for row in rows
        ]
        return suppliers, items

    def local_today(self) -> date:
        return self.session.scalar(
            text(
                "SELECT (now() AT TIME ZONE timezone)::date FROM businesses WHERE id = :business_id"
            ),
            {"business_id": self.business_id},
        )

    # --- Days -------------------------------------------------------------------

    def run(self, summary: SimulationSummary) -> None:
        self.summary = summary
        for index in range(summary.days + 1):
            day = summary.opening_day + timedelta(days=index)
            self.doing = "planning the day"
            try:
                if index == 0:
                    events = self._plan_opening(day)
                else:
                    events = self._plan_day(day, last=index == summary.days)
                for occurred_at, event in self._timed(events):
                    self.event = f"{event.action.describe()} at {event.local_time:%H:%M:%S}"
                    self.doing = self.event
                    self._execute(event.action, occurred_at)
                self.doing = "commit"
                self.session.commit()
                # Not after the last day: nothing follows that would use it,
                # and it would leave a transaction open that nobody commits.
                if index % ANALYZE_EVERY_DAYS == 0 and index < summary.days:
                    self.doing = "analyze"
                    self.session.execute(text("ANALYZE"))
            except Exception as exc:
                self.session.rollback()
                raise SimulationError(f"day {index} ({day}): {self.doing}: {exc}") from exc
        self._tally()

    def _timed(self, events: Sequence[_Event]) -> list[tuple[datetime, _Event]]:
        """The events with their local times as timestamptz, in time order.

        One query converts the whole day in Postgres, in the business's timezone.
        Equal times keep their planning order (sorted is stable).
        """
        if not events:
            return []
        instants = self.session.scalars(
            text(
                """
                SELECT t.local_time AT TIME ZONE b.timezone
                FROM businesses AS b
                CROSS JOIN unnest(CAST(:local_times AS timestamp[]))
                    WITH ORDINALITY AS t(local_time, n)
                WHERE b.id = :business_id
                ORDER BY t.n
                """
            ),
            {"local_times": [e.local_time for e in events], "business_id": self.business_id},
        ).all()
        return sorted(zip(instants, events, strict=True), key=lambda pair: pair[0])

    def _tally(self) -> None:
        """Read what the rules did over the run: alerts by type, and orders by status."""
        alerts = self.session.execute(
            select(Alert.type, func.count(), func.count().filter(Alert.status == "open"))
            .where(Alert.business_id == self.business_id)
            .group_by(Alert.type)
        )
        for alert_type, raised, still_open in alerts:
            self.summary.alerts_raised[alert_type] = raised
            if still_open:
                self.summary.alerts_open[alert_type] = still_open
        has_lines = exists().where(
            PurchaseOrderLine.business_id == PurchaseOrder.business_id,
            PurchaseOrderLine.po_id == PurchaseOrder.id,
        )
        orders = self.session.execute(
            select(PurchaseOrder.status, func.count())
            .where(PurchaseOrder.business_id == self.business_id, has_lines)
            .group_by(PurchaseOrder.status)
        )
        for status, count in orders:
            self.summary.orders_by_status[status] = count

    # --- Planning ---------------------------------------------------------------

    def _plan_opening(self, day: date) -> list[_Event]:
        """Day 0: the first sweep, then one delivery per supplier with every item
        at its order-up-to level (D76)."""
        events = [_Event(datetime.combine(day, SWEEP_TIME), _Sweep())]
        for supplier in self.suppliers:
            spec = supplier.spec
            lines = []
            for item in self._items_of(supplier):
                packs = (Decimal(item.spec.order_up_to) / item.factor_to_base).to_integral_value(
                    ROUND_CEILING
                )
                lines.append(self._delivery_line(item, packs, day))
            when = self._local_time(day, spec.window_start, spec.window_end)
            delivery = _Delivery(
                supplier,
                None,
                self._invoice_ref(supplier, day),
                tuple(lines),
                late=False,
                short=False,
                client_id=self._client_id(),
            )
            events.append(_Event(when, delivery))
        return events

    def _plan_day(self, day: date, *, last: bool) -> list[_Event]:
        """The day's events. Deliveries were planned when their orders were sent."""
        venue = self.venue
        events = [
            _Event(datetime.combine(day, SWEEP_TIME), _Sweep()),
            _Event(datetime.combine(day, venue.morning_check), _MorningCheck(day)),
        ]

        delivery_times: set[datetime] = set()
        due = [event for event in self.arrivals if event.local_time.date() == day]
        self.arrivals = [event for event in self.arrivals if event.local_time.date() != day]
        for event in due:
            when = self._unique(event.local_time, delivery_times)
            events.append(_Event(when, event.action))

        weekday_factor = Decimal(venue.weekday_per_mille[day.weekday()]) / MILLE
        for item in self.items:
            events.extend(self._plan_usage(item, day, weekday_factor))
            events.extend(self._plan_spoilage(item, day))
            self._plan_theft(item)

        events.append(_Event(datetime.combine(day, venue.closes), _ClosingCheck(day)))
        events.append(_Event(datetime.combine(day, venue.order_time), _SendOrders(day)))
        # No count on the last day: it may run past the current time (D41),
        # and a count dated tonight would make the API refuse writes for
        # these items until then (D51, D78).
        counted = tuple(item for item in self.items if item.spec.count_weekday == day.weekday())
        if counted and not last:
            count = _Count(counted, self._client_id())
            events.append(_Event(datetime.combine(day, venue.count_time), count))
        return events

    def _delivery_line(self, item: _Item, packs: Decimal, scheduled_on: date) -> ReceiptLineInput:
        """One delivery line: that many packs at today's price, dated from the
        day the delivery was meant to come."""
        spec = item.spec
        expires_on = None
        if spec.tracks_expiry:
            expires_on = scheduled_on + timedelta(days=item.shelf_life_days)
        lot_code = None
        if spec.tracking == "lot":
            lot_code = f"{item.supplier.spec.key}-{scheduled_on:%y%m%d}"
        return ReceiptLineInput(
            item_id=item.id,
            item_unit_id=item.unit_id,
            qty_packs=packs,
            pack_price=self._drift_price(item),
            expires_on=expires_on,
            lot_code=lot_code,
        )

    def _drift_price(self, item: _Item) -> Decimal:
        """A small random step from the last price, kept within the band around the seed price."""
        step = item.supplier.spec.price_step_per_mille
        price = self.prices[item.sku] * (MILLE + self._draw(-step, step)) / MILLE
        band = item.pack_price * PRICE_BAND_PER_MILLE / MILLE
        price = min(max(price, item.pack_price - band), item.pack_price + band)
        price = price.quantize(MONEY_STEP, ROUND_HALF_UP)
        self.prices[item.sku] = price
        return price

    @staticmethod
    def _invoice_ref(supplier: _Supplier, scheduled_on: date) -> str:
        # From the scheduled date, not the arrival date, so a late delivery
        # never repeats the ref of that supplier's next delivery.
        return f"{supplier.spec.key}-{scheduled_on:%Y%m%d}"

    def _plan_usage(self, item: _Item, day: date, weekday_factor: Decimal) -> list[_Event]:
        """The day's usage of one item, in one to three entries, in whole base units."""
        spec = item.spec
        noise = (MILLE + self._draw(*USAGE_NOISE_PER_MILLE)) / MILLE
        total = int((spec.daily_usage * weekday_factor * noise).to_integral_value(ROUND_HALF_UP))
        if total < 1:
            return []
        entries = min(self._draw(1, 3), total)
        cuts = sorted(self.rng.sample(range(1, total), entries - 1))
        parts = [b - a for a, b in zip([0, *cuts], [*cuts, total], strict=True)]
        close = datetime.combine(day, self.venue.closes) - LAST_ENTRY_BEFORE_CLOSE
        times = sorted(self._local_time(day, self.venue.opens, close.time()) for _ in parts)

        events = []
        for when, part in zip(times, parts, strict=True):
            if not self._chance(self.venue.mistake_per_mille):
                events.append(_Event(when, _Usage(item, Decimal(part), self._client_id(), False)))
                continue
            wrong = _Usage(item, Decimal(self._wrong_quantity(part)), self._client_id(), True)
            reversed_at = when + timedelta(minutes=self._draw(2, 10))
            relogged_at = reversed_at + timedelta(seconds=self._draw(20, 90))
            events += [
                _Event(when, wrong),
                _Event(reversed_at, _Reverse(item, wrong.client_id, self._client_id())),
                _Event(relogged_at, _Usage(item, Decimal(part), self._client_id(), False)),
            ]
        return events

    def _wrong_quantity(self, quantity: int) -> int:
        """A typo: ten times too much, double, or half (one more for a single unit)."""
        match self._draw(0, 2):
            case 0:
                return quantity * 10
            case 1:
                return quantity * 2
            case _:
                return quantity // 2 if quantity > 1 else quantity + 1

    def _plan_spoilage(self, item: _Item, day: date) -> list[_Event]:
        spec = item.spec
        if not self._chance(spec.spoil_per_mille):
            return []
        largest = max(1, spec.daily_usage * spec.spoil_max_per_mille // 1000)
        when = self._local_time(day, self.venue.opens, self.venue.closes)
        return [_Event(when, _Spoilage(item, self._draw(1, largest), self._client_id()))]

    def _plan_theft(self, item: _Item) -> None:
        """Some days a little of an item goes missing: no movement, only loss (D78)."""
        spec = item.spec
        if self._chance(spec.theft_per_mille):
            largest = max(1, spec.daily_usage * spec.theft_max_per_mille // 1000)
            self.loss[item.sku] += self._draw(1, largest)

    # --- Running ----------------------------------------------------------------

    def _execute(self, action: _Action, occurred_at: datetime) -> None:
        match action:
            case _Sweep():
                sweep(self.session, self.business_id, occurred_at)
            case _MorningCheck(day):
                self._waste_expired(day, occurred_at)
            case _ClosingCheck(day):
                self._waste_expiring_today(day, occurred_at)
            case _Delivery():
                self._receive(action, occurred_at)
            case _Cancellation():
                cancel_order(
                    self.session,
                    self.business_id,
                    order_id=action.order_id,
                    cancelled_at=occurred_at,
                )
                self.summary.orders_cancelled += 1
            case _Usage():
                self._use(action, occurred_at)
            case _Reverse():
                result = reverse(
                    self.session,
                    self.business_id,
                    original_client_id=action.original_client_id,
                    client_id=action.client_id,
                    source=SOURCE,
                )
                self.summary.reversals += 1
                self._tally_movements(result.movements)
            case _Spoilage():
                self._spoil(action, occurred_at)
            case _SendOrders(day):
                self._send_orders(day, occurred_at)
            case _Count():
                self._count(action, occurred_at)

    def _receive(self, delivery: _Delivery, occurred_at: datetime) -> None:
        result = receive(
            self.session,
            self.business_id,
            location_id=self.location_id,
            supplier_id=delivery.supplier.id,
            received_at=occurred_at,
            invoice_ref=delivery.invoice_ref,
            lines=delivery.lines,
            client_id=delivery.client_id,
            source=SOURCE,
            po_id=delivery.order_id,
        )
        self.summary.receipts += 1
        self.summary.late_deliveries += delivery.late
        self.summary.short_deliveries += delivery.short
        if delivery.order_id is not None:
            self.summary.orders_received += 1
        self._tally_movements(result.movements)

    def _use(self, usage: _Usage, occurred_at: datetime) -> None:
        result = use(
            self.session,
            self.business_id,
            location_id=self.location_id,
            item_id=usage.item.id,
            quantity=usage.quantity,
            occurred_at=occurred_at,
            client_id=usage.client_id,
            source=SOURCE,
        )
        if not usage.mistake:
            if result.shortfall:
                self.summary.shortfalls_by_sku[usage.item.sku] += 1
            # Over-portioning: a share more than was logged left the shelf (D78).
            spec = usage.item.spec
            self.loss[spec.sku] += usage.quantity * spec.over_portion_per_mille / MILLE
        self._tally_movements(result.movements)

    def _waste_expired(self, day: date, occurred_at: datetime) -> None:
        """Waste each batch with an open expired alert: usually now, at the latest
        on the third day past its expiry date (D79)."""
        for item, batch_id, expires_on, on_hand in self._alerted_batches("expired"):
            days_past = (day - expires_on).days
            if days_past < EXPIRED_WASTE_BY_DAY and not self._chance(EXPIRED_WASTE_PER_MILLE):
                continue
            self._waste(item, batch_id, on_hand, "expired", occurred_at, self._client_id())
            self.summary.expired_by_sku[item.sku] += 1

    def _waste_expiring_today(self, day: date, occurred_at: datetime) -> None:
        """At closing, waste most of what expires today (D79).

        It is usable until midnight and can't be sold tomorrow. Tonight's
        orders are sized from usable on-hand, so stock left on the books here
        would be ordered short by that much. What isn't wasted now has an
        expired alert by the morning check.
        """
        for item, batch_id, expires_on, on_hand in self._alerted_batches("expiring"):
            if expires_on != day or not self._chance(CLOSING_WASTE_PER_MILLE):
                continue
            self._waste(item, batch_id, on_hand, "expired", occurred_at, self._client_id())
            self.summary.expired_by_sku[item.sku] += 1

    def _alerted_batches(self, alert_type: str) -> list[tuple[_Item, UUID, date, Decimal]]:
        """The batches with an open alert of that type, with what they hold:
        (item, batch id, expires on, on hand), by SKU and then oldest first."""
        rows = self.session.execute(
            select(Batch.item_id, Batch.id, Batch.expires_on, StockBalance.qty_on_hand)
            .select_from(Alert)
            .join(Batch, and_(Batch.business_id == Alert.business_id, Batch.id == Alert.batch_id))
            .join(
                StockBalance,
                and_(
                    StockBalance.business_id == Batch.business_id, StockBalance.batch_id == Batch.id
                ),
            )
            .join(Item, and_(Item.business_id == Batch.business_id, Item.id == Batch.item_id))
            .where(
                Alert.business_id == self.business_id,
                Alert.location_id == self.location_id,
                Alert.type == alert_type,
                Alert.status == "open",
            )
            .order_by(Item.sku, Batch.expires_on, Batch.received_at)
        )
        return [
            (self.by_id[item_id], batch_id, expires_on, on_hand)
            for item_id, batch_id, expires_on, on_hand in rows
        ]

    def _spoil(self, spoilage: _Spoilage, occurred_at: datetime) -> None:
        """Waste from a random batch with stock, never more than it holds."""
        item = spoilage.item
        if item.spec.tracks_batches:
            batches = self._batches_with_stock(item)
            if not batches:
                return
            batch_id, on_hand = batches[self.rng.randrange(len(batches))]
        else:
            batch_id = None
            on_hand = self._default_on_hand(item)
            if on_hand <= 0:
                return
        quantity = min(Decimal(spoilage.quantity), on_hand)
        self._waste(
            item, batch_id, quantity, item.spec.spoil_reason, occurred_at, spoilage.client_id
        )

    def _batches_with_stock(self, item: _Item) -> list[tuple[UUID, Decimal]]:
        """The item's received batches with stock on hand, as (batch id, on hand),
        earliest expiry first."""
        rows = self.session.execute(
            select(Batch.id, StockBalance.qty_on_hand)
            .join(
                StockBalance,
                and_(
                    StockBalance.business_id == Batch.business_id, StockBalance.batch_id == Batch.id
                ),
            )
            .where(
                Batch.business_id == self.business_id,
                Batch.location_id == self.location_id,
                Batch.item_id == item.id,
                ~Batch.is_default,
                StockBalance.qty_on_hand > 0,
            )
            .order_by(Batch.expires_on.asc().nulls_last(), Batch.received_at)
        )
        return [(batch_id, on_hand) for batch_id, on_hand in rows]

    def _default_on_hand(self, item: _Item) -> Decimal:
        """What the item's default batch holds; zero if it has none yet."""
        on_hand = self.session.scalar(
            select(StockBalance.qty_on_hand)
            .join(
                Batch,
                and_(
                    Batch.business_id == StockBalance.business_id,
                    Batch.id == StockBalance.batch_id,
                ),
            )
            .where(
                Batch.business_id == self.business_id,
                Batch.location_id == self.location_id,
                Batch.item_id == item.id,
                Batch.is_default,
            )
        )
        return ZERO if on_hand is None else on_hand

    def _waste(
        self,
        item: _Item,
        batch_id: UUID | None,
        quantity: Decimal,
        reason: str,
        occurred_at: datetime,
        client_id: UUID,
    ) -> None:
        self.doing = f"{self.event}: waste {quantity} of {item.sku} ({reason})"
        result = waste(
            self.session,
            self.business_id,
            location_id=self.location_id,
            item_id=item.id,
            batch_id=batch_id,
            quantity=quantity,
            reason=reason,
            occurred_at=occurred_at,
            client_id=client_id,
            source=SOURCE,
        )
        self.summary.waste_by_reason[reason] += 1
        self._tally_movements(result.movements)

    def _tally_movements(self, movements) -> None:
        for movement in movements:
            self.summary.movements_by_type[movement.type] += 1

    # --- Ordering (D77) ---------------------------------------------------------

    def _send_orders(self, day: date, occurred_at: datetime) -> None:
        """The owner, after closing: send the draft of each supplier whose order day it is.

        The lines are the par-level rule's. Now and then the owner raises one
        (the item's over-order chance), which is where overstock and expired
        waste come from. A supplier with nothing on its draft isn't ordered from.
        """
        for supplier in self.suppliers:
            if day.weekday() not in supplier.spec.order_weekdays:
                continue
            lines = self._draft_lines(supplier)
            if not lines:
                continue
            order_id = lines[0][0]
            for _, line_id, item, suggested_packs in lines:
                if not self._chance(item.spec.over_order_per_mille):
                    continue
                extra = Decimal(suggested_packs) * self._draw(*OVER_ORDER_PER_MILLE) / MILLE
                qty_packs = suggested_packs + int(extra.to_integral_value(ROUND_CEILING))
                self.doing = f"{self.event}: order {qty_packs} packs of {item.sku}"
                set_line_quantity(
                    self.session,
                    self.business_id,
                    order_id=order_id,
                    line_id=line_id,
                    qty_packs=qty_packs,
                )
            self.doing = f"{self.event}: send the order to {supplier.spec.key}"
            send_order(self.session, self.business_id, order_id=order_id, sent_at=occurred_at)
            self.summary.orders_sent += 1
            self._plan_delivery(supplier, order_id, day)

    def _draft_lines(self, supplier: _Supplier) -> list[tuple[UUID, UUID, _Item, int]]:
        """The lines of the supplier's draft, by SKU: (order id, line id, item, suggested packs)."""
        rows = self.session.execute(
            select(
                PurchaseOrder.id,
                PurchaseOrderLine.id,
                PurchaseOrderLine.item_id,
                PurchaseOrderLine.suggested_packs,
            )
            .join(
                PurchaseOrderLine,
                and_(
                    PurchaseOrderLine.business_id == PurchaseOrder.business_id,
                    PurchaseOrderLine.po_id == PurchaseOrder.id,
                ),
            )
            .join(
                Item,
                and_(
                    Item.business_id == PurchaseOrderLine.business_id,
                    Item.id == PurchaseOrderLine.item_id,
                ),
            )
            .where(
                PurchaseOrder.business_id == self.business_id,
                PurchaseOrder.location_id == self.location_id,
                PurchaseOrder.supplier_id == supplier.id,
                PurchaseOrder.status == "draft",
            )
            .order_by(Item.sku)
        )
        return [
            (order_id, line_id, self.by_id[item_id], suggested_packs)
            for order_id, line_id, item_id, suggested_packs in rows
        ]

    def _plan_delivery(self, supplier: _Supplier, order_id: UUID, sent_on: date) -> None:
        """Plan the delivery of an order just sent: its day, whether it is late
        or short, and its lines. It is received when its time comes."""
        spec = supplier.spec
        scheduled_on = self._delivery_day(spec, sent_on)
        short = self._chance(spec.short_per_mille)
        lines = []
        for item, packs in self._ordered(order_id):
            if short:
                packs = packs * self._draw(*SHORT_DELIVERY_PER_MILLE) // 1000
            if packs > 0:
                lines.append(self._delivery_line(item, Decimal(packs), scheduled_on))

        late = self._chance(spec.late_per_mille)
        if not late:
            when = self._local_time(scheduled_on, spec.window_start, spec.window_end)
        elif self._chance(500):
            when = self._local_time(scheduled_on, *self.venue.late_same_day)
        else:
            when = self._local_time(scheduled_on + timedelta(days=1), *self.venue.late_next_day)

        if lines:
            action: _Action = _Delivery(
                supplier,
                order_id,
                self._invoice_ref(supplier, scheduled_on),
                tuple(lines),
                late=late,
                short=short,
                client_id=self._client_id(),
            )
        else:
            # Every line came to nothing, and a receipt needs a line.
            action = _Cancellation(supplier, order_id)
        self.arrivals.append(_Event(when, action))

    def _ordered(self, order_id: UUID) -> list[tuple[_Item, int]]:
        """What a sent order orders, by SKU: (item, packs)."""
        rows = self.session.execute(
            select(PurchaseOrderLine.item_id, PurchaseOrderLine.qty_packs)
            .join(
                Item,
                and_(
                    Item.business_id == PurchaseOrderLine.business_id,
                    Item.id == PurchaseOrderLine.item_id,
                ),
            )
            .where(
                PurchaseOrderLine.business_id == self.business_id,
                PurchaseOrderLine.po_id == order_id,
            )
            .order_by(Item.sku)
        )
        return [(self.by_id[item_id], qty_packs) for item_id, qty_packs in rows]

    @staticmethod
    def _delivery_day(spec: SupplierSpec, sent_on: date) -> date:
        """The first delivery weekday at least lead_time_days after the order was sent."""
        for offset in range(spec.lead_time_days, spec.lead_time_days + 7):
            candidate = sent_on + timedelta(days=offset)
            if candidate.weekday() in spec.weekdays:
                return candidate
        raise ValueError(f"supplier {spec.key} has no delivery weekdays")

    # --- Counting (D78) ---------------------------------------------------------

    def _count(self, count: _Count, occurred_at: datetime) -> None:
        """Count the day's items: what the ledger holds, less what went unlogged.

        The loss is found in whole base units; the fraction of a unit carries
        over to the next count. A batch-tracked item loses from its
        earliest-expiring batches first, as usage draws them. Loss beyond what
        the ledger holds is dropped: nothing more can be missing than was there.
        """
        counted = []
        for item in count.items:
            loss = self.loss[item.sku].to_integral_value(ROUND_FLOOR)
            self.loss[item.sku] -= loss
            if item.spec.tracks_batches:
                batches = []
                for batch_id, on_hand in self._batches_with_stock(item):
                    gone = min(loss, on_hand)
                    loss -= gone
                    batches.append(BatchCountInput(batch_id, on_hand - gone))
                counted.append(CountItemInput(item.id, batches=batches))
            else:
                found = max(ZERO, self._default_on_hand(item) - loss)
                counted.append(CountItemInput(item.id, counted_qty=found))
        result = count_stock(
            self.session,
            self.business_id,
            location_id=self.location_id,
            counted_at=occurred_at,
            items=counted,
            client_id=count.client_id,
            source=SOURCE,
        )
        self.summary.counts += 1
        self.summary.count_variances += len(result.movements)
        self._tally_movements(result.movements)

    # --- Helpers ----------------------------------------------------------------

    def _items_of(self, supplier: _Supplier) -> list[_Item]:
        return [item for item in self.items if item.supplier is supplier]

    def _draw(self, low: int, high: int) -> int:
        """A random integer from low to high, both included."""
        return self.rng.randint(low, high)

    def _chance(self, per_mille: int) -> bool:
        return self.rng.randrange(1000) < per_mille

    def _client_id(self) -> UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def _local_time(self, day: date, start: time, end: time) -> datetime:
        """A random second in [start, end) on day, as a naive local datetime."""
        low = start.hour * 3600 + start.minute * 60 + start.second
        high = end.hour * 3600 + end.minute * 60 + end.second
        return datetime.combine(day, time()) + timedelta(seconds=self.rng.randrange(low, high))

    @staticmethod
    def _unique(when: datetime, taken: set[datetime]) -> datetime:
        """The first free second from `when`, so no two deliveries share a time."""
        while when in taken:
            when += timedelta(seconds=1)
        taken.add(when)
        return when
