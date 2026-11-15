"""Order actions and receiving against an order: worked examples (D69 to D72).

The cat fixture's item is whole milk, whose preferred supplier sells it by the
1000 ml bottle; its par level here is "reorder at 2000 ml, order up to 6000
ml". Cream is a second item from the same supplier with the same par level.
With no stock, the rule drafts six bottles of each.
"""

import uuid
from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.draft_lines import check_draft_lines
from app.domain.errors import DomainError
from app.domain.orders import cancel_order, order_packs, send_order, set_line_quantity
from app.domain.rules import sweep
from app.domain.stock import ReceiptLineInput, ReceiveResult, receive
from app.models import Item, ItemUnit, PurchaseOrder, PurchaseOrderLine, Receipt, StockMovement
from tests.builders import (
    Catalogue,
    add,
    new_item,
    new_location,
    new_par_level,
    new_supplier,
    new_supplier_item,
    new_unit,
)
from tests.test_alerts import d, nov, recorded_statements
from tests.test_counts import do_count, untracked
from tests.test_draft_lines import orders
from tests.test_stock import assert_reconciled, line, milk

# --- Fixtures and helpers -------------------------------------------------------


@pytest.fixture
def cream(db_session: Session, cat: Catalogue) -> tuple[Item, ItemUnit]:
    """A second item from cat's supplier, by the 1000 ml carton, with a par level."""
    item = add(db_session, new_item(cat.business.id, name="Cream"))
    unit = add(db_session, new_unit(item, name="carton"))
    add(db_session, new_supplier_item(cat.supplier, item, unit, is_preferred=True))
    add(db_session, new_par_level(cat.location, item))
    return item, unit


@pytest.fixture
def draft(db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit]) -> PurchaseOrder:
    """The supplier's draft: six bottles of milk and six cartons of cream."""
    add(db_session, new_par_level(cat.location, cat.item))
    sweep(db_session, cat.business.id, nov(9))
    (order,) = orders(db_session, cat)
    return order


def order_lines(db: Session, order: PurchaseOrder) -> dict[UUID, PurchaseOrderLine]:
    """The order's lines by item id, as stored now."""
    rows = db.scalars(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.po_id == order.id)
        .execution_options(populate_existing=True)
    )
    return {row.item_id: row for row in rows}


def packs(db: Session, order: PurchaseOrder) -> dict[UUID, tuple[int, int | None]]:
    """item id -> (suggested_packs, qty_packs)."""
    return {
        item_id: (row.suggested_packs, row.qty_packs)
        for item_id, row in order_lines(db, order).items()
    }


def send(db: Session, cat: Catalogue, order: PurchaseOrder, at: datetime | None = None):
    return send_order(db, cat.business.id, order_id=order.id, sent_at=at or nov(10))


def set_qty(db: Session, cat: Catalogue, order: PurchaseOrder, item: Item, qty: int | None):
    line_id = order_lines(db, order)[item.id].id
    return set_line_quantity(db, cat.business.id, order_id=order.id, line_id=line_id, qty_packs=qty)


def receive_against(
    db: Session,
    cat: Catalogue,
    order: PurchaseOrder,
    *lines: ReceiptLineInput,
    received_at: datetime | None = None,
    client_id: UUID | None = None,
    supplier_id: UUID | None = None,
    location_id: UUID | None = None,
) -> ReceiveResult:
    return receive(
        db,
        cat.business.id,
        location_id=location_id or cat.location.id,
        supplier_id=supplier_id or cat.supplier.id,
        received_at=received_at or nov(11),
        invoice_ref=None,
        lines=lines,
        client_id=client_id or uuid.uuid4(),
        source="manual",
        po_id=order.id,
    )


def rows(db: Session, cat: Catalogue) -> tuple[int, int]:
    """The business's movements and receipts, to show a refusal left nothing."""
    return tuple(
        db.scalar(
            select(func.count()).select_from(model).where(model.business_id == cat.business.id)
        )
        for model in (StockMovement, Receipt)
    )


def assert_in_line(db: Session, cat: Catalogue) -> None:
    assert check_draft_lines(db, cat.business.id) == []
    assert_reconciled(db, cat)


# --- Setting a line's quantity --------------------------------------------------


