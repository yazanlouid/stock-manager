"""The shape of a demo venue: its catalogue, suppliers and behaviour, as plain data.

Each venue module (cafe.py, sweets.py, kitchen.py) is one Venue value built
from these. seed.py writes the catalogue part and the par levels; simulator.py
reads the behaviour part (usage, ordering, deliveries, shrinkage, counts).

Numbers are per mille (thousandths) or whole base units, so the simulator can
draw integers and scale them into Decimal without floats.

Par levels (D76, D83) are given per kind of item, in days of average usage:
when the item is short (low stock), when to reorder and how far to order up.
The owner orders after closing and deliveries come in the morning, so the
reorder level has to cover
- the supplier's lead time, less the day the order is placed on;
- the longest gap between that supplier's order days, because an item that
  is just above its reorder level tonight waits until the next order day;
- a margin for busy days.
The low-stock level is below what is normally left just before a delivery
comes, so the alert means a late or short delivery or a busy day.
"""

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

# date.weekday() numbers.
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
EVERY_DAY = (MON, TUE, WED, THU, FRI, SAT, SUN)


@dataclass(frozen=True, slots=True)
class UserSpec:
    email: str
    name: str
    role: str  # owner or staff


@dataclass(frozen=True, slots=True)
class SupplierSpec:
    """A supplier, when it delivers and how reliably."""

    # Short code, used in invoice refs and to link items to their supplier.
    key: str
    name: str
    # Days between sending an order and its delivery; at least 1.
    lead_time_days: int
    # Weekdays it delivers on (date.weekday(): Monday is 0).
    weekdays: tuple[int, ...]
    # Local time window for an on-time delivery.
    window_start: time
    window_end: time
    # Chance a delivery arrives late (later that day or the next morning).
    late_per_mille: int
    # Chance a delivery comes short: 40 to 80% of each line, in whole packs.
    short_per_mille: int
    # Largest pack price change per delivery, as a share of the current price.
    price_step_per_mille: int

    @property
    def order_weekdays(self) -> tuple[int, ...]:
        """The weekdays to order on: each delivery weekday, lead_time_days earlier (D77)."""
        return tuple(sorted({(weekday - self.lead_time_days) % 7 for weekday in self.weekdays}))


@dataclass(frozen=True, slots=True)
class ItemSpec:
    """An item, its one pack unit and preferred supplier, and how the venue uses it."""

    sku: str
    name: str
    category: str
    base_unit: str  # g, ml or piece
    # "expiry": batches with expiry dates; "lot": batches by lot code, no expiry;
    # "none": no batch tracking (the default batch only).
    tracking: str
    shelf_life_days: int | None
    unit_name: str
    factor_to_base: int
    pack_price: Decimal  # in the venue's currency, three decimals
    supplier: str  # SupplierSpec.key
    # Base units used on an average day (weekday factor 1000).
    daily_usage: int
    # The par level, in base units (D76): reorder at or below reorder_point,
    # up to order_up_to; low stock (a real shortage) at or below
    # low_stock_level (D83).
    low_stock_level: int
    reorder_point: int
    order_up_to: int
    # Chance the owner raises a line of this item by 30–70% when ordering.
    over_order_per_mille: int
    # Chance per day of a spoilage, and its largest size as a share of daily usage.
    spoil_per_mille: int
    spoil_max_per_mille: int
    spoil_reason: str  # spoiled, or damaged for packaging
    # Shrinkage (D78): stock that leaves without being logged, found by counts.
    # Over-portioning is a share of every logged usage; theft is a chance per
    # day, and its largest size a share of daily usage.
    over_portion_per_mille: int
    theft_per_mille: int
    theft_max_per_mille: int
    # The weekday this item is counted on, after closing.
    count_weekday: int

    @property
    def tracks_batches(self) -> bool:
        return self.tracking != "none"

    @property
    def tracks_expiry(self) -> bool:
        return self.tracking == "expiry"


