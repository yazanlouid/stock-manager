"""The day-by-day simulator: replays a venue's days through the domain functions.

It writes stock only through receive, use, waste and reverse, with source
"simulator" and no user. It reads batches and balances to decide what to do,
but never writes a table itself.

Days. days=30 means the 30 days ending on end_date (D38). Day 0, the day before
them, holds the opening stock: one delivery from each supplier, sized to last
until its first standing delivery, because every receive needs a receipt line.
Each later day runs, in time order:
- a morning check that wastes expired batches (reason expired), usually the
  morning after the expiry date and sometimes a day or two later, so FEFO has
  to skip expired stock that is still on hand;
- standing deliveries on their weekdays (D40), a few of them late;
- each item's usage, in one to three entries across opening hours, a few of
  them mistakes that are reversed a few minutes later and logged again;
- random spoilage from a batch with stock.

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

Timezones. Dates are the venue's local dates. Local times become timestamptz
in Postgres, in one query per day, as use() does for usage dates (D32).
"""

import random
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import and_, select, text
from sqlalchemy.orm import Session

from app.domain.stock import ReceiptLineInput, receive, reverse, use, waste
from app.models import Batch, Item, ItemUnit, Location, StockBalance, SupplierItem
from app.sim.cafe import CAFE, ItemSpec, SupplierSpec, Venue

DEFAULT_RANDOM_SEED = 20261110
SOURCE = "simulator"

MILLE = Decimal(1000)
MONEY_STEP = Decimal("0.001")
# Pack prices drift, but stay within this share of the seeded price.
PRICE_BAND_PER_MILLE = 150
# Usage noise leans upward, for over-portioning: -10% to +20%.
USAGE_NOISE_PER_MILLE = (-100, 200)
# Standing order noise: -6% to +6%.
ORDER_NOISE_PER_MILLE = (-60, 60)
# An over-ordered line gets 30–70% more packs.
OVER_ORDER_PER_MILLE = (300, 700)
# The morning after its expiry date, an expired batch is wasted with this
# chance; from the third day it always is.
EXPIRED_WASTE_PER_MILLE = 700
EXPIRED_WASTE_BY_DAY = 3
# Usage entries end this long before closing, so a mistake's correction still
# falls on the same day.
LAST_ENTRY_BEFORE_CLOSE = timedelta(minutes=30)


class SimulationError(Exception):
    """A simulated day failed. Days before it are committed; that day is rolled back."""


@dataclass
class SimulationSummary:
    """What a run wrote, counted from the domain functions' results."""

    opening_day: date
    end_date: date
    days: int
    random_seed: int
    receipts: int = 0
    late_deliveries: int = 0
    movements_by_type: Counter[str] = field(default_factory=Counter)
    waste_by_reason: Counter[str] = field(default_factory=Counter)
    # Usage entries (not mistakes) that took a default batch (further) negative.
    shortfalls_by_sku: Counter[str] = field(default_factory=Counter)
    expired_by_sku: Counter[str] = field(default_factory=Counter)
    reversals: int = 0

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
class _MorningCheck:
    day: date

    def describe(self) -> str:
        return "morning check for expired batches"


@dataclass(frozen=True, slots=True)
class _Delivery:
    supplier: _Supplier
    invoice_ref: str
    lines: tuple[ReceiptLineInput, ...]
    late: bool
    client_id: UUID

    def describe(self) -> str:
        late = ", late" if self.late else ""
        return f"receive {self.invoice_ref} ({len(self.lines)} lines{late})"


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