def test_the_owner_s_number_replaces_the_suggestion_until_it_is_cleared(
    db_session: Session, cat: Catalogue, draft: PurchaseOrder
):
    line_of_milk = order_lines(db_session, draft)[cat.item.id]
    assert (order_packs(line_of_milk), line_of_milk.qty_packs) == (6, None)

    set_qty(db_session, cat, draft, cat.item, 4)
    assert packs(db_session, draft)[cat.item.id] == (6, 4)
    assert order_packs(order_lines(db_session, draft)[cat.item.id]) == 4

    set_qty(db_session, cat, draft, cat.item, 0)  # don't order this
    assert order_packs(order_lines(db_session, draft)[cat.item.id]) == 0

    set_qty(db_session, cat, draft, cat.item, None)  # as suggested again
    assert packs(db_session, draft)[cat.item.id] == (6, None)
    assert_in_line(db_session, cat)


def test_a_line_s_quantity_must_be_a_whole_number_of_packs_zero_or_more(
    db_session: Session, cat: Catalogue, draft: PurchaseOrder
):
    with pytest.raises(DomainError, match="qty_packs must not be negative"):
        set_qty(db_session, cat, draft, cat.item, -1)
    for not_an_int in (2.5, "3", True):
        with pytest.raises(TypeError, match="qty_packs must be an int or None"):
            set_qty(db_session, cat, draft, cat.item, not_an_int)
    assert packs(db_session, draft)[cat.item.id] == (6, None)


def test_only_a_draft_s_own_lines_can_be_changed(
    db_session: Session, cat: Catalogue, other: Catalogue, draft: PurchaseOrder
):
    business_id = cat.business.id
    line_id = order_lines(db_session, draft)[cat.item.id].id

    with pytest.raises(DomainError, match="line .* not found on order"):
        set_line_quantity(
            db_session, business_id, order_id=draft.id, line_id=uuid.uuid4(), qty_packs=1
        )
    # Another business can't see the order.
    with pytest.raises(DomainError, match="order .* not found"):
        set_line_quantity(
            db_session, other.business.id, order_id=draft.id, line_id=line_id, qty_packs=1
        )

    send(db_session, cat, draft)
    with pytest.raises(DomainError, match="is sent, not a draft, so its lines can't be changed"):
        set_line_quantity(db_session, business_id, order_id=draft.id, line_id=line_id, qty_packs=1)


# --- Sending --------------------------------------------------------------------


