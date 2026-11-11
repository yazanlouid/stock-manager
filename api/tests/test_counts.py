"""Stock counts: worked examples of count_stock() (domain rule 9, D54 and D55).

Each test runs in the rolled-back transaction from conftest.py. The cat fixture's
item is whole milk, which tracks batches and expiry, bought by the 1000 ml
bottle; flour doesn't track batches. The flour and eggs fixtures are in
conftest.py, and the helpers come from test_stock.py. Tests that write stock
end by checking that the balances still match the ledger (domain rule 1).
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.counts import BatchCountInput, CountItemInput, CountResult, count_stock
from app.domain.errors import DomainError
from app.models import Batch, Item, ItemUnit, StockCount
from tests.builders import Catalogue, add, new_batch, new_location
from tests.test_stock import (
    assert_reconciled,
    batch,
    d,
    default_batch,
    do_receive,
    do_reverse,
    do_use,
    line,
    milk,
    movement_count,
    nov,
    on_hand,
)

# After every movement the tests post, so the count barrier (D51) never bites.
COUNT_AT = nov(10, 20)


# --- Helpers --------------------------------------------------------------------


def untracked(item: Item, counted_qty: str) -> CountItemInput:
    return CountItemInput(item.id, counted_qty=d(counted_qty))


def tracked(item: Item, *batches: tuple[UUID, str]) -> CountItemInput:
    """A batch-tracked item and the batches found; none named means none found."""
    return CountItemInput(
        item.id, batches=[BatchCountInput(batch_id, d(qty)) for batch_id, qty in batches]
    )


def do_count(
    db: Session,
    cat: Catalogue,
    *items: CountItemInput,
    counted_at: datetime = COUNT_AT,
    client_id: UUID | None = None,
    source: str = "count",
    location_id: UUID | None = None,
) -> CountResult:
    return count_stock(
        db,
        cat.business.id,
        location_id=location_id or cat.location.id,
        counted_at=counted_at,
        items=items,
        client_id=client_id or uuid.uuid4(),
        source=source,
    )


def lines_of(result: CountResult) -> list[tuple[UUID, Decimal, Decimal]]:
    return [(x.batch_id, x.expected_qty, x.counted_qty) for x in result.lines]


def adjustments(result: CountResult) -> list[tuple[UUID, Decimal]]:
    return [(m.batch_id, m.qty_delta) for m in result.movements]


def count_rows(db: Session, cat: Catalogue) -> int:
    return db.scalar(
        select(func.count())
        .select_from(StockCount)
        .where(StockCount.business_id == cat.business.id)
    )


# --- An item without batch tracking ---------------------------------------------


def test_an_untracked_item_counted_below_then_above_its_balance(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, sack = flour
    do_receive(db_session, cat, line(item, sack, "1", "12.500"))
    default = default_batch(db_session, cat, item)

    below = do_count(db_session, cat, untracked(item, "24000"))
    above = do_count(db_session, cat, untracked(item, "24500"), counted_at=nov(10, 21))

    assert lines_of(below) == [(default.id, d("25000"), d("24000"))]
    assert adjustments(below) == [(default.id, d("-1000"))]
    assert lines_of(above) == [(default.id, d("24000"), d("24500"))]
    assert adjustments(above) == [(default.id, d("500"))]
    assert on_hand(db_session, default.id) == d("24500")
    assert_reconciled(db_session, cat)


def test_an_untracked_item_counted_at_its_balance_gets_a_line_and_no_movement(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, sack = flour
    do_receive(db_session, cat, line(item, sack, "1", "12.500"))

    result = do_count(db_session, cat, untracked(item, "25000"))

    default = default_batch(db_session, cat, item)
    assert lines_of(result) == [(default.id, d("25000"), d("25000"))]
    assert result.movements == ()
    assert_reconciled(db_session, cat)


# --- A batch-tracked item -------------------------------------------------------


def test_named_batches_take_their_counts_and_the_rest_go_to_zero(
    db_session: Session, cat: Catalogue
) -> None:
    on_12, on_15 = (
        m.batch_id
        for m in do_receive(
            db_session, cat, milk(cat, "1", expires_day=12), milk(cat, "1", expires_day=15)
        ).movements
    )
    (late,) = do_receive(
        db_session, cat, milk(cat, "1", expires_day=20), received_at=nov(5)
    ).movements
    on_20 = late.batch_id
    # Logged for 3 Nov, before the 5 Nov delivery: it empties the 12 and 15 Nov
    # batches and takes the default batch to -500 (D50 keeps it off the 20 Nov one).
    do_use(db_session, cat, cat.item, "2500", occurred_at=nov(3))
    default = default_batch(db_session, cat, cat.item)

    # 200 ml of the 15 Nov batch turn up; the 20 Nov batch isn't found.
    result = do_count(db_session, cat, tracked(cat.item, (on_15, "200")))

    # The empty, unnamed 12 Nov batch gets no line. FEFO order, default last.
    assert lines_of(result) == [
        (on_15, d("0"), d("200")),
        (on_20, d("1000"), d("0")),
        (default.id, d("-500"), d("0")),
    ]
    assert adjustments(result) == [(on_15, d("200")), (on_20, d("-1000")), (default.id, d("500"))]
    assert [on_hand(db_session, b) for b in (on_12, on_15, on_20, default.id)] == [0, 200, 0, 0]
    assert_reconciled(db_session, cat)


def test_a_positive_default_batch_is_set_to_zero(db_session: Session, cat: Catalogue) -> None:
    (first,) = do_receive(db_session, cat, milk(cat, "1", expires_day=12)).movements
    shortfall = do_use(db_session, cat, cat.item, "1500", occurred_at=nov(3))
    # The next delivery settles the -500, then reversing the usage puts the
    # 500 back into the default batch: stock without a batch.
    second = do_receive(db_session, cat, milk(cat, "1", expires_day=15), received_at=nov(4))
    do_reverse(db_session, cat, shortfall.movements[0].client_id)
    default = default_batch(db_session, cat, cat.item)
    on_15 = second.movements[0].batch_id
    assert on_hand(db_session, default.id) == d("500")

    result = do_count(db_session, cat, tracked(cat.item, (first.batch_id, "1000")))

    assert lines_of(result) == [
        (first.batch_id, d("1000"), d("1000")),
        (on_15, d("500"), d("0")),
        (default.id, d("500"), d("0")),
    ]
    assert adjustments(result) == [(on_15, d("-500")), (default.id, d("-500"))]
    assert_reconciled(db_session, cat)


def test_an_empty_batches_list_zeroes_every_batch(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(
        db_session, cat, milk(cat, "1", expires_day=12), milk(cat, "2", expires_day=15)
    )
    on_12, on_15 = (m.batch_id for m in received.movements)
    default = default_batch(db_session, cat, cat.item)

    result = do_count(db_session, cat, tracked(cat.item))

    assert lines_of(result) == [
        (on_12, d("1000"), d("0")),
        (on_15, d("2000"), d("0")),
        (default.id, d("0"), d("0")),
    ]
    assert adjustments(result) == [(on_12, d("-1000")), (on_15, d("-2000"))]
    assert_reconciled(db_session, cat)


def test_an_item_with_nothing_expected_or_found_still_gets_its_default_line(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, _ = flour

    # Neither item was ever received, so the count creates their default batches.
    result = do_count(db_session, cat, untracked(item, "0"), tracked(cat.item))

    defaults = {x.id: default_batch(db_session, cat, x).id for x in (item, cat.item)}
    assert sorted(lines_of(result)) == sorted(
        [(defaults[item.id], d("0"), d("0")), (defaults[cat.item.id], d("0"), d("0"))]
    )
    assert result.movements == ()
    assert_reconciled(db_session, cat)


def test_lines_are_sorted_by_sku(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, _ = flour
    item.sku, cat.item.sku = "B-FLOUR", "A-MILK"
    db_session.flush()

    result = do_count(db_session, cat, untracked(item, "0"), tracked(cat.item))

    assert [x.item_id for x in result.lines] == [cat.item.id, item.id]


# --- Count adjustments ----------------------------------------------------------


def test_each_count_adjust_carries_its_line_and_batch_cost_and_changes_no_cost(
    db_session: Session, cat: Catalogue
) -> None:
    (received,) = do_receive(db_session, cat, milk(cat, "1", "0.850", expires_day=12)).movements
    do_use(db_session, cat, cat.item, "1500", occurred_at=nov(3))  # default batch -500, no cost
    default = default_batch(db_session, cat, cat.item)
    costs_before = {b: batch(db_session, b).unit_cost for b in (received.batch_id, default.id)}
    client_id = uuid.uuid4()

    result = do_count(
        db_session,
        cat,
        tracked(cat.item, (received.batch_id, "300")),
        client_id=client_id,
        source="simulator",
    )

    line_ids = {x.batch_id: x.id for x in result.lines}
    assert [
        (m.type, m.batch_id, m.qty_delta, m.unit_cost, m.stock_count_line_id)
        for m in result.movements
    ] == [
        ("count_adjust", received.batch_id, d("300"), d("0.000850"), line_ids[received.batch_id]),
        ("count_adjust", default.id, d("500"), None, line_ids[default.id]),
    ]
    assert all(m.occurred_at == COUNT_AT for m in result.movements)
    assert all(m.source == "simulator" for m in result.movements)
    assert [(m.client_id, m.client_seq) for m in result.movements] == [
        (client_id, 0),
        (client_id, 1),
    ]
    assert {b: batch(db_session, b).unit_cost for b in costs_before} == costs_before
    assert_reconciled(db_session, cat)


def test_an_inactive_item_can_be_counted(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, sack = flour
    do_receive(db_session, cat, line(item, sack, "1", "12.500"))
    item.is_active = False
    db_session.flush()

    result = do_count(db_session, cat, untracked(item, "0"))

    assert adjustments(result) == [(default_batch(db_session, cat, item).id, d("-25000"))]
    assert_reconciled(db_session, cat)


# --- Refusals -------------------------------------------------------------------


@pytest.fixture
def setup(db_session: Session, cat: Catalogue, other: Catalogue, flour: tuple[Item, ItemUnit]):
    """Batches the refusal cases name: milk at two locations, flour, another business's."""
    item, sack = flour
    received = do_receive(
        db_session, cat, milk(cat, "1", expires_day=12), line(item, sack, "1", "12.500")
    )
    store = add(db_session, new_location(cat.business.id, name="Storeroom", kind="storeroom"))
    return {
        "milk_batch": received.movements[0].batch_id,
        "milk_default": default_batch(db_session, cat, cat.item).id,
        "flour": item,
        "store": store,
        "store_batch": add(db_session, new_batch(store, cat.item)).id,
        "later_batch": add(
            db_session, new_batch(cat.location, cat.item, received_at=nov(10, 21))
        ).id,
        "other": other,
        "other_batch": add(db_session, new_batch(other.location, other.item)).id,
    }