type _Action = _MorningCheck | _Delivery | _Usage | _Reverse | _Spoilage


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
        # Each line's rounding to whole packs, carried to its next delivery.
        self.carry: dict[str, Decimal] = {item.sku: Decimal(0) for item in self.items}
        # Current pack prices; they drift a little per delivery.
        self.prices: dict[str, Decimal] = {item.sku: item.pack_price for item in self.items}
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
        late: list[_Event] = []
        for index in range(summary.days + 1):
            day = summary.opening_day + timedelta(days=index)
            self.doing = "planning the day"
            try:
                if index == 0:
                    events = self._plan_opening(day)
                else:
                    events, late = self._plan_day(day, late, last=index == summary.days)
                for occurred_at, event in self._timed(events):
                    self.event = f"{event.action.describe()} at {event.local_time:%H:%M:%S}"
                    self.doing = self.event
                    self._execute(event.action, occurred_at)
                self.doing = "commit"
                self.session.commit()
            except Exception as exc:
                self.session.rollback()
                raise SimulationError(f"day {index} ({day}): {self.doing}: {exc}") from exc

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

    # --- Planning ---------------------------------------------------------------

    def _plan_opening(self, day: date) -> list[_Event]:
        """Day 0: one delivery per supplier, enough to reach its first standing delivery."""
        events = []
        for supplier in self.suppliers:
            first = self._next_delivery(supplier.spec, day)
            lines = []
            for item in self._items_of(supplier):
                cover = self._days(day + timedelta(days=1), first, extra=item.spec.safety_days)
                line = self._plan_line(item, day, cover, standing=False)
                if line is not None:
                    lines.append(line)
            if lines:
                when = self._local_time(day, supplier.spec.window_start, supplier.spec.window_end)
                events.append(_Event(when, self._delivery(supplier, day, lines, late=False)))
        return events

    def _plan_day(
        self, day: date, late_from_yesterday: list[_Event], *, last: bool
    ) -> tuple[list[_Event], list[_Event]]:
        """The day's events, and the deliveries that slip to tomorrow."""
        venue = self.venue
        events = [_Event(datetime.combine(day, venue.morning_check), _MorningCheck(day))]
        delivery_times = set()
        for event in late_from_yesterday:
            arrival = datetime.combine(day, event.local_time.time())
            events.append(_Event(self._unique(arrival, delivery_times), event.action))

        late_tomorrow = []
        for supplier in self.suppliers:
            spec = supplier.spec
            if day.weekday() not in spec.weekdays:
                continue
            cover = self._days(day, self._next_delivery(spec, day))
            lines = [
                line
                for item in self._items_of(supplier)
                if (line := self._plan_line(item, day, cover, standing=True)) is not None
            ]
            if not lines:
                # Every line rounded to zero packs, and a receipt needs a line.
                continue
            is_late = self._chance(spec.late_per_mille)
            delivery = self._delivery(supplier, day, lines, late=is_late)
            if not is_late:
                window = (spec.window_start, spec.window_end)
            elif last or self._chance(500):
                window = venue.late_same_day
            else:
                # Tomorrow's arrival time; tomorrow's plan makes it unique.
                when = self._local_time(day, *venue.late_next_day)
                late_tomorrow.append(_Event(when, delivery))
                continue
            when = self._unique(self._local_time(day, *window), delivery_times)
            events.append(_Event(when, delivery))

        weekday_factor = Decimal(venue.weekday_per_mille[day.weekday()]) / MILLE
        for item in self.items:
            events.extend(self._plan_usage(item, day, weekday_factor))
            events.extend(self._plan_spoilage(item, day))
        return events, late_tomorrow

    def _plan_line(
        self, item: _Item, day: date, cover: list[date], *, standing: bool
    ) -> ReceiptLineInput | None:
        """One delivery line in whole packs, or None if it rounds to zero packs.

        The order is expected usage over the cover days, plus the item's buffer,
        plus (for standing orders) noise, plus the carry. Rounding up to whole
        packs moves the excess into the carry, which the next delivery orders
        less for, so big packs neither run dry (each delivery covers what was
        wanted) nor pile up (the excess stays under one pack). An over-ordered line adds packs
        on top and doesn't touch the carry.
        """
        spec = item.spec
        want = self._expected_usage(spec, cover) * (MILLE + spec.buffer_per_mille) / MILLE
        if standing:
            want = want * (MILLE + self._draw(*ORDER_NOISE_PER_MILLE)) / MILLE
        want += self.carry[item.sku]
        packs = max(Decimal(0), (want / item.factor_to_base).to_integral_value(ROUND_CEILING))
        self.carry[item.sku] = (want - packs * item.factor_to_base).quantize(MONEY_STEP)
        if standing and self._chance(spec.over_order_per_mille):
            extra = max(packs, Decimal(1)) * self._draw(*OVER_ORDER_PER_MILLE) / MILLE
            packs += extra.to_integral_value(ROUND_CEILING)
        if packs == 0:
            return None

        expires_on = None
        if spec.tracks_expiry:
            expires_on = day + timedelta(days=item.shelf_life_days)
        lot_code = f"{item.supplier.spec.key}-{day:%y%m%d}" if spec.tracking == "lot" else None
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

    def _delivery(
        self, supplier: _Supplier, scheduled_on: date, lines: list[ReceiptLineInput], *, late: bool
    ) -> _Delivery:
        # The ref comes from the scheduled date, not the arrival date, so a late
        # delivery never repeats the ref of that supplier's next delivery.
        invoice_ref = f"{supplier.spec.key}-{scheduled_on:%Y%m%d}"
        if late:
            self.summary.late_deliveries += 1
        return _Delivery(supplier, invoice_ref, tuple(lines), late, self._client_id())

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

    # --- Running ----------------------------------------------------------------

    def _execute(self, action: _Action, occurred_at: datetime) -> None:
        match action:
            case _MorningCheck(day):
                self._waste_expired(day, occurred_at)
            case _Delivery():
                self._receive(action, occurred_at)
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
                self._count(result.movements)
            case _Spoilage():
                self._spoil(action, occurred_at)

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
        )
        self.summary.receipts += 1
        self._count(result.movements)

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
        if result.shortfall and not usage.mistake:
            self.summary.shortfalls_by_sku[usage.item.sku] += 1
        self._count(result.movements)

    def _waste_expired(self, day: date, occurred_at: datetime) -> None:
        """Waste each expired batch still on hand: usually now, at the latest on day 3."""
        by_id = {item.id: item for item in self.items}
        rows = self.session.execute(
            self._batches_with_stock()
            .where(Batch.expires_on < day)
            .order_by(Item.sku, Batch.expires_on, Batch.received_at)
        ).all()
        for batch_id, item_id, expires_on, on_hand in rows:
            item = by_id.get(item_id)
            if item is None:
                continue
            days_past = (day - expires_on).days
            if days_past < EXPIRED_WASTE_BY_DAY and not self._chance(EXPIRED_WASTE_PER_MILLE):
                continue
            self._waste(item, batch_id, on_hand, "expired", occurred_at, self._client_id())
            self.summary.expired_by_sku[item.sku] += 1

    def _spoil(self, spoilage: _Spoilage, occurred_at: datetime) -> None:
        """Waste from a random batch with stock, never more than it holds."""
        item = spoilage.item
        if item.spec.tracks_batches:
            batches = self.session.execute(
                self._batches_with_stock()
                .where(Batch.item_id == item.id)
                .order_by(Batch.expires_on.asc().nulls_last(), Batch.received_at)
            ).all()
            if not batches:
                return
            batch_id, _, _, on_hand = batches[self.rng.randrange(len(batches))]
        else:
            batch_id = None
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
            if on_hand is None or on_hand <= 0:
                return
        quantity = min(Decimal(spoilage.quantity), on_hand)
        self._waste(
            item, batch_id, quantity, item.spec.spoil_reason, occurred_at, spoilage.client_id
        )

    def _batches_with_stock(self):
        """This location's non-default batches with stock on hand (batch, item, expiry, on hand)."""
        return (
            select(Batch.id, Batch.item_id, Batch.expires_on, StockBalance.qty_on_hand)
            .join(
                StockBalance,
                and_(
                    StockBalance.business_id == Batch.business_id, StockBalance.batch_id == Batch.id
                ),
            )
            .join(Item, and_(Item.business_id == Batch.business_id, Item.id == Batch.item_id))
            .where(
                Batch.business_id == self.business_id,
                Batch.location_id == self.location_id,
                ~Batch.is_default,
                StockBalance.qty_on_hand > 0,
            )
        )

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
        self._count(result.movements)

    def _count(self, movements) -> None:
        for movement in movements:
            self.summary.movements_by_type[movement.type] += 1

    # --- Helpers ----------------------------------------------------------------

    def _items_of(self, supplier: _Supplier) -> list[_Item]:
        return [item for item in self.items if item.supplier is supplier]

    def _expected_usage(self, spec: ItemSpec, days: list[date]) -> Decimal:
        factors = sum(self.venue.weekday_per_mille[d.weekday()] for d in days)
        return Decimal(spec.daily_usage) * factors / MILLE

    @staticmethod
    def _next_delivery(spec: SupplierSpec, day: date) -> date:
        for offset in range(1, 8):
            candidate = day + timedelta(days=offset)
            if candidate.weekday() in spec.weekdays:
                return candidate
        raise ValueError(f"supplier {spec.key} has no delivery weekdays")

    @staticmethod
    def _days(start: date, stop: date, *, extra: int = 0) -> list[date]:
        """The days from start up to, not including, stop, then `extra` more."""
        count = (stop - start).days + extra
        return [start + timedelta(days=n) for n in range(count)]

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
