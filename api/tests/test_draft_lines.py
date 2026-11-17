"""Draft order lines: worked examples of what is due and how the draft follows it
(domain rule 8, D67, D68).

Most tests build batches and balances with the helpers from test_alerts.py
and call evaluate_draft_lines directly; the last section goes through the
stock writes and the sweep. The cat fixture's item is whole milk, whose
preferred supplier sells it by the 1000 ml bottle. Its par level here is
"reorder at 2000 ml, order up to 6000 ml" unless a test says otherwise.
"""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.draft_lines import check_draft_lines, evaluate_draft_lines
from app.domain.rules import sweep
from app.models import Item, PurchaseOrder, PurchaseOrderLine
from tests.builders import (
    OCCURRED_AT,
    Catalogue,
    add,
    new_item,
    new_location,
    new_order_line,
    new_par_level,
    new_purchase_order,
    new_supplier,
    new_supplier_item,
    new_unit,
)
from tests.test_alerts import batch_with, d, default_with, nov, open_alerts, set_on_hand
from tests.test_counts import do_count, tracked
from tests.test_stock import assert_reconciled, do_receive, do_use, milk

# --- Helpers --------------------------------------------------------------------


def evaluate(db: Session, cat: Catalogue, item: Item | None = None):
    pair = (cat.location.id, (item or cat.item).id)
    return evaluate_draft_lines(db, cat.business.id, [pair])


def orders(db: Session, cat: Catalogue, status: str = "draft") -> list[PurchaseOrder]:
    return list(
        db.scalars(
            select(PurchaseOrder)
            .where(PurchaseOrder.business_id == cat.business.id, PurchaseOrder.status == status)
            .order_by(PurchaseOrder.id)
        )
    )


def lines(db: Session, cat: Catalogue) -> list[tuple[UUID, UUID, int, int | None]]:
    """The business's draft lines as (supplier_id, item_id, suggested_packs, qty_packs)."""
    rows = db.execute(
        select(
            PurchaseOrder.supplier_id,
            PurchaseOrderLine.item_id,
            PurchaseOrderLine.suggested_packs,
            PurchaseOrderLine.qty_packs,
        )
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.po_id)
        .where(PurchaseOrder.business_id == cat.business.id, PurchaseOrder.status == "draft")
        .order_by(PurchaseOrderLine.id)
    )
    return [tuple(row) for row in rows]


def suggested(db: Session, cat: Catalogue) -> list[int]:
    return [packs for _, _, packs, _ in lines(db, cat)]


def sent_order(db: Session, cat: Catalogue, packs: int, **overrides) -> PurchaseOrder:
    """A sent order to cat's supplier for that many bottles of milk."""
    values = {"status": "sent", "sent_at": OCCURRED_AT} | overrides
    order = add(db, new_purchase_order(cat.location, cat.supplier, **values))
    add(db, new_order_line(order, cat.item, cat.unit, suggested_packs=packs, qty_packs=packs))
    return order


@pytest.fixture
def par(db_session: Session, cat: Catalogue):
    """Milk: low stock at 1000 ml, reorder at 2000 ml, order up to 6000 ml."""
    return add(db_session, new_par_level(cat.location, cat.item))


# --- When a line is due, and for how much ---------------------------------------


@pytest.mark.parametrize(
    ("on_hand", "packs"),
    [
        ("2000.001", None),  # above the reorder point: nothing to order
        ("2000", 4),  # at it: 4000 ml short of 6000, four bottles
        ("1999.999", 5),  # 4000.001 ml short rounds up to five
        ("0.001", 6),
    ],
)
def test_a_line_is_due_at_or_below_the_reorder_point_in_whole_packs(
    db_session: Session, cat: Catalogue, par, on_hand: str, packs: int | None
):
    batch_with(db_session, cat, on_hand, expires_day=20)

    evaluate(db_session, cat)

    assert suggested(db_session, cat) == ([] if packs is None else [packs])


def test_the_line_goes_on_the_preferred_supplier_s_draft_in_its_pack_unit(
    db_session: Session, cat: Catalogue, par
):
    # No stock at all: 6000 ml to order.
    (difference,) = evaluate(db_session, cat)

    assert (difference.kind, difference.wanted_packs) == ("missing", 6)
    (order,) = orders(db_session, cat)
    assert (order.location_id, order.supplier_id) == (cat.location.id, cat.supplier.id)
    assert (order.sent_at, order.received_at, order.cancelled_at) == (None, None, None)
    line = db_session.scalars(select(PurchaseOrderLine)).one()
    assert (line.po_id, line.item_id, line.item_unit_id) == (order.id, cat.item.id, cat.unit.id)
    assert (line.suggested_packs, line.qty_packs) == (6, None)

    # Evaluating again changes nothing.
    assert evaluate(db_session, cat) == []


