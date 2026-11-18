"""Item setup: worked examples of D90 to D94 through the domain functions.

The cat fixture's item is whole milk (ml, batches with expiry), sold by its
supplier by the 1000 ml bottle, its preferred supplier price. Each test that
touches the rules ends with in_line: the alerts and draft lines match a fresh
evaluation. Stock comes from test_alerts.py's batch_with, which writes
balances without movements, as the alerts examples do.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.alerts import check_alerts
from app.domain.draft_lines import check_draft_lines
from app.domain.errors import DomainError
from app.domain.items import (
    UnitInput,
    add_unit,
    create_item,
    remove_par_level,
    remove_supplier_price,
    rename_unit,
    set_par_level,
    set_supplier_price,
    update_item,
)
from app.models import Item, ItemUnit, ParLevel, SupplierItem
from tests.builders import (
    Catalogue,
    add,
    new_item,
    new_par_level,
    new_supplier,
    new_supplier_item,
    new_unit,
)
from tests.test_alerts import batch_with, d, nov, open_alerts, recorded_statements, sweep
from tests.test_draft_lines import lines, suggested
from tests.test_stock import do_use

AT = nov(10, 10)


def in_line(db: Session, cat: Catalogue) -> None:
    assert check_alerts(db, cat.business.id) == []
    assert check_draft_lines(db, cat.business.id) == []


def par(db: Session, cat: Catalogue, low: str, reorder: str, up_to: str = "6000") -> ParLevel:
    return set_par_level(
        db,
        cat.business.id,
        cat.item.id,
        cat.location.id,
        low_stock_level=d(low),
        reorder_point=d(reorder),
        order_up_to=d(up_to),
        at=AT,
    )


# --- Creating items -------------------------------------------------------------


def test_create_item_with_its_units(db_session: Session, cat: Catalogue):
    item = create_item(
        db_session,
        cat.business.id,
        sku=" DRY-FLOUR ",
        name="Flour",
        base_unit="g",
        category="dry goods",
        units=[UnitInput("25 kg sack", d("25000")), UnitInput("1 kg bag", d("1000"))],
    )

    assert (item.sku, item.name, item.base_unit, item.is_active) == (
        "DRY-FLOUR",
        "Flour",
        "g",
        True,
    )
    assert (item.tracks_batches, item.tracks_expiry, item.shelf_life_days) == (False, False, None)
    units = db_session.scalars(
        select(ItemUnit).where(ItemUnit.item_id == item.id).order_by(ItemUnit.factor_to_base)
    ).all()
    assert [(u.name, u.factor_to_base) for u in units] == [
        ("1 kg bag", d("1000")),
        ("25 kg sack", d("25000")),
    ]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sku": "  "}, "sku must not be empty"),
        ({"name": ""}, "name must not be empty"),
        ({"base_unit": "kg"}, "base_unit must be one of g, ml, piece"),
        ({"tracks_expiry": True}, "must track batches"),
        ({"shelf_life_days": 0}, "shelf_life_days must be positive"),
        ({"units": [UnitInput("bag", d("0"))]}, "factor_to_base must be positive"),
        ({"units": [UnitInput("bag", d("1")), UnitInput("bag", d("2"))]}, "same name"),
    ],
)
def test_create_item_refuses_bad_input(db_session: Session, cat: Catalogue, changes, message):
    values = {"sku": "NEW-1", "name": "New", "base_unit": "g"} | changes
    with pytest.raises(DomainError, match=message):
        create_item(db_session, cat.business.id, **values)


def test_a_sku_is_unique_per_business_only(db_session: Session, cat: Catalogue, other: Catalogue):
    with pytest.raises(DomainError, match=f"SKU {cat.item.sku} is already used"):
        create_item(db_session, cat.business.id, sku=cat.item.sku, name="Milk 2", base_unit="ml")

    # The same SKU in another business is fine.
    create_item(db_session, other.business.id, sku=cat.item.sku, name="Milk", base_unit="ml")


# --- Updating items -------------------------------------------------------------


def test_names_skus_and_categories_change_freely(db_session: Session, cat: Catalogue):
    item = update_item(
        db_session, cat.business.id, cat.item.id, at=AT, sku="DAI-MILK", name="Milk", category=None
    )

    assert (item.sku, item.name, item.category) == ("DAI-MILK", "Milk", None)
    taken = add(db_session, new_item(cat.business.id, sku="TAKEN"))
    with pytest.raises(DomainError, match="SKU TAKEN is already used"):
        update_item(db_session, cat.business.id, cat.item.id, at=AT, sku=taken.sku)


def test_base_unit_and_tracking_change_only_before_the_first_movement(
    db_session: Session, cat: Catalogue
):
    item = update_item(
        db_session,
        cat.business.id,
        cat.item.id,
        at=AT,
        tracks_expiry=False,
        tracks_batches=False,
        base_unit="g",
    )
    assert (item.base_unit, item.tracks_batches, item.tracks_expiry) == ("g", False, False)
    with pytest.raises(DomainError, match="must track batches"):
        update_item(db_session, cat.business.id, cat.item.id, at=AT, tracks_expiry=True)

    do_use(db_session, cat, cat.item, "100", occurred_at=nov(10, 8))

    for change in ({"base_unit": "ml"}, {"tracks_batches": True}):
        with pytest.raises(DomainError, match="has movements"):
            update_item(db_session, cat.business.id, cat.item.id, at=AT, **change)
    # Sending the values it already has is no change.
    update_item(db_session, cat.business.id, cat.item.id, at=AT, base_unit="g")


def test_a_shorter_shelf_life_narrows_the_expiry_window_at_once(
    db_session: Session, cat: Catalogue
):
    batch = batch_with(db_session, cat, "1000", expires_day=13)
    sweep(db_session, cat, nov(10))
    # expiry_warn_days is 3 and milk has no shelf life: 3 days left is expiring.
    assert open_alerts(db_session, cat) == {("expiring", batch.id)}

    # Half of a 4-day shelf life is 2 days, so 3 days left no longer is (D63).
    update_item(db_session, cat.business.id, cat.item.id, at=AT, shelf_life_days=4)

    assert open_alerts(db_session, cat) == set()
    in_line(db_session, cat)


def test_deactivating_resolves_low_stock_and_drops_the_draft_line_but_not_the_rest(
    db_session: Session, cat: Catalogue
):
    expired = batch_with(db_session, cat, "500", expires_day=9)
    sweep(db_session, cat, nov(10))
    par(db_session, cat, "1000", "2000")
    assert open_alerts(db_session, cat) == {("expired", expired.id), ("low_stock", None)}
    assert suggested(db_session, cat) == [6]

    update_item(db_session, cat.business.id, cat.item.id, at=AT, is_active=False)

    # The expired stock is still there, so its alert stays (D93).
    assert open_alerts(db_session, cat) == {("expired", expired.id)}
    assert suggested(db_session, cat) == []
    in_line(db_session, cat)

    update_item(db_session, cat.business.id, cat.item.id, at=nov(10, 11), is_active=True)
    assert open_alerts(db_session, cat) == {("expired", expired.id), ("low_stock", None)}
    assert suggested(db_session, cat) == [6]
    in_line(db_session, cat)


# --- Units ----------------------------------------------------------------------


def test_units_are_added_and_renamed_never_refactored(db_session: Session, cat: Catalogue):
    crate = add_unit(
        db_session, cat.business.id, cat.item.id, name="crate", factor_to_base=d("12000")
    )
    renamed = rename_unit(db_session, cat.business.id, cat.item.id, crate.id, name="crate of 12")

    assert (renamed.name, renamed.factor_to_base) == ("crate of 12", d("12000"))
    with pytest.raises(DomainError, match=f"already has a unit named {cat.unit.name}"):
        add_unit(
            db_session, cat.business.id, cat.item.id, name=cat.unit.name, factor_to_base=d("1")
        )
    with pytest.raises(DomainError, match=f"already has a unit named {cat.unit.name}"):
        rename_unit(db_session, cat.business.id, cat.item.id, crate.id, name=cat.unit.name)
    with pytest.raises(DomainError, match="factor_to_base must be positive"):
        add_unit(db_session, cat.business.id, cat.item.id, name="none", factor_to_base=d("-1"))


# --- Supplier prices ------------------------------------------------------------


def test_a_new_preferred_supplier_takes_the_draft_line(db_session: Session, cat: Catalogue):
    par(db_session, cat, "1000", "2000")
    assert lines(db_session, cat) == [(cat.supplier.id, cat.item.id, 6, None)]
    second = add(db_session, new_supplier(cat.business.id, name="Second dairy"))
    crate = add(db_session, new_unit(cat.item, name="crate", factor_to_base=Decimal("12000")))

    price = set_supplier_price(
        db_session,
        cat.business.id,
        cat.item.id,
        supplier_id=second.id,
        item_unit_id=crate.id,
        pack_price=d("9.600"),
        is_preferred=True,
        at=AT,
    )

    # The old preferred row is unset, and the line moves to the new supplier,
    # in its pack: 6000 ml is one crate of 12 L, rounded up (D94).
    db_session.refresh(cat.supplier_item)
    assert (price.is_preferred, cat.supplier_item.is_preferred) == (True, False)
    assert lines(db_session, cat) == [(second.id, cat.item.id, 1, None)]
    in_line(db_session, cat)

    # A price change that keeps the preference changes no line.
    set_supplier_price(
        db_session,
        cat.business.id,
        cat.item.id,
        supplier_id=second.id,
        item_unit_id=crate.id,
        pack_price=d("9.900"),
        is_preferred=True,
        at=AT,
    )
    db_session.refresh(price)
    assert price.pack_price == d("9.900")
    assert lines(db_session, cat) == [(second.id, cat.item.id, 1, None)]

    # Without a preferred supplier, nobody is ordered from.
    remove_supplier_price(db_session, cat.business.id, cat.item.id, price.id, at=AT)
    assert lines(db_session, cat) == []
    in_line(db_session, cat)


def test_a_supplier_price_names_the_business_s_supplier_and_one_of_the_item_s_units(
    db_session: Session, cat: Catalogue, other: Catalogue
):
    eggs = add(db_session, new_item(cat.business.id, name="Eggs", base_unit="piece"))
    tray = add(db_session, new_unit(eggs, name="tray", factor_to_base=Decimal("30")))
    base = {"pack_price": d("1"), "is_preferred": False, "at": AT}

    with pytest.raises(DomainError, match="supplier .* not found"):
        set_supplier_price(
            db_session,
            cat.business.id,
            cat.item.id,
            supplier_id=other.supplier.id,
            item_unit_id=cat.unit.id,
            **base,
        )
    with pytest.raises(DomainError, match="unit .* not found for this item"):
        set_supplier_price(
            db_session,
            cat.business.id,
            cat.item.id,
            supplier_id=cat.supplier.id,
            item_unit_id=tray.id,
            **base,
        )
    with pytest.raises(DomainError, match="pack_price must not be negative"):
        set_supplier_price(
            db_session,
            cat.business.id,
            cat.item.id,
            supplier_id=cat.supplier.id,
            item_unit_id=cat.unit.id,
            **(base | {"pack_price": d("-1")}),
        )


def test_removing_a_supplier_price_that_is_not_preferred_leaves_the_draft(
    db_session: Session, cat: Catalogue
):
    par(db_session, cat, "1000", "2000")
    second = add(db_session, new_supplier(cat.business.id, name="Second dairy"))
    spare = add(db_session, new_supplier_item(second, cat.item, cat.unit, is_preferred=False))

    remove_supplier_price(db_session, cat.business.id, cat.item.id, spare.id, at=AT)

    assert db_session.get(SupplierItem, spare.id) is None
    assert lines(db_session, cat) == [(cat.supplier.id, cat.item.id, 6, None)]
    with pytest.raises(DomainError, match="not found"):
        remove_supplier_price(db_session, cat.business.id, cat.item.id, spare.id, at=AT)


# --- Par levels -----------------------------------------------------------------


def test_setting_and_removing_a_par_level_re_evaluates_the_item_there(
    db_session: Session, cat: Catalogue
):
    batch_with(db_session, cat, "1500", expires_day=20)

    # 1500 ml: at or below the reorder point (a line) but above the low level.
    level = par(db_session, cat, "1000", "2000")
    assert (level.low_stock_level, level.reorder_point, level.order_up_to) == (
        d("1000"),
        d("2000"),
        d("6000"),
    )
    assert open_alerts(db_session, cat) == set()
    assert suggested(db_session, cat) == [5]
    in_line(db_session, cat)

    # Raising the low level to 1500 makes the item short, on the same row.
    again = par(db_session, cat, "1500", "2000")
    assert again.id == level.id
    assert open_alerts(db_session, cat) == {("low_stock", None)}
    in_line(db_session, cat)

    remove_par_level(db_session, cat.business.id, cat.item.id, cat.location.id, at=AT)
    assert open_alerts(db_session, cat) == set()
    assert suggested(db_session, cat) == []
    in_line(db_session, cat)
    with pytest.raises(DomainError, match="has no par level"):
        remove_par_level(db_session, cat.business.id, cat.item.id, cat.location.id, at=AT)


@pytest.mark.parametrize(
    ("levels", "message"),
    [
        (("2001", "2000", "6000"), "must be in order"),
        (("0", "6001", "6000"), "must be in order"),
        (("-1", "2000", "6000"), "low_stock_level must not be negative"),
        (("0", "0", "0"), "order_up_to must be positive"),
    ],
)
def test_par_levels_must_be_in_order(db_session: Session, cat: Catalogue, levels, message):
    with pytest.raises(DomainError, match=message):
        par(db_session, cat, *levels)


def test_a_par_level_is_for_one_of_the_business_s_locations(
    db_session: Session, cat: Catalogue, other: Catalogue
):
    with pytest.raises(DomainError, match="location .* not found"):
        set_par_level(
            db_session,
            cat.business.id,
            cat.item.id,
            other.location.id,
            low_stock_level=d("1"),
            reorder_point=d("2"),
            order_up_to=d("3"),
            at=AT,
        )


# --- Tenancy and locking --------------------------------------------------------


def test_another_business_s_item_is_not_found(
    db_session: Session, cat: Catalogue, other: Catalogue
):
    with pytest.raises(DomainError, match="item .* not found"):
        update_item(db_session, cat.business.id, other.item.id, at=AT, name="Mine now")
    with pytest.raises(DomainError, match="item .* not found"):
        add_unit(db_session, cat.business.id, other.item.id, name="x", factor_to_base=d("1"))


def test_every_setup_write_locks_the_business_row_first(db_session: Session, cat: Catalogue):
    """D92: the same lock as an order action, before anything is read."""
    add(db_session, new_par_level(cat.location, cat.item))
    writes = [
        lambda: create_item(db_session, cat.business.id, sku="LOCK-1", name="x", base_unit="g"),
        lambda: update_item(db_session, cat.business.id, cat.item.id, at=AT, name="x"),
        lambda: add_unit(
            db_session, cat.business.id, cat.item.id, name="lock", factor_to_base=d("1")
        ),
        lambda: par(db_session, cat, "1", "2", "3"),
        lambda: remove_par_level(db_session, cat.business.id, cat.item.id, cat.location.id, at=AT),
    ]
    for write in writes:
        with recorded_statements(db_session) as statements:
            write()
        assert "FOR NO KEY UPDATE" in statements[0]


def test_an_inactive_item_s_setup_can_still_change(db_session: Session, cat: Catalogue):
    update_item(db_session, cat.business.id, cat.item.id, at=AT, is_active=False)

    par(db_session, cat, "1000", "2000")

    assert db_session.get(Item, cat.item.id).is_active is False
    assert open_alerts(db_session, cat) == set()
    in_line(db_session, cat)