def test_sending_a_draft_makes_its_lines_final_and_puts_them_on_order(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    sent = send(db_session, cat, draft)

    assert (sent.id, sent.status, sent.sent_at) == (draft.id, "sent", nov(10))
    assert (sent.received_at, sent.cancelled_at) == (None, None)
    # Every line now carries what it orders.
    assert packs(db_session, sent) == {cat.item.id: (6, 6), cream[0].id: (6, 6)}
    # Six bottles of each are on order, so nothing more is due: no new draft.
    assert orders(db_session, cat) == []
    assert_in_line(db_session, cat)


def test_an_order_cut_short_of_the_need_leaves_the_rest_on_a_new_draft(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    set_qty(db_session, cat, draft, cat.item, 4)

    sent = send(db_session, cat, draft)

    assert packs(db_session, sent) == {cat.item.id: (6, 4), cream[0].id: (6, 6)}
    # 4000 ml of milk is on order; 2000 ml more reaches 6000.
    (new_draft,) = orders(db_session, cat)
    assert new_draft.id != sent.id
    assert packs(db_session, new_draft) == {cat.item.id: (2, None)}
    assert_in_line(db_session, cat)


def test_sending_drops_the_lines_set_to_zero_and_the_rule_drafts_them_again(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    set_qty(db_session, cat, draft, cream[0], 0)

    sent = send(db_session, cat, draft)

    assert packs(db_session, sent) == {cat.item.id: (6, 6)}
    # Cream wasn't ordered and is still low, so it is on the next draft at once.
    (new_draft,) = orders(db_session, cat)
    assert packs(db_session, new_draft) == {cream[0].id: (6, None)}
    assert_in_line(db_session, cat)


def test_a_draft_with_nothing_to_order_can_t_be_sent(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    set_qty(db_session, cat, draft, cat.item, 0)
    set_qty(db_session, cat, draft, cream[0], 0)

    with pytest.raises(DomainError, match="has nothing to order"):
        send(db_session, cat, draft)

    db_session.refresh(draft)
    assert (draft.status, draft.sent_at) == ("draft", None)
    assert len(order_lines(db_session, draft)) == 2


def test_only_a_draft_can_be_sent(
    db_session: Session, cat: Catalogue, other: Catalogue, draft: PurchaseOrder
):
    with pytest.raises(DomainError, match="order .* not found"):
        send_order(db_session, cat.business.id, order_id=uuid.uuid4(), sent_at=nov(10))
    with pytest.raises(DomainError, match="order .* not found"):
        send_order(db_session, other.business.id, order_id=draft.id, sent_at=nov(10))
    with pytest.raises(DomainError, match="sent_at must include a timezone"):
        send(db_session, cat, draft, at=datetime(2026, 11, 10, 9))

    send(db_session, cat, draft)
    with pytest.raises(DomainError, match="is sent, not a draft, so it can't be sent"):
        send(db_session, cat, draft)


# --- Cancelling -----------------------------------------------------------------


def test_cancelling_a_sent_order_takes_it_off_order_and_the_rule_drafts_again(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    business_id = cat.business.id
    sent = send(db_session, cat, draft)
    assert orders(db_session, cat) == []

    cancelled = cancel_order(db_session, business_id, order_id=sent.id, cancelled_at=nov(12))

    assert (cancelled.status, cancelled.sent_at, cancelled.cancelled_at) == (
        "cancelled",
        nov(10),
        nov(12),
    )
    # Its lines stay as the record of what was ordered.
    assert packs(db_session, cancelled) == {cat.item.id: (6, 6), cream[0].id: (6, 6)}
    (new_draft,) = orders(db_session, cat)
    assert packs(db_session, new_draft) == {cat.item.id: (6, None), cream[0].id: (6, None)}
    assert_in_line(db_session, cat)

    with pytest.raises(DomainError, match="is cancelled, not sent, so it can't be cancelled"):
        cancel_order(db_session, business_id, order_id=sent.id, cancelled_at=nov(13))


def test_a_draft_can_t_be_cancelled(db_session: Session, cat: Catalogue, draft: PurchaseOrder):
    with pytest.raises(DomainError, match="is draft, not sent, so it can't be cancelled"):
        cancel_order(db_session, cat.business.id, order_id=draft.id, cancelled_at=nov(10))


# --- Receiving against an order -------------------------------------------------


def test_a_delivery_against_a_sent_order_closes_it(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    cream_item, carton = cream
    sent = send(db_session, cat, draft)

    result = receive_against(
        db_session, cat, sent, milk(cat, "6"), line(cream_item, carton, "6", "3.500", None)
    )

    assert result.receipt.po_id == sent.id
    db_session.refresh(sent)
    assert (sent.status, sent.sent_at, sent.received_at) == ("received", nov(10), nov(11))
    # Both items are back at 6000, and nothing is on order: no draft.
    assert orders(db_session, cat) == []
    assert_in_line(db_session, cat)


def test_what_came_short_or_didn_t_come_goes_on_the_next_draft(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    sent = send(db_session, cat, draft)

    # Two of the six bottles of milk arrive, and no cream.
    receive_against(db_session, cat, sent, milk(cat, "2"))

    db_session.refresh(sent)
    assert sent.status == "received"
    # Milk has 2000 ml, at its reorder point, with nothing on order any more;
    # cream wasn't on the receipt at all, and is re-evaluated because it was
    # on the order.
    (new_draft,) = orders(db_session, cat)
    assert packs(db_session, new_draft) == {cat.item.id: (4, None), cream[0].id: (6, None)}
    assert_in_line(db_session, cat)


def test_a_delivery_can_bring_more_than_ordered_and_items_that_weren_t(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    item, sack = flour
    sent = send(db_session, cat, draft)

    result = receive_against(db_session, cat, sent, milk(cat, "9"), line(item, sack, "1", "12.500"))

    assert [m.type for m in result.movements] == ["receive", "receive"]
    db_session.refresh(sent)
    assert sent.status == "received"
    assert_in_line(db_session, cat)


@pytest.mark.parametrize("status", ["draft", "received", "cancelled"])
def test_only_a_sent_order_can_be_received_against(
    db_session: Session, cat: Catalogue, draft: PurchaseOrder, status: str
):
    business_id = cat.business.id
    if status != "draft":
        send(db_session, cat, draft)
    if status == "received":
        receive_against(db_session, cat, draft, milk(cat, "6"))
    if status == "cancelled":
        cancel_order(db_session, business_id, order_id=draft.id, cancelled_at=nov(11))
    before = rows(db_session, cat)

    with pytest.raises(DomainError, match=f"is {status}, not sent, so nothing can be received"):
        receive_against(db_session, cat, draft, milk(cat, "1"), received_at=nov(12))

    assert rows(db_session, cat) == before


def test_a_delivery_must_be_from_the_order_s_supplier_at_its_location(
    db_session: Session, cat: Catalogue, other: Catalogue, draft: PurchaseOrder
):
    business_id = cat.business.id
    sent = send(db_session, cat, draft)
    bakery = add(db_session, new_supplier(business_id, name="Bakery Co"))
    second = add(db_session, new_location(business_id, name="Second branch"))
    before = rows(db_session, cat)

    with pytest.raises(DomainError, match="is for another supplier"):
        receive_against(db_session, cat, sent, milk(cat, "6"), supplier_id=bakery.id)
    with pytest.raises(DomainError, match="is for another location"):
        receive_against(db_session, cat, sent, milk(cat, "6"), location_id=second.id)
    with pytest.raises(DomainError, match="order .* not found"):
        receive(
            db_session,
            business_id,
            location_id=cat.location.id,
            supplier_id=cat.supplier.id,
            received_at=nov(11),
            invoice_ref=None,
            lines=[milk(cat, "6")],
            client_id=uuid.uuid4(),
            source="manual",
            po_id=uuid.uuid4(),
        )

    assert rows(db_session, cat) == before
    db_session.refresh(sent)
    assert sent.status == "sent"


def test_a_retried_delivery_against_an_order_replays(
    db_session: Session, cat: Catalogue, draft: PurchaseOrder
):
    sent = send(db_session, cat, draft)
    client_id = uuid.uuid4()
    first = receive_against(db_session, cat, sent, milk(cat, "6"), client_id=client_id)

    again = receive_against(db_session, cat, sent, milk(cat, "6"), client_id=client_id)

    assert again.replayed
    assert again.receipt.id == first.receipt.id
    assert [m.id for m in again.movements] == [m.id for m in first.movements]


def test_the_count_barrier_holds_only_the_items_the_delivery_brings(
    db_session: Session, cat: Catalogue, cream: tuple[Item, ItemUnit], draft: PurchaseOrder
):
    """Cream is on the order but not on the receipt. It was counted after the
    delivery's time, which would refuse a write for cream (D51), but the
    receipt writes no cream: it only re-evaluates cream's draft line."""
    sent = send(db_session, cat, draft)
    do_count(db_session, cat, untracked(cream[0], "0"), counted_at=nov(12))

    receive_against(db_session, cat, sent, milk(cat, "6"), received_at=nov(11))

    db_session.refresh(sent)
    assert sent.status == "received"
    assert_in_line(db_session, cat)


# --- Locking --------------------------------------------------------------------


def test_every_order_action_locks_the_business_row_first(
    db_session: Session, cat: Catalogue, draft: PurchaseOrder
):
    """D72: with the business row held exclusively, no stock write is in
    progress, so the action needs no item locks."""
    business_id = cat.business.id
    line_id = order_lines(db_session, draft)[cat.item.id].id
    actions = {
        "set a quantity": lambda: set_line_quantity(
            db_session, business_id, order_id=draft.id, line_id=line_id, qty_packs=5
        ),
        "send": lambda: send(db_session, cat, draft),
        "cancel": lambda: cancel_order(
            db_session, business_id, order_id=draft.id, cancelled_at=nov(11)
        ),
    }
    for name, action in actions.items():
        with recorded_statements(db_session) as statements:
            action()
        assert "FROM businesses" in statements[0] and "FOR NO KEY UPDATE" in statements[0], name


def test_pack_counts_are_stored_as_integers(db_session: Session, cat: Catalogue):
    """Pack counts are integers end to end (D70), unlike receipt lines."""
    add(db_session, new_par_level(cat.location, cat.item, order_up_to=d("2500.5")))
    sweep(db_session, cat.business.id, nov(9))

    stored = db_session.scalars(select(PurchaseOrderLine)).one()

    assert (stored.suggested_packs, type(stored.suggested_packs)) == (3, int)
