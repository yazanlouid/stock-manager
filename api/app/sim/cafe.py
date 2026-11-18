"""The demo café as plain data: catalogue, suppliers and how the café behaves.

Every name here is made up; none is a real business. The shape of a venue,
and how par levels are given in days of usage, are in venue.py.

For the bakery, which takes an order every evening for the next morning, the
reorder level is one day and a margin. For the dry goods supplier, which is
ordered from once a week and takes three days, it is eleven and a half days.
"""

from datetime import time

from app.sim.venue import (
    EVERY_DAY,
    MON,
    SAT,
    SUN,
    THU,
    TUE,
    WED,
    ItemSpec,
    SupplierSpec,
    UserSpec,
    Venue,
    make_item,
)

# --- Item constructors, one per kind of stock -------------------------------------


def _dairy(sku, name, base_unit, unit_name, factor, price, daily, shelf, **kw) -> ItemSpec:
    # Ordered most evenings for the next morning, with one two-day gap a week:
    # reorder at 2.5 days, up to 3.5, low below 1 day. A little over-pouring
    # at the bar.
    kw = {
        "low_stock_days_per_mille": 1000,
        "reorder_days_per_mille": 2500,
        "order_up_to_days_per_mille": 3500,
        "count_weekday": SUN,
        "over_order_per_mille": 40,
        "spoil_per_mille": 30,
        "over_portion_per_mille": 30,
    } | kw
    return make_item(
        sku, name, "dairy", base_unit, "expiry", unit_name, factor, price, "DAI", daily,
        shelf_life_days=shelf, **kw,
    )  # fmt: skip


def _pastry(sku, name, unit_name, factor, price, daily, shelf, **kw) -> ItemSpec:
    # Ordered every evening for the next morning: reorder at just over a day,
    # up to a day and a half, so a busy Friday still has enough; low below a
    # third of a day, which a normal evening keeps. Now and then one is eaten
    # without being logged.
    kw = {
        "low_stock_days_per_mille": 330,
        "reorder_days_per_mille": 1150,
        "order_up_to_days_per_mille": 1550,
        "count_weekday": MON,
        "over_order_per_mille": 80,
        "spoil_per_mille": 40,
        "theft_per_mille": 60,
        "theft_max_per_mille": 150,
    } | kw
    return make_item(
        sku, name, "bakery", "piece", "expiry", unit_name, factor, price, "BAK", daily,
        shelf_life_days=shelf, **kw,
    )  # fmt: skip


def _produce(sku, name, unit_name, factor, price, daily, shelf, **kw) -> ItemSpec:
    # Three orders a week, up to three days apart, and the longest gap is the
    # busy weekend: reorder at 4 days, up to 4.8, low below 1.5. Trimmings and
    # bruised fruit go in the bin unlogged.
    kw = {
        "low_stock_days_per_mille": 1500,
        "reorder_days_per_mille": 4000,
        "order_up_to_days_per_mille": 4800,
        "count_weekday": WED,
        "over_order_per_mille": 80,
        "spoil_per_mille": 60,
        "over_portion_per_mille": 50,
    } | kw
    return make_item(
        sku, name, "produce", "g", "expiry", unit_name, factor, price, "PRO", daily,
        shelf_life_days=shelf, **kw,
    )  # fmt: skip


def _beans(sku, name, unit_name, factor, price, daily, **kw) -> ItemSpec:
    # Two orders a week with two days' lead: reorder at 6.5 days, up to 9.5,
    # low below 2.5. Heavy-handed dosing, and the odd bag that walks.
    kw = {
        "low_stock_days_per_mille": 2500,
        "reorder_days_per_mille": 6500,
        "order_up_to_days_per_mille": 9500,
        "count_weekday": SAT,
        "spoil_per_mille": 5,
        "over_portion_per_mille": 40,
        "theft_per_mille": 30,
        "theft_max_per_mille": 100,
    } | kw
    return make_item(sku, name, "coffee", "g", "lot", unit_name, factor, price, "ROA", daily, **kw)


def _dry(sku, name, base_unit, unit_name, factor, price, daily, **kw) -> ItemSpec:
    # One order a week with three days' lead: reorder at 11.5 days, up to 18,
    # low below 4. Syrups and sauces are poured generously.
    kw = {
        "low_stock_days_per_mille": 4000,
        "reorder_days_per_mille": 11500,
        "order_up_to_days_per_mille": 18000,
        "count_weekday": THU,
        "spoil_per_mille": 8,
        "over_portion_per_mille": 60,
    } | kw
    return make_item(
        sku, name, "dry goods", base_unit, "none", unit_name, factor, price, "DRY", daily, **kw
    )


def _packaging(sku, name, unit_name, factor, price, daily, **kw) -> ItemSpec:
    # One order a week with three days' lead, like dry goods. Double-cupping
    # and dropped lids aren't logged.
    kw = {
        "low_stock_days_per_mille": 4000,
        "reorder_days_per_mille": 11500,
        "order_up_to_days_per_mille": 18000,
        "count_weekday": TUE,
        "spoil_per_mille": 10,
        "spoil_reason": "damaged",
        "over_portion_per_mille": 20,
    } | kw
    return make_item(
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
    order_time=time(23, 10),
    count_time=time(23, 30),
    # Jordan's weekend is Friday–Saturday, and Thursday evening is busy too.
    weekday_per_mille=(950, 940, 960, 1130, 1250, 1200, 950),
    late_same_day=(time(14, 0), time(18, 0)),
    late_next_day=(time(7, 30), time(9, 30)),
    mistake_per_mille=20,
    suppliers=(
        # Delivers six mornings a week, so it is ordered from every evening but Thursday.
        SupplierSpec(
            "DAI",
            "Hilltop Dairy",
            1,
            (MON, TUE, WED, THU, SAT, SUN),
            time(6, 45),
            time(8, 30),
            late_per_mille=60,
            short_per_mille=40,
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
            short_per_mille=40,
            price_step_per_mille=10,
        ),
        # Ordered on Monday, Wednesday and Saturday evenings.
        SupplierSpec(
            "PRO",
            "Green Valley Produce",
            1,
            (TUE, THU, SUN),
            time(8, 0),
            time(10, 0),
            late_per_mille=80,
            short_per_mille=60,
            price_step_per_mille=60,
        ),
        # Ordered on Monday and Friday evenings.
        SupplierSpec(
            "ROA",
            "Ember Roasters",
            2,
            (WED, SUN),
            time(9, 0),
            time(11, 0),
            late_per_mille=60,
            short_per_mille=30,
            price_step_per_mille=15,
        ),
        # Ordered on Friday evening for Monday.
        SupplierSpec(
            "DRY",
            "Pantry Wholesale",
            3,
            (MON,),
            time(10, 0),
            time(12, 0),
            late_per_mille=50,
            short_per_mille=30,
            price_step_per_mille=20,
        ),
        # Ordered on Sunday evening for Wednesday.
        SupplierSpec(
            "PAC",
            "PackRight Supplies",
            3,
            (WED,),
            time(11, 0),
            time(13, 0),
            late_per_mille=50,
            short_per_mille=30,
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
        _dairy(
            "DAI-HALLOUMI",
            "Halloumi",
            "g",
            "250 g pack",
            250,
            "1.500",
            1200,
            30,
            theft_per_mille=30,
            theft_max_per_mille=200,
        ),  # fmt: skip
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
