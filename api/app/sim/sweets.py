"""The demo sweets shop as plain data (D86).

Sukkar & Semsem Sweets makes Arabic sweets on site: knafeh from fresh cheese
and kataifi dough, baklava from filo, nuts and ghee, and semolina sweets. It
buys ingredients, not finished sweets, so its stock is bulk and perishable at
once: 50 kg sacks and 16 kg ghee tins next to cheese and ashta that keep a
few days, and nuts that cost the most per kilo and sometimes walk. Thursday
and Friday are the busy days.

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

# Delivered six mornings a week (not Sunday), ordered the evening before: the
# longest gap is Saturday night to Monday morning, two days. Reorder at 2.5
# days, up to 3.5, low below 1. Generous portions of cheese on knafeh.
_cheese = item_kind(
    "cheese and dairy",
    "g",
    "expiry",
    "CHE",
    low_stock_days_per_mille=1000,
    reorder_days_per_mille=2500,
    order_up_to_days_per_mille=3500,
    count_weekday=MON,
    over_order_per_mille=50,
    spoil_per_mille=40,
    over_portion_per_mille=40,
)
# Delivered every morning: reorder at 1.3 days, up to 1.9, low below 0.4.
_dough = item_kind(
    "dough",
    "g",
    "expiry",
    "DOU",
    low_stock_days_per_mille=400,
    reorder_days_per_mille=1300,
    order_up_to_days_per_mille=1900,
    count_weekday=TUE,
    over_order_per_mille=80,
    spoil_per_mille=50,
    over_portion_per_mille=40,
)
# Roasted to order, by lot, twice a week with two days' lead: ordered Sunday
# and Thursday evenings, four days apart at most. Reorder at 6.5 days, up to
# 9.5, low below 2.5. A handful of pistachios on top is never weighed.
_nuts = item_kind(
    "nuts",
    "g",
    "lot",
    "NUT",
    low_stock_days_per_mille=2500,
    reorder_days_per_mille=6500,
    order_up_to_days_per_mille=9500,
    count_weekday=WED,
    spoil_per_mille=5,
    over_portion_per_mille=30,
)
# Bulk dry goods, weekly on Sunday with two days' lead (ordered Friday):
# reorder at 10 days, up to 16, low below 3.5. Ghee and syrup sugar are
# measured by eye.
_grocery = item_kind(
    "dry goods",
    "g",
    "none",
    "GRO",
    low_stock_days_per_mille=3500,
    reorder_days_per_mille=10000,
    order_up_to_days_per_mille=16000,
    count_weekday=THU,
    spoil_per_mille=8,
    over_portion_per_mille=50,
)
# Boxes and trays, weekly on Wednesday with three days' lead (ordered
# Sunday): reorder at 11.5 days, up to 18, low below 4.
_packaging = item_kind(
    "packaging",
    "piece",
    "none",
    "PKG",
    low_stock_days_per_mille=4000,
    reorder_days_per_mille=11500,
    order_up_to_days_per_mille=18000,
    count_weekday=SUN,
    spoil_per_mille=10,
    spoil_reason="damaged",
    over_portion_per_mille=20,
)

# A sweets shop selling about 80 kg of sweets a day, more on Thursday and Friday.
SWEETS = Venue(
    name="Sukkar & Semsem Sweets",
    timezone="Asia/Amman",
    currency="JOD",
    location_name="Jabal Amman shop",
    location_kind="branch",
    users=(
        UserSpec("owner@sukkarsemsem.example", "Rami Khoury", "owner"),
        UserSpec("staff@sukkarsemsem.example", "Dana Nasser", "staff"),
    ),
    opens=time(9, 0),
    closes=time(23, 0),
    morning_check=time(7, 0),
    order_time=time(23, 10),
    count_time=time(23, 30),
    # Families buy sweets on Thursday evening and after Friday prayers.
    weekday_per_mille=(900, 900, 950, 1250, 1400, 1100, 900),
    late_same_day=(time(13, 0), time(17, 0)),
    late_next_day=(time(8, 0), time(10, 0)),
    mistake_per_mille=20,
    suppliers=(
        SupplierSpec(
            "CHE",
            "Nabulsi Valley Cheese",
            1,
            (MON, TUE, WED, THU, FRI, SAT),
            time(7, 15),
            time(8, 30),
            late_per_mille=60,
            short_per_mille=70,
            price_step_per_mille=25,
        ),
        SupplierSpec(
            "DOU",
            "Golden Thread Kataifi",
            1,
            EVERY_DAY,
            time(7, 10),
            time(8, 0),
            late_per_mille=50,
            short_per_mille=60,
            price_step_per_mille=10,
        ),
        SupplierSpec(
            "NUT",
            "Levant Roastery Nuts",
            2,
            (TUE, SAT),
            time(9, 0),
            time(11, 0),
            late_per_mille=60,
            short_per_mille=40,
            price_step_per_mille=40,
        ),
        SupplierSpec(
            "GRO",
            "Citadel Wholesale",
            2,
            (SUN,),
            time(9, 30),
            time(11, 30),
            late_per_mille=50,
            short_per_mille=30,
            price_step_per_mille=15,
        ),
        SupplierSpec(
            "PKG",
            "Ribbon & Box Co",
            3,
            (WED,),
            time(10, 0),
            time(12, 0),
            late_per_mille=40,
            short_per_mille=30,
            price_step_per_mille=10,
        ),
    ),  # fmt: skip
    items=(
        # Cheese and dairy: batches with expiry, six deliveries a week.
        _cheese("CHE-NABULSI", "Nabulsi cheese, desalted", "2 kg block", 2000, "9.000", 9000, 5),
        _cheese("CHE-AKKAWI", "Akkawi cheese", "2 kg block", 2000, "8.500", 6000, 5),
        # Ashta keeps three days, so it is ordered closer to what is used.
        _cheese(
            "CHE-ASHTA",
            "Ashta cream",
            "1 kg tub",
            1000,
            "4.500",
            4000,
            3,
            low_stock_days_per_mille=600,
            reorder_days_per_mille=1800,
            order_up_to_days_per_mille=2400,
        ),
        _cheese("CHE-MILK", "Full-fat milk", "1 L carton", 1000, "0.900", 6000, 7, base_unit="ml"),
        _cheese("CHE-BUTTER", "Unsalted butter", "5 kg block", 5000, "22.000", 3000, 60),
        # Dough: batches with expiry, delivered daily.
        _dough("DOU-KATAIFI", "Kataifi dough", "500 g pack", 500, "1.200", 8000, 4),
        _dough("DOU-FILO", "Filo sheets", "1 kg pack", 1000, "2.000", 3000, 10),
        _dough("DOU-PUFF", "Puff pastry", "1 kg pack", 1000, "2.500", 1500, 7),
        # Nuts: batches by roast lot, no expiry. Pistachios and pine nuts walk.
        _nuts(
            "NUT-PISTACHIO",
            "Pistachios, Halabi",
            "1 kg bag",
            1000,
            "16.000",
            2500,
            theft_per_mille=60,
            theft_max_per_mille=150,
        ),
        _nuts("NUT-CASHEW", "Cashews", "1 kg bag", 1000, "11.000", 1200),
        _nuts("NUT-WALNUT", "Walnuts", "1 kg bag", 1000, "9.000", 1500),
        _nuts("NUT-ALMOND", "Almonds, blanched", "1 kg bag", 1000, "8.000", 800),
        _nuts(
            "NUT-PINE",
            "Pine nuts",
            "500 g bag",
            500,
            "18.000",
            150,
            theft_per_mille=40,
            theft_max_per_mille=300,
        ),
        # Dry goods: no batch tracking, weekly.
        _grocery("GRO-SEMOLINA", "Fine semolina", "25 kg sack", 25000, "14.000", 9000),
        _grocery("GRO-SUGAR", "White sugar", "50 kg sack", 50000, "32.000", 15000),
        _grocery("GRO-GHEE", "Ghee (samneh)", "16 kg tin", 16000, "75.000", 6000),
        _grocery("GRO-FLOUR", "All-purpose flour", "50 kg sack", 50000, "18.000", 4000),
        _grocery("GRO-ROSE", "Rose water", "1 L bottle", 1000, "2.500", 300, base_unit="ml"),
        _grocery(
            "GRO-BLOSSOM", "Orange blossom water", "1 L bottle", 1000, "2.500", 400, base_unit="ml"
        ),
        _grocery("GRO-COCONUT", "Desiccated coconut", "1 kg bag", 1000, "3.500", 500),
        _grocery("GRO-STARCH", "Corn starch", "1 kg box", 1000, "1.200", 800),
        # Packaging: no batch tracking, weekly; damage instead of spoilage.
        _packaging("PKG-BOX-HALF", "Sweets box, 0.5 kg", "case of 200", 200, "24.000", 120),
        _packaging("PKG-BOX-KILO", "Sweets box, 1 kg", "case of 100", 100, "18.000", 70),
        _packaging("PKG-TRAY", "Round foil tray, 35 cm", "case of 50", 50, "15.000", 25),
        _packaging("PKG-RIBBON", "Gift ribbon roll", "box of 10", 10, "5.000", 2),
        _packaging("PKG-BAG", "Carrier bag", "case of 500", 500, "10.000", 150),
    ),  # fmt: skip
)