@dataclass(frozen=True, slots=True)
class Venue:
    name: str
    timezone: str
    currency: str
    location_name: str
    location_kind: str
    users: tuple[UserSpec, ...]
    # Usage is logged between opens and closes, local time.
    opens: time
    closes: time
    # When the morning check wastes expired batches, before any delivery.
    morning_check: time
    # After closing: when the owner sends the day's orders, and then when the
    # day's count is taken, so the count is the last thing dated that day.
    order_time: time
    count_time: time
    # Usage factor per weekday (date.weekday() order), 1000 = an average day.
    weekday_per_mille: tuple[int, ...]
    # Local windows for late deliveries: later the same day, or the next morning.
    late_same_day: tuple[time, time]
    late_next_day: tuple[time, time]
    # Chance a usage entry is logged with a wrong quantity, then reversed.
    mistake_per_mille: int
    suppliers: tuple[SupplierSpec, ...]
    items: tuple[ItemSpec, ...]


# --- Building items ------------------------------------------------------------


def make_item(
    sku: str,
    name: str,
    category: str,
    base_unit: str,
    tracking: str,
    unit_name: str,
    factor_to_base: int,
    pack_price: str,
    supplier: str,
    daily_usage: int,
    *,
    low_stock_days_per_mille: int,
    reorder_days_per_mille: int,
    order_up_to_days_per_mille: int,
    count_weekday: int,
    shelf_life_days: int | None = None,
    over_order_per_mille: int = 0,
    spoil_per_mille: int = 15,
    spoil_max_per_mille: int = 100,
    spoil_reason: str = "spoiled",
    over_portion_per_mille: int = 0,
    theft_per_mille: int = 0,
    theft_max_per_mille: int = 0,
) -> ItemSpec:
    """An ItemSpec, with its par level worked out from days of average usage.

    reorder_days_per_mille 2500 means "reorder at two and a half days of usage".
    The low-stock level is below what is normally left just before a delivery
    comes, so the alert means a late or short delivery or a busy day (D83).
    """
    return ItemSpec(
        sku=sku,
        name=name,
        category=category,
        base_unit=base_unit,
        tracking=tracking,
        shelf_life_days=shelf_life_days,
        unit_name=unit_name,
        factor_to_base=factor_to_base,
        pack_price=Decimal(pack_price),
        supplier=supplier,
        daily_usage=daily_usage,
        low_stock_level=daily_usage * low_stock_days_per_mille // 1000,
        reorder_point=daily_usage * reorder_days_per_mille // 1000,
        order_up_to=daily_usage * order_up_to_days_per_mille // 1000,
        over_order_per_mille=over_order_per_mille,
        spoil_per_mille=spoil_per_mille,
        spoil_max_per_mille=spoil_max_per_mille,
        spoil_reason=spoil_reason,
        over_portion_per_mille=over_portion_per_mille,
        theft_per_mille=theft_per_mille,
        theft_max_per_mille=theft_max_per_mille,
        count_weekday=count_weekday,
    )


def item_kind(category: str, base_unit: str, tracking: str, supplier: str, **defaults):
    """A constructor for one kind of item at a venue: every item of the kind
    shares its category, supplier, par-level days, count day and shrinkage,
    and any of them can be overridden per item (base_unit and tracking too).

    The constructor takes (sku, name, unit_name, factor_to_base, pack_price,
    daily_usage, shelf_life_days=None, **overrides).
    """

    def build(
        sku: str,
        name: str,
        unit_name: str,
        factor_to_base: int,
        pack_price: str,
        daily_usage: int,
        shelf_life_days: int | None = None,
        **overrides,
    ) -> ItemSpec:
        options = {"base_unit": base_unit, "tracking": tracking} | defaults | overrides
        return make_item(
            sku,
            name,
            category,
            options.pop("base_unit"),
            options.pop("tracking"),
            unit_name,
            factor_to_base,
            pack_price,
            supplier,
            daily_usage,
            shelf_life_days=shelf_life_days,
            **options,
        )

    return build
