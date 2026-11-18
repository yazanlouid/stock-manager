"""The demo cloud kitchen as plain data (D86).

Sahtein Cloud Kitchen cooks Levantine grills and bowls for delivery apps only,
in Dubai, so it keeps its books in dirhams and its days in UTC+4. Meat keeps
two to four days and is delivered six mornings a week; bread comes every
morning; packaging goes out with every order. Its sauce supplier delivers
single tubs and buckets, so a short delivery there can come to nothing, and
the owner cancels the order. Its dry goods use all three kinds of tracking:
frozen fries by expiry date, spices by lot, rice and oil not at all.

Every name here is made up; none is a real business. The shape of a venue,
and how par levels are given in days of usage, are in venue.py.
"""

from datetime import time

from app.sim.venue import (
    EVERY_DAY,
    FRI,
    MON,
    SAT,
    SUN,
    THU,
    TUE,
    WED,
    SupplierSpec,
    UserSpec,
    Venue,
    item_kind,
)

# Six mornings a week (not Sunday), ordered the evening before; the longest
# gap is Saturday night to Monday morning. Reorder at 2.2 days, up to 3, low
# below 0.8. Heavy-handed portions, and the odd lamb shoulder that walks.
_meat = item_kind(
    "meat",
    "g",
    "expiry",
    "MEA",
    low_stock_days_per_mille=800,
    reorder_days_per_mille=2200,
    order_up_to_days_per_mille=3000,
    count_weekday=MON,
    over_order_per_mille=50,
    spoil_per_mille=30,
    over_portion_per_mille=50,
)
# Four mornings a week, ordered the evening before, two days apart at most:
# reorder at 3 days, up to 4, low below 1.2. Trimmings go in the bin unlogged.
_produce = item_kind(
    "produce",
    "g",
    "expiry",
    "VEG",
    low_stock_days_per_mille=1200,
    reorder_days_per_mille=3000,
    order_up_to_days_per_mille=4000,
    count_weekday=TUE,
    over_order_per_mille=80,
    spoil_per_mille=60,
    over_portion_per_mille=60,
)
# Every morning: reorder at 1.2 days, up to 1.6, low below 0.3.
_bread = item_kind(
    "bread",
    "piece",
    "expiry",
    "BRD",
    low_stock_days_per_mille=300,
    reorder_days_per_mille=1200,
    order_up_to_days_per_mille=1600,
    count_weekday=WED,
    over_order_per_mille=60,
    spoil_per_mille=50,
    over_portion_per_mille=30,
)
# Monday and Thursday mornings, ordered the evening before, four days apart
# at most: reorder at 5 days, up to 7, low below 1.5.
_sauce = item_kind(
    "sauces",
    "g",
    "expiry",
    "SAU",
    low_stock_days_per_mille=1500,
    reorder_days_per_mille=5000,
    order_up_to_days_per_mille=7000,
    count_weekday=THU,
    spoil_per_mille=10,
    over_portion_per_mille=40,
)
# Tuesday and Saturday with two days' lead (ordered Sunday and Thursday):
# reorder at 6.5 days, up to 9.5, low below 2.5. Untracked unless the item
# says otherwise.
_dry = item_kind(
    "dry goods",
    "g",
    "none",
    "DRY",
    low_stock_days_per_mille=2500,
    reorder_days_per_mille=6500,
    order_up_to_days_per_mille=9500,
    count_weekday=SUN,
    spoil_per_mille=8,
    over_portion_per_mille=40,
)
# Weekly on Sunday with three days' lead (ordered Thursday): reorder at 11.5
# days, up to 18, low below 4. Extra containers and bags go out unlogged.
_packaging = item_kind(
    "packaging",
    "piece",
    "none",
    "PKG",
    low_stock_days_per_mille=4000,
    reorder_days_per_mille=11500,
    order_up_to_days_per_mille=18000,
    count_weekday=SAT,
    spoil_per_mille=10,
    spoil_reason="damaged",
    over_portion_per_mille=30,
)