def test_above_the_reorder_point_there_is_no_draft_at_all(db_session: Session, cat: Catalogue, par):
    batch_with(db_session, cat, "5000", expires_day=20)

    assert evaluate(db_session, cat) == []
    assert orders(db_session, cat) == []


def test_no_line_without_a_par_level_a_preferred_supplier_or_an_active_item(
    db_session: Session, cat: Catalogue
):
    assert evaluate(db_session, cat) == []  # no par level

    add(db_session, new_par_level(cat.location, cat.item))
    cat.supplier_item.is_preferred = False
    assert evaluate(db_session, cat) == []  # nobody to order from

    cat.supplier_item.is_preferred = True
    cat.item.is_active = False
    assert evaluate(db_session, cat) == []  # a retired item isn't reordered

    cat.item.is_active = True
    assert [x.kind for x in evaluate(db_session, cat)] == ["missing"]


def test_a_negative_default_batch_counts_against_usable_on_hand(
    db_session: Session, cat: Catalogue, par
):
    batch_with(db_session, cat, "2500", expires_day=20)
    default_with(db_session, cat, "-700")  # 1800 usable

    evaluate(db_session, cat)

    assert suggested(db_session, cat) == [5]  # 6000 - 1800 = 4200 ml


def test_expired_stock_is_not_usable_so_the_sweep_can_put_an_item_on_a_draft(
    db_session: Session, cat: Catalogue, par
):
    batch_with(db_session, cat, "3000", expires_day=10)
    batch_with(db_session, cat, "1000", expires_day=25)

    sweep(db_session, cat.business.id, nov(10))  # the first batch's last day
    assert suggested(db_session, cat) == []

    sweep(db_session, cat.business.id, nov(11))  # only 1000 ml is usable now
    assert suggested(db_session, cat) == [5]


# --- On order -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent_packs", "packs"),
    [
        (3, 1),  # 6000 - 2000 on hand - 3000 on order = 1000 ml
        (4, None),  # the sent order already brings it up to 6000
        (9, None),  # more than enough on order
    ],
)
def test_what_is_on_a_sent_order_is_not_ordered_again(
    db_session: Session, cat: Catalogue, par, sent_packs: int, packs: int | None
):
    batch_with(db_session, cat, "2000", expires_day=20)
    sent_order(db_session, cat, sent_packs)

    evaluate(db_session, cat)

    assert suggested(db_session, cat) == ([] if packs is None else [packs])


def test_only_sent_orders_at_the_location_count_as_on_order(
    db_session: Session, cat: Catalogue, par
):
    batch_with(db_session, cat, "2000", expires_day=20)
    elsewhere = add(db_session, new_location(cat.business.id, name="Second branch"))
    sent_order(db_session, cat, 4, status="received", received_at=OCCURRED_AT)
    sent_order(db_session, cat, 4, status="cancelled", cancelled_at=OCCURRED_AT)
    sent_order(db_session, cat, 4, location_id=elsewhere.id)

    evaluate(db_session, cat)

    assert suggested(db_session, cat) == [4]


def test_on_order_is_counted_in_each_line_s_own_pack_unit(db_session: Session, cat: Catalogue, par):
    batch_with(db_session, cat, "2000", expires_day=20)
    crate = add(db_session, new_unit(cat.item, name="crate", factor_to_base=Decimal("3000")))
    order = add(
        db_session,
        new_purchase_order(cat.location, cat.supplier, status="sent", sent_at=OCCURRED_AT),
    )
    add(db_session, new_order_line(order, cat.item, crate, suggested_packs=1, qty_packs=1))

    evaluate(db_session, cat)

    # One crate of 3000 ml is on order, so one more bottle reaches 6000.
    assert suggested(db_session, cat) == [1]


# --- The draft follows the stock ------------------------------------------------


def test_the_suggestion_grows_as_stock_falls_and_keeps_the_owner_s_number(
    db_session: Session, cat: Catalogue, par
):
    batch = batch_with(db_session, cat, "2000", expires_day=20)
    evaluate(db_session, cat)
    line = db_session.scalars(select(PurchaseOrderLine)).one()
    line.qty_packs = 3  # the owner wants three

    set_on_hand(db_session, batch, "900")
    (difference,) = evaluate(db_session, cat)

    assert (difference.kind, difference.line_packs, difference.wanted_packs) == ("mismatch", 4, 6)
    assert lines(db_session, cat) == [(cat.supplier.id, cat.item.id, 6, 3)]


