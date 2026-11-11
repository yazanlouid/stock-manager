"""The demo café as plain data: catalogue, suppliers and how the café behaves.

seed.py writes the catalogue part of a Venue; simulator.py reads the behaviour
part (usage, delivery days, noise). Week 2 adds the sweets shop and the cloud
kitchen as more Venue values, without new code.

Every name here is made up; none is a real business. Numbers are per mille
(thousandths) or whole base units, so the simulator can draw integers and
scale them into Decimal without floats.
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
    """A supplier and its standing order (D40)."""

    # Short code, used in invoice refs and to link items to their supplier.
    key: str
    name: str
    lead_time_days: int
    # Weekdays the standing order arrives (date.weekday(): Monday is 0).
    weekdays: tuple[int, ...]
    # Local time window for an on-time delivery.
    window_start: time
    window_end: time
    # Chance a delivery arrives late (later that day or the next morning).
    late_per_mille: int
    # Largest pack price change per delivery, as a share of the current price.
    price_step_per_mille: int


@dataclass(frozen=True, slots=True)
class ItemSpec:
    """An item, its one pack unit and preferred supplier, and how the café uses it."""

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
    pack_price: Decimal  # JOD, three decimals
    supplier: str  # SupplierSpec.key
    # Base units used on an average day (weekday factor 1000).
    daily_usage: int
    # How far above expected usage the standing order is sized.
    buffer_per_mille: int
    # Extra days of usage in the opening stock, on top of the cover until the
    # first standing delivery.
    safety_days: int
    # Chance a delivery line is over-ordered by 30–70%.
    over_order_per_mille: int
    # Chance per day of a spoilage, and its largest size as a share of daily usage.
    spoil_per_mille: int
    spoil_max_per_mille: int
    spoil_reason: str  # spoiled, or damaged for packaging

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
    # Usage factor per weekday (date.weekday() order), 1000 = an average day.
    weekday_per_mille: tuple[int, ...]
    # Local windows for late deliveries: later the same day, or the next morning.
    late_same_day: tuple[time, time]
    late_next_day: tuple[time, time]
    # Chance a usage entry is logged with a wrong quantity, then reversed.
    mistake_per_mille: int
    suppliers: tuple[SupplierSpec, ...]
    items: tuple[ItemSpec, ...]


# --- Item constructors, one per kind of stock -------------------------------------


def _item(
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
    shelf_life_days: int | None = None,
    buffer_per_mille: int = 100,
    safety_days: int = 3,
    over_order_per_mille: int = 0,
    spoil_per_mille: int = 15,
    spoil_max_per_mille: int = 100,
    spoil_reason: str = "spoiled",
) -> ItemSpec:
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
        buffer_per_mille=buffer_per_mille,
        safety_days=safety_days,
        over_order_per_mille=over_order_per_mille,
        spoil_per_mille=spoil_per_mille,
        spoil_max_per_mille=spoil_max_per_mille,
        spoil_reason=spoil_reason,
    )


def _dairy(sku, name, base_unit, unit_name, factor, price, daily, shelf, **kw) -> ItemSpec:
    kw = {"safety_days": 1, "over_order_per_mille": 40, "spoil_per_mille": 30} | kw
    return _item(
        sku, name, "dairy", base_unit, "expiry", unit_name, factor, price, "DAI", daily,
        shelf_life_days=shelf, **kw,
    )  # fmt: skip


def _pastry(sku, name, unit_name, factor, price, daily, shelf, **kw) -> ItemSpec:
    kw = {
        "buffer_per_mille": 40,
        "safety_days": 1,
        "over_order_per_mille": 80,
        "spoil_per_mille": 40,
    } | kw
    return _item(
        sku, name, "bakery", "piece", "expiry", unit_name, factor, price, "BAK", daily,
        shelf_life_days=shelf, **kw,
    )  # fmt: skip


def _produce(sku, name, unit_name, factor, price, daily, shelf, **kw) -> ItemSpec:
    kw = {
        "buffer_per_mille": 80,
        "safety_days": 1,
        "over_order_per_mille": 80,
        "spoil_per_mille": 60,
    } | kw
    return _item(
        sku, name, "produce", "g", "expiry", unit_name, factor, price, "PRO", daily,
        shelf_life_days=shelf, **kw,
    )  # fmt: skip


def _beans(sku, name, unit_name, factor, price, daily, **kw) -> ItemSpec:
    kw = {"spoil_per_mille": 5} | kw
    return _item(sku, name, "coffee", "g", "lot", unit_name, factor, price, "ROA", daily, **kw)


def _dry(sku, name, base_unit, unit_name, factor, price, daily, **kw) -> ItemSpec:
    kw = {"spoil_per_mille": 8} | kw
    return _item(
        sku, name, "dry goods", base_unit, "none", unit_name, factor, price, "DRY", daily, **kw
    )


def _packaging(sku, name, unit_name, factor, price, daily, **kw) -> ItemSpec:
    kw = {"spoil_per_mille": 10, "spoil_reason": "damaged"} | kw
    return _item(
        sku, name, "packaging", "piece", "none", unit_name, factor, price, "PAC", daily, **kw
    )


# --- The café ---------------------------------------------------------------------

# A café of about 250 drinks a day, plus pastries and a few breakfast plates.
CAFE = Venue(
    name="Dar Zaatar Café",
    timezone="Asia/Amman",
    currency="JOD",
    location_name="Weibdeh branch",
    location_kind="branch",
    users=(
        UserSpec("owner@darzaatar.example", "Lina Haddad", "owner"),
        UserSpec("staff@darzaatar.example", "Omar Saleh", "staff"),
    ),
    opens=time(7, 0),
    closes=time(23, 0),
    morning_check=time(6, 30),
    # Jordan's weekend is Friday–Saturday, and Thursday evening is busy too.
    weekday_per_mille=(950, 940, 960, 1130, 1250, 1200, 950),
    late_same_day=(time(14, 0), time(18, 0)),
    late_next_day=(time(7, 30), time(9, 30)),
    mistake_per_mille=20,
    suppliers=(
        SupplierSpec(
            "DAI",
            "Hilltop Dairy",
            1,
            (MON, TUE, WED, THU, SAT, SUN),
            time(6, 45),
            time(8, 30),
            late_per_mille=60,
            price_step_per_mille=15,
        ),
        SupplierSpec(
            "BAK",
            "Crumb & Co Bakery",
            1,
            EVERY_DAY,
            time(6, 40),
            time(7, 30),
            late_per_mille=50,
            price_step_per_mille=10,
        ),
        SupplierSpec(
            "PRO",
            "Green Valley Produce",
            1,
            (TUE, THU, SUN),
            time(8, 0),
            time(10, 0),
            late_per_mille=80,
            price_step_per_mille=60,
        ),
        SupplierSpec(
            "ROA",
            "Ember Roasters",
            2,
            (WED, SUN),
            time(9, 0),
            time(11, 0),
            late_per_mille=60,
            price_step_per_mille=15,
        ),
        SupplierSpec(
            "DRY",
            "Pantry Wholesale",
            3,
            (MON,),
            time(10, 0),
            time(12, 0),
            late_per_mille=50,
            price_step_per_mille=20,
        ),
        SupplierSpec(
            "PAC",
            "PackRight Supplies",
            3,
            (WED,),
            time(11, 0),
            time(13, 0),
            late_per_mille=50,
            price_step_per_mille=10,
        ),
    ),  # fmt: skip
    items=(
        # Dairy: batches with expiry, delivered six days a week.
        _dairy("DAI-MILK-WHOLE", "Whole milk", "ml", "crate of 12 × 1 L", 12000, "9.600", 22000, 7),
        _dairy("DAI-MILK-SKIM", "Skimmed milk", "ml", "1 L carton", 1000, "0.850", 3000, 7),
        _dairy("DAI-MILK-OAT", "Oat milk", "ml", "1 L carton", 1000, "2.400", 2500, 90),
        _dairy("DAI-MILK-LF", "Lactose-free milk", "ml", "1 L carton", 1000, "1.200", 1000, 14),
        _dairy("DAI-CREAM", "Whipping cream", "ml", "1 L carton", 1000, "3.500", 600, 10),
        _dairy("DAI-YOGHURT", "Plain yoghurt", "g", "1 kg tub", 1000, "1.800", 1500, 14),
        _dairy("DAI-LABNEH", "Labneh", "g", "1 kg tub", 1000, "3.200", 800, 10),
        _dairy("DAI-HALLOUMI", "Halloumi", "g", "250 g pack", 250, "1.500", 1200, 30),
        # Bakery: pastries with a shelf life of a few days, delivered daily.
        _pastry("BAK-CROISSANT", "Butter croissant", "tray of 12", 12, "4.200", 45, 2),
        _pastry("BAK-PAIN-CHOC", "Pain au chocolat", "tray of 12", 12, "4.800", 25, 2),
        _pastry("BAK-MUFFIN", "Blueberry muffin", "box of 6", 6, "3.000", 18, 3),
        _pastry("BAK-CINNAMON", "Cinnamon roll", "tray of 6", 6, "3.300", 15, 2),
        _pastry("BAK-KAAK", "Sesame ka'ak", "bag of 10", 10, "2.000", 20, 1),
        _pastry("BAK-COOKIE", "Chocolate chip cookie", "box of 12", 12, "3.600", 20, 5),
        # Produce: fruit and mint with expiry, three deliveries a week.
        _produce("PRO-LEMON", "Lemons", "5 kg bag", 5000, "4.500", 1500, 10),
        _produce("PRO-ORANGE", "Juice oranges", "10 kg box", 10000, "6.000", 4000, 10),
        _produce("PRO-MINT", "Fresh mint", "500 g box", 500, "1.000", 400, 3),
        _produce("PRO-BANANA", "Bananas", "6 kg box", 6000, "4.200", 1500, 5),
        _produce("PRO-STRAWBERRY", "Strawberries", "2 kg tray", 2000, "5.000", 800, 4),
        # Coffee: batches by roast lot, no expiry.
        _beans("ROA-ESPRESSO", "House espresso blend", "1 kg bag", 1000, "14.000", 4000),
        _beans("ROA-ETHIOPIA", "Ethiopia single origin", "1 kg bag", 1000, "19.000", 600),
        _beans("ROA-DECAF", "Decaf blend", "1 kg bag", 1000, "16.000", 300),
        _beans("ROA-ARABIC", "Arabic coffee with cardamom", "500 g bag", 500, "6.000", 500),
        # Dry goods: no batch tracking, delivered weekly.
        _dry("DRY-SUGAR", "White sugar", "g", "10 kg sack", 10000, "6.500", 1500),
        _dry("DRY-SUGAR-STICK", "Brown sugar sticks", "piece", "box of 1000", 1000, "9.000", 120),
        _dry("DRY-SYR-VANILLA", "Vanilla syrup", "ml", "750 ml bottle", 750, "4.500", 400),
        _dry("DRY-SYR-CARAMEL", "Caramel syrup", "ml", "750 ml bottle", 750, "4.500", 450),
        _dry("DRY-SYR-HAZELNUT", "Hazelnut syrup", "ml", "750 ml bottle", 750, "4.500", 250),
        _dry("DRY-CHOC-SAUCE", "Chocolate sauce", "ml", "1.89 L bottle", 1890, "7.500", 350),
        _dry("DRY-COCOA", "Cocoa powder", "g", "1 kg tin", 1000, "8.000", 250),
        _dry("DRY-MATCHA", "Matcha powder", "g", "500 g tin", 500, "18.000", 60),
        _dry("DRY-TEA-BLACK", "Black tea bags", "piece", "box of 100", 100, "3.000", 40),
        _dry("DRY-HONEY", "Wildflower honey", "g", "1 kg jar", 1000, "9.000", 150),
        # Packaging: no batch tracking, delivered weekly; damage instead of spoilage.
        _packaging("PAC-CUP-8OZ", "Paper cup 8 oz", "case of 1000", 1000, "35.000", 90),
        _packaging("PAC-CUP-12OZ", "Paper cup 12 oz", "case of 1000", 1000, "42.000", 110),
        _packaging("PAC-CUP-COLD", "Clear cold cup 16 oz", "case of 1000", 1000, "55.000", 60),
        _packaging("PAC-LID-HOT", "Hot cup lid", "case of 1000", 1000, "18.000", 200),
        _packaging("PAC-LID-COLD", "Cold cup dome lid", "case of 1000", 1000, "20.000", 60),
        _packaging("PAC-STRAW", "Paper straw", "box of 500", 500, "6.000", 70),
        _packaging("PAC-NAPKIN", "Paper napkin", "pack of 500", 500, "2.500", 250),
    ),
)