# About 220 delivery orders a day, busiest on Thursday to Saturday evenings.
KITCHEN = Venue(
    name="Sahtein Cloud Kitchen",
    timezone="Asia/Dubai",
    currency="AED",
    location_name="Al Quoz kitchen",
    location_kind="branch",
    users=(
        UserSpec("owner@sahtein.example", "Karim Mansour", "owner"),
        UserSpec("staff@sahtein.example", "Huda Fares", "staff"),
    ),
    opens=time(11, 0),
    closes=time(23, 0),
    morning_check=time(7, 30),
    order_time=time(23, 15),
    count_time=time(23, 40),
    # The UAE weekend is Saturday and Sunday; Friday and Saturday nights are busiest.
    weekday_per_mille=(900, 900, 950, 1100, 1250, 1200, 1000),
    late_same_day=(time(13, 0), time(17, 0)),
    late_next_day=(time(8, 30), time(10, 30)),
    mistake_per_mille=30,
    suppliers=(
        SupplierSpec(
            "MEA",
            "Gulf Fresh Meats",
            1,
            (MON, TUE, WED, THU, FRI, SAT),
            time(7, 45),
            time(9, 30),
            late_per_mille=80,
            short_per_mille=50,
            price_step_per_mille=30,
        ),
        SupplierSpec(
            "VEG",
            "Desert Bloom Produce",
            1,
            (MON, WED, FRI, SUN),
            time(8, 0),
            time(10, 0),
            late_per_mille=100,
            short_per_mille=80,
            price_step_per_mille=60,
        ),
        SupplierSpec(
            "BRD",
            "Saj & Pita Bakery",
            1,
            EVERY_DAY,
            time(7, 40),
            time(9, 0),
            late_per_mille=60,
            short_per_mille=40,
            price_step_per_mille=10,
        ),
        # Single buckets and bottles: 40 to 80% of one pack rounds down to
        # nothing, and an order that brings nothing is cancelled (D77).
        SupplierSpec(
            "SAU",
            "Tahini House",
            1,
            (MON, THU),
            time(9, 0),
            time(11, 0),
            late_per_mille=50,
            short_per_mille=250,
            price_step_per_mille=15,
        ),
        SupplierSpec(
            "DRY",
            "Silver Spoon Wholesale",
            2,
            (TUE, SAT),
            time(9, 30),
            time(11, 30),
            late_per_mille=60,
            short_per_mille=40,
            price_step_per_mille=20,
        ),
        SupplierSpec(
            "PKG",
            "BoxIt Packaging",
            3,
            (SUN,),
            time(10, 0),
            time(12, 0),
            late_per_mille=50,
            short_per_mille=30,
            price_step_per_mille=10,
        ),
    ),  # fmt: skip
    items=(
        # Meat: batches with expiry, six deliveries a week.
        _meat(
            "MEA-CHICKEN-THIGH", "Chicken thighs, boneless", "5 kg box", 5000, "85.000", 25000, 3
        ),
        _meat("MEA-CHICKEN-BREAST", "Chicken breast", "5 kg box", 5000, "110.000", 12000, 3),
        _meat("MEA-BEEF-MINCE", "Beef mince", "2 kg pack", 2000, "70.000", 6000, 3),
        _meat(
            "MEA-LAMB",
            "Lamb shoulder, boned",
            "3 kg pack",
            3000,
            "120.000",
            4000,
            4,
            theft_per_mille=20,
            theft_max_per_mille=300,
        ),
        # Produce: batches with expiry, four deliveries a week.
        _produce("VEG-ONION", "Red onions", "10 kg sack", 10000, "25.000", 8000, 21),
        _produce("VEG-TOMATO", "Tomatoes", "6 kg box", 6000, "30.000", 7000, 6),
        _produce(
            "VEG-LETTUCE",
            "Iceberg lettuce",
            "box of 12 heads",
            12,
            "45.000",
            20,
            5,
            base_unit="piece",
        ),
        _produce(
            "VEG-PARSLEY",
            "Flat-leaf parsley",
            "box of 20 bunches",
            20,
            "30.000",
            25,
            3,
            base_unit="piece",
        ),
        _produce("VEG-LEMON", "Lemons", "5 kg bag", 5000, "35.000", 2000, 14),
        _produce("VEG-POTATO", "Potatoes", "20 kg sack", 20000, "40.000", 6000, 21),
        _produce("VEG-GARLIC", "Garlic", "1 kg bag", 1000, "12.000", 800, 30),
        # Bread: batches with expiry, every morning.
        _bread("BRD-PITA", "Pita bread", "bag of 50", 50, "15.000", 300, 2),
        _bread("BRD-SAJ", "Saj bread", "pack of 20", 20, "18.000", 120, 2),
        # Sauces: one bucket or bottle at a time.
        _sauce("SAU-TAHINI", "Tahini", "18 kg bucket", 18000, "230.000", 1500, 180),
        _sauce(
            "SAU-POMEGRANATE",
            "Pomegranate molasses",
            "2 L bottle",
            2000,
            "45.000",
            250,
            365,
            base_unit="ml",
        ),
        # Dry goods: frozen fries by expiry, spices by lot, the rest untracked.
        _dry("DRY-RICE", "Basmati rice", "20 kg sack", 20000, "110.000", 12000),
        _dry("DRY-OIL", "Vegetable oil", "18 L tin", 18000, "120.000", 8000, base_unit="ml"),
        _dry(
            "DRY-FRIES",
            "Frozen fries",
            "carton of 4 × 2.5 kg",
            10000,
            "75.000",
            9000,
            180,
            tracking="expiry",
        ),
        _dry("DRY-SUMAC", "Sumac", "1 kg bag", 1000, "45.000", 100, tracking="lot"),
        _dry("DRY-SEVEN-SPICE", "Seven-spice mix", "1 kg bag", 1000, "40.000", 150, tracking="lot"),
        _dry("DRY-SALT", "Fine salt", "25 kg sack", 25000, "20.000", 600),
        _dry("DRY-FREEKEH", "Freekeh", "5 kg bag", 5000, "55.000", 1500),
        # Packaging: no batch tracking, weekly; damage instead of spoilage.
        _packaging("PKG-BOWL", "Meal container, 750 ml", "case of 500", 500, "250.000", 220),
        _packaging("PKG-SAUCE-CUP", "Sauce cup, 2 oz", "case of 1000", 1000, "90.000", 300),
        _packaging("PKG-BAG", "Paper delivery bag", "case of 250", 250, "110.000", 180),
        _packaging("PKG-CUTLERY", "Cutlery kit", "case of 500", 500, "150.000", 150),
        _packaging("PKG-NAPKIN", "Napkins", "pack of 500", 500, "12.000", 400),
    ),  # fmt: skip
)