def test_a_line_is_removed_when_the_item_recovers_and_the_empty_draft_is_reused(
    db_session: Session, cat: Catalogue, par
):
    batch = batch_with(db_session, cat, "2000", expires_day=20)
    evaluate(db_session, cat)
    (draft,) = orders(db_session, cat)

    set_on_hand(db_session, batch, "5000")
    (difference,) = evaluate(db_session, cat)

    assert difference.kind == "not_due"
    assert lines(db_session, cat) == []
    assert [order.id for order in orders(db_session, cat)] == [draft.id]  # kept, empty

    set_on_hand(db_session, batch, "100")
    evaluate(db_session, cat)
    assert suggested(db_session, cat) == [6]
    assert [order.id for order in orders(db_session, cat)] == [draft.id]  # the same draft


def test_items_of_one_supplier_share_a_draft_and_each_supplier_has_its_own(
    db_session: Session, cat: Catalogue, par
):
    business_id = cat.business.id
    bakery = add(db_session, new_supplier(business_id, name="Bakery Co"))
    for name, supplier in (("Cream", cat.supplier), ("Croissant", bakery)):
        item = add(db_session, new_item(business_id, name=name))
        unit = add(db_session, new_unit(item))
        add(db_session, new_supplier_item(supplier, item, unit, is_preferred=True))
        add(db_session, new_par_level(cat.location, item))

    sweep(db_session, business_id, nov(10))

    drafts = orders(db_session, cat)
    assert sorted(order.supplier_id for order in drafts) == sorted([cat.supplier.id, bakery.id])
    per_supplier = {
        supplier_id: sum(1 for line in lines(db_session, cat) if line[0] == supplier_id)
        for supplier_id in (cat.supplier.id, bakery.id)
    }
    assert per_supplier == {cat.supplier.id: 2, bakery.id: 1}


def test_drafts_are_per_location(db_session: Session, cat: Catalogue, par):
    second = add(db_session, new_location(cat.business.id, name="Second branch"))
    add(db_session, new_par_level(second, cat.item, order_up_to=d("3000")))

    evaluate_draft_lines(
        db_session, cat.business.id, [(cat.location.id, cat.item.id), (second.id, cat.item.id)]
    )

    by_location = {order.location_id: order.id for order in orders(db_session, cat)}
    assert by_location.keys() == {cat.location.id, second.id}
    assert sorted(suggested(db_session, cat)) == [3, 6]


def test_a_change_of_preferred_supplier_moves_the_line_to_the_new_supplier_s_draft(
    db_session: Session, cat: Catalogue, par
):
    evaluate(db_session, cat)
    assert lines(db_session, cat) == [(cat.supplier.id, cat.item.id, 6, None)]

    # Another supplier becomes preferred, selling 2 L bottles.
    other_supplier = add(db_session, new_supplier(cat.business.id, name="Hilltop Dairy"))
    big = add(db_session, new_unit(cat.item, name="2 L bottle", factor_to_base=d("2000")))
    cat.supplier_item.is_preferred = False
    db_session.flush()
    add(db_session, new_supplier_item(other_supplier, cat.item, big, is_preferred=True))

    assert sorted(x.kind for x in evaluate(db_session, cat)) == ["missing", "not_due"]
    assert lines(db_session, cat) == [(other_supplier.id, cat.item.id, 3, None)]


def test_a_change_of_pack_unit_resets_the_owner_s_number(db_session: Session, cat: Catalogue, par):
    evaluate(db_session, cat)
    line = db_session.scalars(select(PurchaseOrderLine)).one()
    line.qty_packs = 2
    crate = add(db_session, new_unit(cat.item, name="crate", factor_to_base=d("3000")))
    cat.supplier_item.item_unit_id = crate.id
    db_session.flush()

    evaluate(db_session, cat)

    db_session.refresh(line)
    # Two bottles meant something; two crates wouldn't.
    assert (line.item_unit_id, line.suggested_packs, line.qty_packs) == (crate.id, 2, None)


def test_a_suggestion_can_be_larger_than_a_32_bit_integer(db_session: Session, cat: Catalogue):
    """A big order_up_to over a tiny pack: six billion packs. The columns are
    bigint so that this is a silly suggestion and not a failed stock write."""
    add(db_session, new_par_level(cat.location, cat.item, order_up_to=d("6000000")))
    drop = add(db_session, new_unit(cat.item, name="drop", factor_to_base=d("0.001")))
    cat.supplier_item.item_unit_id = drop.id
    db_session.flush()

    evaluate(db_session, cat)

    assert suggested(db_session, cat) == [6_000_000_000]


