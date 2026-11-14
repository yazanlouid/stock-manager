"""Alerts: worked examples of what is due, the sweep and the check (D59 to D64).

The first half builds batches and balances with tests/builders.py and calls
evaluate_alerts and sweep_alerts directly, one rule at a time. A balance is a
projection, so a test may set it. The second half goes through the stock
writes. The cat fixture's item is whole milk, which tracks batches and expiry
and has no shelf life, so its expiry window is the business's 3 days; the
business is in Asia/Amman (UTC+3).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.domain.alerts import (
    AlertChange,
    AlertDifference,
    AlertKey,
    check_alerts,
    evaluate_alerts,
    sweep_alerts,
    sweep_alerts_if_due,
)
from app.domain.errors import DomainError
from app.models import Alert, Batch, Business, Item, StockBalance
from tests.builders import (
    Catalogue,
    add,
    new_alert,
    new_balance,
    new_batch,
    new_default_batch,
    new_item,
    new_location,
    new_par_level,
)


def d(value: str) -> Decimal:
    return Decimal(value)


def nov(day: int, hour: int = 9) -> datetime:
    """A time in November 2026, in UTC: 09:00 UTC is noon in Amman."""
    return datetime(2026, 11, day, hour, tzinfo=UTC)


def day(n: int) -> date:
    return date(2026, 11, n)


# --- Helpers --------------------------------------------------------------------


def batch_with(
    db: Session, cat: Catalogue, qty: str, expires_day: int | None, item: Item | None = None
) -> Batch:
    """A received batch of the item (cat's milk by default) holding qty."""
    expires_on = day(expires_day) if expires_day else None
    batch = add(db, new_batch(cat.location, item or cat.item, expires_on=expires_on))
    add(db, new_balance(batch, qty_on_hand=d(qty)))
    return batch


def default_with(db: Session, cat: Catalogue, qty: str, item: Item | None = None) -> Batch:
    """The item's default batch holding qty, which may be negative."""
    batch = add(db, new_default_batch(cat.location, item or cat.item))
    add(db, new_balance(batch, qty_on_hand=d(qty)))
    return batch


def set_on_hand(db: Session, batch: Batch, qty: str) -> None:
    db.execute(
        update(StockBalance).where(StockBalance.batch_id == batch.id).values(qty_on_hand=d(qty))
    )


def evaluate(
    db: Session, cat: Catalogue, at: datetime, item: Item | None = None
) -> list[AlertChange]:
    pair = (cat.location.id, (item or cat.item).id)
    return evaluate_alerts(db, cat.business.id, [pair], at)


def sweep(db: Session, cat: Catalogue, as_of: datetime) -> list[AlertChange]:
    return sweep_alerts(db, cat.business.id, as_of)


def alerts(db: Session, cat: Catalogue, status: str = "open") -> list[Alert]:
    return list(
        db.scalars(
            select(Alert)
            .where(Alert.business_id == cat.business.id, Alert.status == status)
            .order_by(Alert.raised_at, Alert.id)
            .execution_options(populate_existing=True)
        )
    )


def open_alerts(db: Session, cat: Catalogue) -> set[tuple[str, UUID | None]]:
    """The open alerts as (type, batch_id) pairs."""
    return {(alert.type, alert.batch_id) for alert in alerts(db, cat)}


def swept_on(db: Session, cat: Catalogue) -> date | None:
    return db.scalar(select(Business.alerts_swept_on).where(Business.id == cat.business.id))


# --- Negative stock -------------------------------------------------------------


def test_a_negative_default_batch_raises_negative_stock_until_it_is_settled(
    db_session: Session, cat: Catalogue
):
    default = default_with(db_session, cat, "-250")

    changes = evaluate(db_session, cat, nov(10))

    key = AlertKey("negative_stock", cat.location.id, cat.item.id, default.id)
    assert changes == [AlertChange("raised", key)]
    (alert,) = alerts(db_session, cat)
    assert (alert.type, alert.batch_id, alert.raised_at) == ("negative_stock", default.id, nov(10))

    # Evaluating again changes nothing.
    assert evaluate(db_session, cat, nov(10, 10)) == []

    # Settled to zero: no longer negative, so the alert resolves.
    set_on_hand(db_session, default, "0")
    assert evaluate(db_session, cat, nov(11)) == [AlertChange("resolved", key)]
    assert open_alerts(db_session, cat) == set()
    (resolved,) = alerts(db_session, cat, "resolved")
    assert (resolved.id, resolved.resolved_at) == (alert.id, nov(11))


def test_a_default_batch_at_zero_or_above_raises_nothing(db_session: Session, cat: Catalogue):
    default_with(db_session, cat, "0")
    assert evaluate(db_session, cat, nov(10)) == []


# --- Expired and expiring -------------------------------------------------------


def test_nothing_counts_as_expired_or_expiring_before_the_first_sweep(
    db_session: Session, cat: Catalogue
):
    batch_with(db_session, cat, "1000", expires_day=1)

    assert evaluate(db_session, cat, nov(10)) == []
    assert swept_on(db_session, cat) is None


def test_a_batch_is_expired_from_the_day_after_its_expiry_date(db_session: Session, cat: Catalogue):
    batch = batch_with(db_session, cat, "1000", expires_day=10)

    # On its expiry date it is still usable (domain rule 3), so only expiring.
    sweep(db_session, cat, nov(10))
    assert open_alerts(db_session, cat) == {("expiring", batch.id)}

    # The next day the expiring alert resolves and the expired one is raised.
    changes = sweep(db_session, cat, nov(11))
    assert {(c.change, c.key.type) for c in changes} == {
        ("resolved", "expiring"),
        ("raised", "expired"),
    }
    assert open_alerts(db_session, cat) == {("expired", batch.id)}
    (expiring,) = alerts(db_session, cat, "resolved")
    assert (expiring.raised_at, expiring.resolved_at) == (nov(10), nov(11))


def test_an_expired_alert_resolves_when_the_batch_is_emptied(db_session: Session, cat: Catalogue):
    batch = batch_with(db_session, cat, "1000", expires_day=5)
    sweep(db_session, cat, nov(10))
    assert open_alerts(db_session, cat) == {("expired", batch.id)}

    set_on_hand(db_session, batch, "0")
    evaluate(db_session, cat, nov(10, 12))

    assert open_alerts(db_session, cat) == set()


@pytest.mark.parametrize(
    ("days_left", "expiring"),
    [(4, False), (3, True), (1, True), (0, True)],
)
def test_without_a_shelf_life_the_window_is_the_business_expiry_warn_days(
    db_session: Session, cat: Catalogue, days_left: int, expiring: bool
):
    batch = batch_with(db_session, cat, "1000", expires_day=10 + days_left)

    sweep(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == ({("expiring", batch.id)} if expiring else set())


@pytest.mark.parametrize(
    ("shelf_life_days", "days_left", "expiring"),
    [
        # 1 day (ka'ak): a window of 0, so only on the expiry date itself.
        (1, 1, False),
        (1, 0, True),
        # 2 days (croissants): from the day before, not on arrival.
        (2, 2, False),
        (2, 1, True),
        # 5 days: half, rounded down, is 2.
        (5, 3, False),
        (5, 2, True),
        # 90 days (oat milk): half would be 45, so the business's 3 days cap it.
        (90, 4, False),
        (90, 3, True),
    ],
)
def test_the_window_is_at_most_half_the_shelf_life(
    db_session: Session, cat: Catalogue, shelf_life_days: int, days_left: int, expiring: bool
):
    item = add(
        db_session,
        new_item(
            cat.business.id,
            tracks_batches=True,
            tracks_expiry=True,
            shelf_life_days=shelf_life_days,
        ),
    )
    batch = batch_with(db_session, cat, "12", expires_day=10 + days_left, item=item)

    sweep(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == ({("expiring", batch.id)} if expiring else set())


def test_expiry_warn_days_can_be_set_per_business(db_session: Session, cat: Catalogue):
    cat.business.expiry_warn_days = 5
    batch = batch_with(db_session, cat, "1000", expires_day=15)

    sweep(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == {("expiring", batch.id)}


def test_an_empty_batch_raises_nothing_even_when_expired(db_session: Session, cat: Catalogue):
    batch_with(db_session, cat, "0", expires_day=5)
    batch_with(db_session, cat, "0", expires_day=11)

    assert sweep(db_session, cat, nov(10)) == []


def test_a_batch_without_an_expiry_date_never_expires(db_session: Session, cat: Catalogue):
    batch_with(db_session, cat, "30", expires_day=None)

    assert sweep(db_session, cat, nov(10)) == []


# --- Low stock ------------------------------------------------------------------


@pytest.mark.parametrize(("on_hand", "low"), [("2000.001", False), ("2000", True), ("0", True)])
def test_low_stock_is_due_at_or_below_the_reorder_point(
    db_session: Session, cat: Catalogue, on_hand: str, low: bool
):
    add(db_session, new_par_level(cat.location, cat.item, reorder_point=d("2000")))
    batch_with(db_session, cat, on_hand, expires_day=20)

    evaluate(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == ({("low_stock", None)} if low else set())


def test_an_item_with_a_par_level_and_no_stock_at_all_is_low(db_session: Session, cat: Catalogue):
    add(db_session, new_par_level(cat.location, cat.item, reorder_point=d("0")))

    evaluate(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == {("low_stock", None)}


def test_low_stock_resolves_when_stock_rises_above_the_reorder_point(
    db_session: Session, cat: Catalogue
):
    add(db_session, new_par_level(cat.location, cat.item, reorder_point=d("2000")))
    batch = batch_with(db_session, cat, "500", expires_day=20)
    evaluate(db_session, cat, nov(10))

    set_on_hand(db_session, batch, "5500")
    key = AlertKey("low_stock", cat.location.id, cat.item.id, None)
    assert evaluate(db_session, cat, nov(11)) == [AlertChange("resolved", key)]


def test_without_a_par_level_an_item_is_never_low(db_session: Session, cat: Catalogue):
    batch_with(db_session, cat, "1", expires_day=20)

    assert evaluate(db_session, cat, nov(10)) == []


def test_an_inactive_item_is_never_low_but_its_stock_still_alerts(
    db_session: Session, cat: Catalogue
):
    cat.item.is_active = False
    add(db_session, new_par_level(cat.location, cat.item))
    batch = batch_with(db_session, cat, "100", expires_day=5)
    default = default_with(db_session, cat, "-40")

    sweep(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == {("expired", batch.id), ("negative_stock", default.id)}


def test_usable_on_hand_sums_every_batch_and_a_negative_default_counts_against_it(
    db_session: Session, cat: Catalogue
):
    add(db_session, new_par_level(cat.location, cat.item, reorder_point=d("2000")))
    batch_with(db_session, cat, "1500", expires_day=20)
    batch_with(db_session, cat, "1500", expires_day=22)
    default = default_with(db_session, cat, "0")

    # 3000 usable: above the reorder point.
    assert evaluate(db_session, cat, nov(10)) == []

    # A shortfall of 1000 leaves 2000: at the reorder point.
    set_on_hand(db_session, default, "-1000")
    evaluate(db_session, cat, nov(10, 10))
    assert open_alerts(db_session, cat) == {("low_stock", None), ("negative_stock", default.id)}


def test_expired_stock_is_not_usable_so_a_sweep_can_make_an_item_low(
    db_session: Session, cat: Catalogue
):
    add(db_session, new_par_level(cat.location, cat.item, reorder_point=d("2000")))
    old = batch_with(db_session, cat, "3000", expires_day=10)
    batch_with(db_session, cat, "1000", expires_day=25)

    # Before any sweep nothing has expired, so all 4000 count.
    assert evaluate(db_session, cat, nov(10)) == []

    # Still usable on its expiry date.
    sweep(db_session, cat, nov(10))
    assert open_alerts(db_session, cat) == {("expiring", old.id)}

    # The day after, only the 1000 that hasn't expired counts.
    sweep(db_session, cat, nov(11))
    assert open_alerts(db_session, cat) == {("expired", old.id), ("low_stock", None)}


def test_low_stock_is_per_location(db_session: Session, cat: Catalogue):
    second = add(db_session, new_location(cat.business.id, name="Second branch"))
    add(db_session, new_par_level(cat.location, cat.item))
    add(db_session, new_par_level(second, cat.item))
    batch_with(db_session, cat, "9000", expires_day=20)

    evaluate_alerts(
        db_session,
        cat.business.id,
        [(cat.location.id, cat.item.id), (second.id, cat.item.id)],
        nov(10),
    )

    # Plenty at the first location, nothing at the second.
    (alert,) = alerts(db_session, cat)
    assert (alert.type, alert.location_id) == ("low_stock", second.id)


# --- Lifecycle ------------------------------------------------------------------


def test_a_recurrence_raises_a_new_alert_and_keeps_the_resolved_one(
    db_session: Session, cat: Catalogue
):
    default = default_with(db_session, cat, "-10")
    evaluate(db_session, cat, nov(10))
    set_on_hand(db_session, default, "0")
    evaluate(db_session, cat, nov(11))
    set_on_hand(db_session, default, "-5")
    evaluate(db_session, cat, nov(12))

    (first,) = alerts(db_session, cat, "resolved")
    (second,) = alerts(db_session, cat)
    assert first.id != second.id
    assert (first.raised_at, first.resolved_at) == (nov(10), nov(11))
    assert (second.raised_at, second.resolved_at) == (nov(12), None)


def test_a_write_dated_before_an_alert_was_raised_resolves_it_at_raised_at(
    db_session: Session, cat: Catalogue
):
    default = default_with(db_session, cat, "-10")
    evaluate(db_session, cat, nov(10))

    # A backdated request settles the shortfall.
    set_on_hand(db_session, default, "0")
    evaluate(db_session, cat, nov(8))

    (alert,) = alerts(db_session, cat, "resolved")
    assert (alert.raised_at, alert.resolved_at) == (nov(10), nov(10))


def test_evaluate_only_looks_at_the_pairs_it_is_given(db_session: Session, cat: Catalogue):
    flour = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    default_with(db_session, cat, "-10")
    default_with(db_session, cat, "-10", item=flour)

    evaluate(db_session, cat, nov(10), item=flour)

    assert [alert.item_id for alert in alerts(db_session, cat)] == [flour.id]
    assert evaluate_alerts(db_session, cat.business.id, [], nov(10)) == []


def test_evaluate_never_touches_another_business(
    db_session: Session, cat: Catalogue, other: Catalogue
):
    default_with(db_session, other, "-10")

    # cat's business, but the other business's location and item.
    changes = evaluate_alerts(
        db_session, cat.business.id, [(other.location.id, other.item.id)], nov(10)
    )

    assert changes == []
    assert alerts(db_session, other) == []


# --- The sweep ------------------------------------------------------------------


def test_the_sweep_sets_the_alerts_date_to_as_of_in_the_business_timezone(
    db_session: Session, cat: Catalogue
):
    # 22:00 UTC on 10 Nov is 01:00 on 11 Nov in Amman.
    sweep(db_session, cat, datetime(2026, 11, 10, 22, tzinfo=UTC))

    assert swept_on(db_session, cat) == day(11)


def test_the_sweep_evaluates_every_item_with_a_batch_or_a_par_level(
    db_session: Session, cat: Catalogue
):
    flour = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    sugar = add(db_session, new_item(cat.business.id, name="Sugar", base_unit="g"))
    milk = batch_with(db_session, cat, "1000", expires_day=5)
    flour_default = default_with(db_session, cat, "-10", item=flour)
    add(db_session, new_par_level(cat.location, sugar))

    changes = sweep(db_session, cat, nov(10))

    assert {(c.change, c.key.type, c.key.item_id, c.key.batch_id) for c in changes} == {
        ("raised", "expired", cat.item.id, milk.id),
        ("raised", "negative_stock", flour.id, flour_default.id),
        ("raised", "low_stock", sugar.id, None),
    }
    assert all(alert.raised_at == nov(10) for alert in alerts(db_session, cat))


def test_a_sweep_on_the_same_date_changes_nothing_and_an_earlier_one_is_refused(
    db_session: Session, cat: Catalogue
):
    batch_with(db_session, cat, "1000", expires_day=5)
    sweep(db_session, cat, nov(10))

    assert sweep(db_session, cat, nov(10, 15)) == []

    with pytest.raises(DomainError, match="already swept as of 2026-11-10"):
        sweep(db_session, cat, nov(9))
    assert swept_on(db_session, cat) == day(10)


def test_the_sweep_repairs_alerts_that_are_out_of_line(db_session: Session, cat: Catalogue):
    """A full re-evaluation: it raises what is missing and resolves what isn't due,
    including an alert whose item has neither a batch nor a par level any more."""
    sugar = add(db_session, new_item(cat.business.id, name="Sugar", base_unit="g"))
    default = default_with(db_session, cat, "-10")
    add(db_session, new_alert(cat.location, sugar, raised_at=nov(1)))

    sweep(db_session, cat, nov(10))

    assert open_alerts(db_session, cat) == {("negative_stock", default.id)}


def test_the_sweep_refuses_an_unknown_business_and_a_time_without_a_timezone(
    db_session: Session, cat: Catalogue
):
    with pytest.raises(DomainError, match="not found"):
        sweep_alerts(db_session, uuid.uuid4(), nov(10))
    with pytest.raises(DomainError, match="as_of must include a timezone"):
        sweep(db_session, cat, datetime(2026, 11, 10, 9))


def test_sweep_if_due_sweeps_once_a_day(db_session: Session, cat: Catalogue):
    batch = batch_with(db_session, cat, "1000", expires_day=10)
    business_id = cat.business.id

    # Never swept: due.
    assert sweep_alerts_if_due(db_session, business_id, nov(10)) == day(10)
    assert open_alerts(db_session, cat) == {("expiring", batch.id)}

    # Later the same day: not due, so nothing is evaluated. The stale alert
    # planted here would be resolved by a sweep.
    planted = add(db_session, new_alert(cat.location, cat.item, raised_at=nov(10)))
    assert sweep_alerts_if_due(db_session, business_id, nov(10, 15)) == day(10)
    assert planted.id in {alert.id for alert in alerts(db_session, cat)}

    # The next day: due again.
    assert sweep_alerts_if_due(db_session, business_id, nov(11)) == day(11)
    assert open_alerts(db_session, cat) == {("expired", batch.id)}

    # A clock behind the alerts date leaves it alone.
    assert sweep_alerts_if_due(db_session, business_id, nov(9)) == day(11)
    assert swept_on(db_session, cat) == day(11)


# --- The check ------------------------------------------------------------------


def test_check_alerts_reports_what_a_fresh_evaluation_would_change(
    db_session: Session, cat: Catalogue
):
    flour = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    default = default_with(db_session, cat, "-10")
    business_id = cat.business.id

    # Due, but nothing was evaluated yet.
    missing = AlertDifference(
        "missing", AlertKey("negative_stock", cat.location.id, cat.item.id, default.id)
    )
    assert check_alerts(db_session, business_id) == [missing]

    evaluate(db_session, cat, nov(10))
    assert check_alerts(db_session, business_id) == []

    # Open, but not due: flour has no par level.
    add(db_session, new_alert(cat.location, flour, raised_at=nov(10)))
    not_due = AlertDifference("not_due", AlertKey("low_stock", cat.location.id, flour.id, None))
    assert check_alerts(db_session, business_id) == [not_due]


def test_check_alerts_judges_dates_as_of_the_last_sweep(db_session: Session, cat: Catalogue):
    batch_with(db_session, cat, "1000", expires_day=10)
    sweep(db_session, cat, nov(10))

    # The batch expired days ago by the wall clock, but the check judges as of
    # the alerts date, 10 Nov, when it was still expiring.
    assert check_alerts(db_session, cat.business.id) == []