Case = Callable[[Catalogue, dict[str, Any]], dict[str, Any]]

REFUSALS: list[Any] = [
    pytest.param(lambda c, s: {"items": []}, "at least one item", id="no-items"),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item), tracked(c.item)]},
        "listed twice",
        id="item-twice",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item, (s["milk_batch"], "1"), (s["milk_batch"], "2"))]},
        "listed twice",
        id="batch-twice",
    ),
    pytest.param(
        lambda c, s: {"items": [untracked(c.item, "5")]},
        "tracks batches",
        id="counted-qty-for-tracked-item",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(s["flour"])]},
        "doesn't track batches",
        id="batches-for-untracked-item",
    ),
    pytest.param(
        lambda c, s: {"items": [CountItemInput(s["flour"].id)]},
        "doesn't track batches",
        id="nothing-for-untracked-item",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item, (s["milk_default"], "1"))]},
        "is a default batch",
        id="default-batch-named",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item, (s["store_batch"], "1"))]},
        "not at this location",
        id="batch-at-another-location",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item, (s["later_batch"], "1"))]},
        "after counted_at",
        id="batch-received-after-counted-at",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item, (s["other_batch"], "1"))]},
        "not found",
        id="another-business-batch",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(s["other"].item)]},
        "not found",
        id="another-business-item",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item)], "location_id": s["other"].location.id},
        "not found",
        id="another-business-location",
    ),
    pytest.param(
        lambda c, s: {"items": [untracked(s["flour"], "-0.001")]},
        "must not be negative",
        id="negative-count",
    ),
    pytest.param(
        lambda c, s: {"items": [tracked(c.item, (s["milk_batch"], "1.0005"))]},
        "three decimal places",
        id="four-decimals",
    ),
]