def test_evaluate_only_looks_at_the_pairs_it_is_given_in_its_own_business(
    db_session: Session, cat: Catalogue, other: Catalogue, par
):
    add(db_session, new_par_level(other.location, other.item))

    assert evaluate_draft_lines(db_session, cat.business.id, []) == []
    evaluate_draft_lines(db_session, cat.business.id, [(other.location.id, other.item.id)])

    assert orders(db_session, cat) == [] and orders(db_session, other) == []


# --- The check ------------------------------------------------------------------


def test_check_draft_lines_reports_what_a_fresh_evaluation_would_change(
    db_session: Session, cat: Catalogue, par
):
    business_id = cat.business.id
    batch = batch_with(db_session, cat, "2000", expires_day=20)

    assert [x.kind for x in check_draft_lines(db_session, business_id)] == ["missing"]

    evaluate(db_session, cat)
    assert check_draft_lines(db_session, business_id) == []

    set_on_hand(db_session, batch, "900")
    assert [x.kind for x in check_draft_lines(db_session, business_id)] == ["mismatch"]

    set_on_hand(db_session, batch, "5000")
    assert [x.kind for x in check_draft_lines(db_session, business_id)] == ["not_due"]


def test_check_draft_lines_finds_a_line_whose_par_level_is_gone(
    db_session: Session, cat: Catalogue
):
    draft = add(db_session, new_purchase_order(cat.location, cat.supplier))
    add(db_session, new_order_line(draft, cat.item, cat.unit))

    assert [x.kind for x in check_draft_lines(db_session, cat.business.id)] == ["not_due"]


# --- Through the stock writes ---------------------------------------------------


def assert_in_line(db: Session, cat: Catalogue) -> None:
    assert check_draft_lines(db, cat.business.id) == []
    assert_reconciled(db, cat)


def test_usage_puts_an_item_on_the_draft_and_a_receipt_takes_it_off(
    db_session: Session, cat: Catalogue, par
):
    do_receive(db_session, cat, milk(cat, "5"), received_at=nov(9))
    assert orders(db_session, cat) == []

    do_use(db_session, cat, cat.item, "2999", occurred_at=nov(10, 8))
    assert suggested(db_session, cat) == []  # 2001 ml left
    do_use(db_session, cat, cat.item, "1", occurred_at=nov(10, 9))
    assert suggested(db_session, cat) == [4]
    do_use(db_session, cat, cat.item, "1500", occurred_at=nov(10, 10))
    assert suggested(db_session, cat) == [6]  # 500 ml left: 5500 short
    assert_in_line(db_session, cat)

    do_receive(db_session, cat, milk(cat, "6"), received_at=nov(11))
    assert suggested(db_session, cat) == []
    assert_in_line(db_session, cat)


def test_between_the_low_stock_level_and_the_reorder_point_milk_is_on_the_draft_but_not_low(
    db_session: Session, cat: Catalogue, par
):
    # The par fixture: low stock at 1000 ml, reorder at 2000 ml (D83).
    do_receive(db_session, cat, milk(cat, "5", expires_day=20), received_at=nov(9))

    do_use(db_session, cat, cat.item, "3000", occurred_at=nov(10, 8))
    assert suggested(db_session, cat) == [4]  # 2000 ml left: at the reorder point
    assert open_alerts(db_session, cat) == set()  # but not short
    do_use(db_session, cat, cat.item, "999", occurred_at=nov(10, 9))
    assert open_alerts(db_session, cat) == set()  # 1001 ml left
    do_use(db_session, cat, cat.item, "1", occurred_at=nov(10, 10))
    assert suggested(db_session, cat) == [5]
    assert open_alerts(db_session, cat) == {("low_stock", None)}
    assert_in_line(db_session, cat)

    do_receive(db_session, cat, milk(cat, "5", expires_day=20), received_at=nov(11))
    assert (suggested(db_session, cat), open_alerts(db_session, cat)) == ([], set())
    assert_in_line(db_session, cat)


def test_a_count_and_a_shortfall_move_the_draft_too(db_session: Session, cat: Catalogue, par):
    received = do_receive(db_session, cat, milk(cat, "5"), received_at=nov(9))
    batch_id = received.movements[0].batch_id

    # The count finds 1.2 L where 5 L was expected.
    do_count(db_session, cat, tracked(cat.item, (batch_id, "1200")), counted_at=nov(10))
    assert suggested(db_session, cat) == [5]

    # A usage of more than is there leaves a shortfall of 300 ml, which counts.
    do_use(db_session, cat, cat.item, "1500", occurred_at=nov(10, 12))
    assert suggested(db_session, cat) == [7]  # 6000 - (-300) = 6300 ml
    assert_in_line(db_session, cat)