@pytest.mark.parametrize(("case", "message"), REFUSALS)
def test_a_bad_count_is_refused(
    db_session: Session, cat: Catalogue, setup: dict[str, Any], case: Case, message: str
) -> None:
    arguments = case(cat, setup)
    before = movement_count(db_session, cat), count_rows(db_session, cat)

    with pytest.raises(DomainError, match=message):
        do_count(db_session, cat, *arguments.pop("items"), **arguments)

    assert (movement_count(db_session, cat), count_rows(db_session, cat)) == before


def test_a_batch_of_another_item_is_refused(
    db_session: Session, cat: Catalogue, eggs: tuple[Item, ItemUnit]
) -> None:
    item, tray = eggs
    received = do_receive(db_session, cat, milk(cat, "1"), line(item, tray, "1", "3.000"))

    with pytest.raises(DomainError, match="is not a batch of item"):
        do_count(db_session, cat, tracked(item, (received.movements[0].batch_id, "1")))


# --- Replay ---------------------------------------------------------------------


def test_a_retried_count_returns_what_it_wrote(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"), milk(cat, "1", expires_day=25))
    client_id = uuid.uuid4()
    first = do_count(
        db_session,
        cat,
        tracked(cat.item, (received.movements[0].batch_id, "400")),
        client_id=client_id,
    )
    before = movement_count(db_session, cat), count_rows(db_session, cat)

    # A retry with different contents still replays the first count.
    again = do_count(db_session, cat, tracked(cat.item), client_id=client_id)

    assert again.replayed and not first.replayed
    assert again.count is first.count
    assert [x.id for x in again.lines] == [x.id for x in first.lines]
    assert [m.id for m in again.movements] == [m.id for m in first.movements]
    assert len(first.movements) == 2
    assert (movement_count(db_session, cat), count_rows(db_session, cat)) == before


def test_a_count_that_changed_nothing_still_replays(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, _ = flour
    client_id = uuid.uuid4()
    first = do_count(db_session, cat, untracked(item, "0"), client_id=client_id)

    again = do_count(db_session, cat, untracked(item, "0"), client_id=client_id)

    assert first.movements == () and again.replayed
    assert again.count is first.count
    assert [x.id for x in again.lines] == [x.id for x in first.lines]
    assert count_rows(db_session, cat) == 1


def test_a_count_s_client_id_is_refused_for_a_usage(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, _ = flour
    client_id = uuid.uuid4()
    # The count posts nothing, so only stock_counts knows the client_id.
    do_count(db_session, cat, untracked(item, "0"), client_id=client_id)

    with pytest.raises(DomainError, match="already used for a different request"):
        do_use(db_session, cat, item, "1", occurred_at=nov(11), client_id=client_id)


def test_a_usage_s_client_id_is_refused_for_a_count(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, _ = flour
    usage = do_use(db_session, cat, item, "100")

    with pytest.raises(DomainError, match="already used for a different request"):
        do_count(db_session, cat, untracked(item, "0"), client_id=usage.movements[0].client_id)


def test_counts_are_scoped_to_the_business(
    db_session: Session, cat: Catalogue, other: Catalogue
) -> None:
    client_id = uuid.uuid4()
    do_count(db_session, cat, tracked(cat.item), client_id=client_id)

    # The same client_id in another business is a new count, not a replay.
    result = do_count(db_session, other, tracked(other.item), client_id=client_id)

    assert not result.replayed
    assert db_session.scalar(
        select(Batch.business_id).where(Batch.id == result.lines[0].batch_id)
    ) == (other.business.id)
