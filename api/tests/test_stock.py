"""Stock domain functions: worked examples of receive, use, waste, reverse and rebuild.

Each test runs in the rolled-back transaction from conftest.py. The cat fixture's
item is whole milk, which tracks batches and expiry, bought by the 1000 ml
bottle; the business is in Asia/Amman. Tests that write stock end by checking
that the balances still match the ledger (domain rule 1).
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.domain.errors import DomainError
from app.domain.stock import (
    BalanceDifference,
    ReceiptLineInput,
    ReceiveResult,
    UseResult,
    rebuild_balances,
    receive,
    reverse,
    use,
    waste,
)
from app.models import Batch, Item, ItemUnit, ReceiptLine, StockBalance, StockMovement
from tests.builders import RECEIVED_AT, Catalogue, add, new_item, new_unit


def d(value: str) -> Decimal:
    return Decimal(value)


def nov(day: int, hour: int = 12, minute: int = 0) -> datetime:
    """A time in November 2026, in UTC. Amman is UTC+3."""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


NOV_10_NOON = nov(10)


# --- Fixtures and helpers -------------------------------------------------------


@pytest.fixture
def flour(db_session: Session, cat: Catalogue) -> tuple[Item, ItemUnit]:
    """An item without batch tracking, bought by the 25 kg sack."""
    item = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    return item, add(db_session, new_unit(item, name="sack", factor_to_base=d("25000")))


@pytest.fixture
def eggs(db_session: Session, cat: Catalogue) -> tuple[Item, ItemUnit]:
    """An item that tracks batches but not expiry, bought by the tray of 30."""
    item = add(
        db_session, new_item(cat.business.id, name="Eggs", base_unit="piece", tracks_batches=True)
    )
    return item, add(db_session, new_unit(item, name="tray", factor_to_base=d("30")))


def line(
    item: Item,
    unit: ItemUnit,
    packs: str,
    price: str,
    expires_on: date | None = None,
    lot_code: str | None = None,
) -> ReceiptLineInput:
    return ReceiptLineInput(item.id, unit.id, d(packs), d(price), expires_on, lot_code)


def milk(cat: Catalogue, packs: str, price: str = "0.850", expires_day: int = 20):
    """A receipt line for bottles of the cat fixture's milk, expiring that day in November."""
    return line(cat.item, cat.unit, packs, price, date(2026, 11, expires_day))


def do_receive(
    db: Session,
    cat: Catalogue,
    *lines: ReceiptLineInput,
    received_at: datetime = RECEIVED_AT,
    invoice_ref: str | None = None,
    client_id: UUID | None = None,
) -> ReceiveResult:
    return receive(
        db,
        cat.business.id,
        location_id=cat.location.id,
        supplier_id=cat.supplier.id,
        received_at=received_at,
        invoice_ref=invoice_ref,
        lines=lines,
        client_id=client_id or uuid.uuid4(),
        source="manual",
        user_id=cat.user.id,
    )


def do_use(
    db: Session,
    cat: Catalogue,
    item: Item,
    quantity: str,
    occurred_at: datetime = NOV_10_NOON,
    client_id: UUID | None = None,
) -> UseResult:
    return use(
        db,
        cat.business.id,
        location_id=cat.location.id,
        item_id=item.id,
        quantity=d(quantity),
        occurred_at=occurred_at,
        client_id=client_id or uuid.uuid4(),
        source="simulator",
    )


def do_waste(
    db: Session,
    cat: Catalogue,
    item: Item,
    batch_id: UUID | None,
    quantity: str,
    reason: str = "spoiled",
    occurred_at: datetime = NOV_10_NOON,
):
    return waste(
        db,
        cat.business.id,
        location_id=cat.location.id,
        item_id=item.id,
        batch_id=batch_id,
        quantity=d(quantity),
        reason=reason,
        occurred_at=occurred_at,
        client_id=uuid.uuid4(),
        source="manual",
    )


def do_reverse(
    db: Session, cat: Catalogue, original_client_id: UUID, client_id: UUID | None = None
):
    return reverse(
        db,
        cat.business.id,
        original_client_id=original_client_id,
        client_id=client_id or uuid.uuid4(),
        source="manual",
    )


def on_hand(db: Session, batch_id: UUID) -> Decimal:
    return db.scalar(select(StockBalance.qty_on_hand).where(StockBalance.batch_id == batch_id))


def batch(db: Session, batch_id: UUID) -> Batch:
    """The batch as stored now, not as this session last saw it."""
    return db.get(Batch, batch_id, populate_existing=True)


def default_batch(db: Session, cat: Catalogue, item: Item) -> Batch:
    return db.scalars(
        select(Batch)
        .where(Batch.location_id == cat.location.id, Batch.item_id == item.id, Batch.is_default)
        .execution_options(populate_existing=True)
    ).one()


def movement_count(db: Session, cat: Catalogue) -> int:
    return db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(StockMovement.business_id == cat.business.id)
    )


def summary(movements) -> list[tuple[str, Decimal, Decimal | None]]:
    return [(m.type, m.qty_delta, m.unit_cost) for m in movements]


def assert_reconciled(db: Session, cat: Catalogue) -> None:
    assert rebuild_balances(db, cat.business.id) == []


# --- Receive --------------------------------------------------------------------


def test_a_tracked_receipt_creates_a_batch_per_line(db_session: Session, cat: Catalogue) -> None:
    result = do_receive(
        db_session,
        cat,
        line(cat.item, cat.unit, "2", "0.850", date(2026, 11, 9), " LOT-A "),
        line(cat.item, cat.unit, "1", "0.900", date(2026, 11, 12)),
        invoice_ref="  INV-7 ",
    )

    assert result.receipt.invoice_ref == "INV-7"  # trimmed
    first, second = result.movements
    assert summary(result.movements) == [
        ("receive", d("2000"), d("0.000850")),
        ("receive", d("1000"), d("0.000900")),
    ]
    assert [m.client_seq for m in result.movements] == [0, 1]
    assert all(m.occurred_at == RECEIVED_AT for m in result.movements)

    a, b = batch(db_session, first.batch_id), batch(db_session, second.batch_id)
    assert (a.expires_on, a.lot_code, a.unit_cost) == (date(2026, 11, 9), "LOT-A", d("0.000850"))
    assert (b.expires_on, b.lot_code, b.unit_cost) == (date(2026, 11, 12), None, d("0.000900"))
    assert a.received_at == b.received_at == RECEIVED_AT
    assert not a.is_default and not b.is_default
    assert (on_hand(db_session, a.id), on_hand(db_session, b.id)) == (d("2000"), d("1000"))

    receipt_line = db_session.get(ReceiptLine, first.receipt_line_id)
    assert (receipt_line.qty_base, receipt_line.batch_id) == (d("2000"), a.id)
    # The receipt also created milk's default batch: it is the item's lock.
    assert on_hand(db_session, default_batch(db_session, cat, cat.item).id) == 0
    assert_reconciled(db_session, cat)


def test_an_untracked_receipt_goes_into_the_default_batch_at_weighted_average_cost(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, sack = flour

    first = do_receive(db_session, cat, line(item, sack, "1", "12.500"))
    default = default_batch(db_session, cat, item)
    assert first.movements[0].batch_id == default.id
    assert default.unit_cost == d("0.000500")

    do_receive(db_session, cat, line(item, sack, "3", "15.000"))

    # (25000 × 0.000500 + 75000 × 0.000600) / 100000
    assert default_batch(db_session, cat, item).unit_cost == d("0.000575")
    assert on_hand(db_session, default.id) == d("100000")
    assert_reconciled(db_session, cat)


def test_a_receipt_into_a_negative_default_batch_takes_the_line_cost(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, sack = flour
    do_receive(db_session, cat, line(item, sack, "1", "12.500"))
    do_use(db_session, cat, item, "30000")  # 5000 g short

    do_receive(db_session, cat, line(item, sack, "1", "20.000"))

    default = default_batch(db_session, cat, item)
    assert default.unit_cost == d("0.000800")  # nothing on hand to average with
    assert on_hand(db_session, default.id) == d("20000")
    assert_reconciled(db_session, cat)


def test_a_receipt_settles_a_negative_default_batch_with_a_settle_pair(
    db_session: Session, cat: Catalogue
) -> None:
    shortfall = do_use(db_session, cat, cat.item, "1500")
    default = default_batch(db_session, cat, cat.item)
    assert shortfall.shortfall == d("1500")
    assert on_hand(db_session, default.id) == d("-1500")

    result = do_receive(db_session, cat, milk(cat, "1", "0.850"), milk(cat, "2", "0.900"))

    first, settle_out, settle_in, second, settle_out_2, settle_in_2 = result.movements
    assert summary(result.movements) == [
        ("receive", d("1000"), d("0.000850")),
        ("settle", d("-1000"), d("0.000850")),
        ("settle", d("1000"), d("0.000850")),
        ("receive", d("2000"), d("0.000900")),
        ("settle", d("-500"), d("0.000900")),
        ("settle", d("500"), d("0.000900")),
    ]
    assert [m.client_seq for m in result.movements] == [0, 1, 2, 3, 4, 5]
    assert settle_out.batch_id == first.batch_id and settle_in.batch_id == default.id
    assert settle_out_2.batch_id == second.batch_id and settle_in_2.batch_id == default.id

    assert on_hand(db_session, default.id) == 0
    assert on_hand(db_session, first.batch_id) == 0
    assert on_hand(db_session, second.batch_id) == d("1500")
    # The default batch was negative at each settle, so it took that batch's cost.
    assert default_batch(db_session, cat, cat.item).unit_cost == d("0.000900")
    assert_reconciled(db_session, cat)


def test_unit_cost_is_rounded_half_up_to_six_decimals(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, _ = flour
    bag = add(db_session, new_unit(item, name="bag", factor_to_base=d("2000")))

    # 0.001 / 2000 = 0.0000005: half up gives 0.000001, half even would give 0.
    result = do_receive(db_session, cat, line(item, bag, "1", "0.001"))

    assert result.movements[0].unit_cost == d("0.000001")


# --- Use ------------------------------------------------------------------------


def test_a_usage_splits_across_batches_in_fefo_order(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(
        db_session,
        cat,
        milk(cat, "1", "0.800", expires_day=15),
        milk(cat, "1", "0.850", expires_day=12),
        milk(cat, "1", "0.900", expires_day=20),
    )
    on_15, on_12, on_20 = (m.batch_id for m in received.movements)

    result = do_use(db_session, cat, cat.item, "2500")

    assert [(m.batch_id, m.qty_delta, m.unit_cost) for m in result.movements] == [
        (on_12, d("-1000"), d("0.000850")),
        (on_15, d("-1000"), d("0.000800")),
        (on_20, d("-500"), d("0.000900")),
    ]
    assert [m.client_seq for m in result.movements] == [0, 1, 2]
    assert result.shortfall == 0
    assert on_hand(db_session, on_20) == d("500")
    assert_reconciled(db_session, cat)


def test_usage_dates_are_local_to_the_business(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(
        db_session, cat, milk(cat, "1", expires_day=9), milk(cat, "1", expires_day=11)
    )
    on_9, on_11 = (m.batch_id for m in received.movements)

    # 23:30 UTC on 9 Nov is 02:30 on 10 Nov in Amman, so the 9 Nov batch has expired.
    late = do_use(db_session, cat, cat.item, "100", occurred_at=nov(9, 23, 30))
    # 20:30 UTC on 9 Nov is 23:30 on 9 Nov in Amman: its last usable day.
    early = do_use(db_session, cat, cat.item, "100", occurred_at=nov(9, 20, 30))

    assert [m.batch_id for m in late.movements] == [on_11]
    assert [m.batch_id for m in early.movements] == [on_9]
    assert_reconciled(db_session, cat)


def test_a_shortfall_takes_the_default_batch_negative(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"))

    result = do_use(db_session, cat, cat.item, "1500")

    default = default_batch(db_session, cat, cat.item)
    assert [(m.batch_id, m.qty_delta) for m in result.movements] == [
        (received.movements[0].batch_id, d("-1000")),
        (default.id, d("-500")),
    ]
    # The default batch has never received anything, so it has no cost yet.
    assert result.movements[1].unit_cost is None
    assert result.shortfall == d("500")
    assert on_hand(db_session, default.id) == d("-500")
    assert_reconciled(db_session, cat)


# --- Waste ----------------------------------------------------------------------


def test_waste_beyond_on_hand_is_refused(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"))
    before = movement_count(db_session, cat)

    with pytest.raises(DomainError, match="1000.000 on hand"):
        do_waste(db_session, cat, cat.item, received.movements[0].batch_id, "1000.001")

    assert movement_count(db_session, cat) == before


def test_an_expired_batch_can_be_wasted(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(db_session, cat, milk(cat, "1", "0.850", expires_day=5))
    batch_id = received.movements[0].batch_id

    result = do_waste(db_session, cat, cat.item, batch_id, "1000", reason="expired")

    (movement,) = result.movements
    assert (movement.type, movement.qty_delta, movement.reason) == ("waste", d("-1000"), "expired")
    assert movement.unit_cost == d("0.000850")
    assert on_hand(db_session, batch_id) == 0
    assert_reconciled(db_session, cat)


def test_an_untracked_item_wastes_from_its_default_batch(
    db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
) -> None:
    item, sack = flour
    do_receive(db_session, cat, line(item, sack, "1", "12.500"))

    result = do_waste(db_session, cat, item, None, "500", reason="damaged")

    default = default_batch(db_session, cat, item)
    assert [(m.batch_id, m.unit_cost) for m in result.movements] == [(default.id, d("0.000500"))]
    assert on_hand(db_session, default.id) == d("24500")
    assert_reconciled(db_session, cat)


def test_waste_must_name_a_batch_of_a_tracked_item(db_session: Session, cat: Catalogue) -> None:
    do_receive(db_session, cat, milk(cat, "1"))

    with pytest.raises(DomainError, match="name the batch"):
        do_waste(db_session, cat, cat.item, None, "100")


def test_waste_of_another_items_batch_is_not_found(
    db_session: Session, cat: Catalogue, eggs: tuple[Item, ItemUnit]
) -> None:
    item, tray = eggs
    received = do_receive(db_session, cat, milk(cat, "1"), line(item, tray, "1", "3.000"))

    with pytest.raises(DomainError, match="not found"):
        do_waste(db_session, cat, item, received.movements[0].batch_id, "1")


# --- Reverse --------------------------------------------------------------------


def test_reversing_a_split_usage_restores_both_batches(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(
        db_session, cat, milk(cat, "1", "0.850", expires_day=12), milk(cat, "1", "0.900")
    )
    first, second = (m.batch_id for m in received.movements)
    usage = do_use(db_session, cat, cat.item, "1500", occurred_at=nov(10, 9))

    result = do_reverse(db_session, cat, usage.movements[0].client_id)

    assert [
        (m.type, m.batch_id, m.qty_delta, m.unit_cost, m.occurred_at, m.reverses_id)
        for m in result.movements
    ] == [
        ("reversal", first, d("1000"), d("0.000850"), nov(10, 9), usage.movements[0].id),
        ("reversal", second, d("500"), d("0.000900"), nov(10, 9), usage.movements[1].id),
    ]
    assert [m.client_seq for m in result.movements] == [0, 1]
    assert (on_hand(db_session, first), on_hand(db_session, second)) == (d("1000"), d("1000"))
    assert_reconciled(db_session, cat)


def test_reversing_a_waste_restores_the_batch(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"))
    batch_id = received.movements[0].batch_id
    wasted = do_waste(db_session, cat, cat.item, batch_id, "250")

    result = do_reverse(db_session, cat, wasted.movements[0].client_id)

    # Only waste rows carry a reason.
    assert [(m.qty_delta, m.reason) for m in result.movements] == [(d("250"), None)]
    assert on_hand(db_session, batch_id) == d("1000")
    assert_reconciled(db_session, cat)


def test_a_request_can_be_reversed_only_once(db_session: Session, cat: Catalogue) -> None:
    do_receive(db_session, cat, milk(cat, "1"))
    usage = do_use(db_session, cat, cat.item, "100")
    do_reverse(db_session, cat, usage.movements[0].client_id)

    with pytest.raises(DomainError, match="already reversed"):
        do_reverse(db_session, cat, usage.movements[0].client_id)


def test_a_receipt_cannot_be_reversed(db_session: Session, cat: Catalogue) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"))

    with pytest.raises(DomainError, match="only usage and waste can be reversed, not receive"):
        do_reverse(db_session, cat, received.movements[0].client_id)


def test_a_reversal_cannot_be_reversed(db_session: Session, cat: Catalogue) -> None:
    do_receive(db_session, cat, milk(cat, "1"))
    usage = do_use(db_session, cat, cat.item, "100")
    reversal = do_reverse(db_session, cat, usage.movements[0].client_id)

    with pytest.raises(DomainError, match="not reversal"):
        do_reverse(db_session, cat, reversal.movements[0].client_id)


def test_reversing_an_unknown_request_is_refused(db_session: Session, cat: Catalogue) -> None:
    with pytest.raises(DomainError, match="no request"):
        do_reverse(db_session, cat, uuid.uuid4())


# --- Idempotency ----------------------------------------------------------------


def test_a_retried_receive_writes_nothing_new(db_session: Session, cat: Catalogue) -> None:
    client_id = uuid.uuid4()
    first = do_receive(db_session, cat, milk(cat, "1"), client_id=client_id, invoice_ref="INV-1")
    before = movement_count(db_session, cat)

    retry = do_receive(db_session, cat, milk(cat, "1"), client_id=client_id, invoice_ref="INV-1")

    assert retry.replayed and not first.replayed
    assert retry.receipt.id == first.receipt.id
    assert [m.id for m in retry.movements] == [m.id for m in first.movements]
    assert movement_count(db_session, cat) == before


def test_a_retried_use_writes_nothing_new(db_session: Session, cat: Catalogue) -> None:
    do_receive(db_session, cat, milk(cat, "1"))
    client_id = uuid.uuid4()
    first = do_use(db_session, cat, cat.item, "1500", client_id=client_id)
    before = movement_count(db_session, cat)

    retry = do_use(db_session, cat, cat.item, "1500", client_id=client_id)

    assert retry.replayed
    assert [m.id for m in retry.movements] == [m.id for m in first.movements]
    assert retry.shortfall is None  # a replay doesn't recompute it
    assert movement_count(db_session, cat) == before
    assert_reconciled(db_session, cat)


def test_a_retried_reversal_returns_what_it_wrote(db_session: Session, cat: Catalogue) -> None:
    do_receive(db_session, cat, milk(cat, "1"))
    usage = do_use(db_session, cat, cat.item, "100")
    client_id = uuid.uuid4()
    first = do_reverse(db_session, cat, usage.movements[0].client_id, client_id=client_id)

    retry = do_reverse(db_session, cat, usage.movements[0].client_id, client_id=client_id)

    assert retry.replayed
    assert [m.id for m in retry.movements] == [m.id for m in first.movements]


def test_a_client_id_cant_be_reused_for_a_different_request(
    db_session: Session, cat: Catalogue
) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"))

    with pytest.raises(DomainError, match="already used for a different request"):
        do_use(db_session, cat, cat.item, "100", client_id=received.movements[0].client_id)


# --- Tenancy --------------------------------------------------------------------


def test_another_businesss_rows_are_not_found(
    db_session: Session, cat: Catalogue, other: Catalogue
) -> None:
    theirs = do_receive(db_session, other, milk(other, "1"))
    their_usage = do_use(db_session, other, other.item, "100")
    do_receive(db_session, cat, milk(cat, "1"))

    attempts = {
        "location": lambda: receive(
            db_session,
            cat.business.id,
            location_id=other.location.id,
            supplier_id=cat.supplier.id,
            received_at=RECEIVED_AT,
            invoice_ref=None,
            lines=[milk(cat, "1")],
            client_id=uuid.uuid4(),
            source="manual",
        ),
        "supplier": lambda: receive(
            db_session,
            cat.business.id,
            location_id=cat.location.id,
            supplier_id=other.supplier.id,
            received_at=RECEIVED_AT,
            invoice_ref=None,
            lines=[milk(cat, "1")],
            client_id=uuid.uuid4(),
            source="manual",
        ),
        "item": lambda: do_use(db_session, cat, other.item, "100"),
        "batch": lambda: do_waste(db_session, cat, cat.item, theirs.movements[0].batch_id, "1"),
        "no request": lambda: do_reverse(db_session, cat, their_usage.movements[0].client_id),
        "user": lambda: use(
            db_session,
            cat.business.id,
            location_id=cat.location.id,
            item_id=cat.item.id,
            quantity=d("1"),
            occurred_at=nov(10),
            client_id=uuid.uuid4(),
            source="manual",
            user_id=other.user.id,
        ),
    }
    for label, attempt in attempts.items():
        with pytest.raises(DomainError, match=label):
            attempt()

    # Their usage's client_id is free in this business: idempotency is per business.
    mine = do_use(db_session, cat, cat.item, "100", client_id=their_usage.movements[0].client_id)
    assert not mine.replayed


# --- Validation -----------------------------------------------------------------


def test_receive_validates_its_lines(
    db_session: Session,
    cat: Catalogue,
    flour: tuple[Item, ItemUnit],
    eggs: tuple[Item, ItemUnit],
) -> None:
    flour_item, sack = flour
    egg_item, tray = eggs
    half = add(db_session, new_unit(egg_item, name="half", factor_to_base=d("0.5")))
    refused = {
        "qty_packs must have at most three decimal places": line(egg_item, tray, "1.0001", "3.000"),
        r"qty_base \(qty_packs": line(egg_item, half, "0.001", "3.000"),
        "pack_price must not be negative": line(egg_item, tray, "1", "-1"),
        "expires_on is required": line(cat.item, cat.unit, "1", "0.850"),
        "expires_on must be empty": line(egg_item, tray, "1", "3.000", date(2026, 11, 20)),
        "no expiry date or lot code": line(flour_item, sack, "1", "12.500", lot_code="L1"),
        "unit .* not found for item": line(flour_item, tray, "1", "12.500"),
    }
    for message, bad_line in refused.items():
        with pytest.raises(DomainError, match=message):
            do_receive(db_session, cat, bad_line)

    with pytest.raises(DomainError, match="at least one line"):
        do_receive(db_session, cat)
    with pytest.raises(DomainError, match="timezone"):
        do_receive(db_session, cat, milk(cat, "1"), received_at=datetime(2026, 11, 2, 7, 30))
    assert movement_count(db_session, cat) == 0


def test_a_blank_lot_code_becomes_null(db_session: Session, cat: Catalogue) -> None:
    result = do_receive(
        db_session, cat, line(cat.item, cat.unit, "1", "0.850", date(2026, 11, 20), "   ")
    )

    assert batch(db_session, result.movements[0].batch_id).lot_code is None


def test_an_invoice_cant_be_received_twice(db_session: Session, cat: Catalogue) -> None:
    do_receive(db_session, cat, milk(cat, "1"), invoice_ref="INV-9")

    with pytest.raises(DomainError, match="INV-9 from this supplier was already received"):
        do_receive(db_session, cat, milk(cat, "1"), invoice_ref=" INV-9 ")


def test_requests_validate_quantity_source_and_reason(db_session: Session, cat: Catalogue) -> None:
    with pytest.raises(DomainError, match="three decimal places"):
        do_use(db_session, cat, cat.item, "0.0001")
    with pytest.raises(TypeError, match="Decimal"):
        use(
            db_session,
            cat.business.id,
            location_id=cat.location.id,
            item_id=cat.item.id,
            quantity=1.5,
            occurred_at=nov(10),
            client_id=uuid.uuid4(),
            source="manual",
        )
    with pytest.raises(DomainError, match="source must be one of"):
        use(
            db_session,
            cat.business.id,
            location_id=cat.location.id,
            item_id=cat.item.id,
            quantity=d("1"),
            occurred_at=nov(10),
            client_id=uuid.uuid4(),
            source="pos",
        )
    with pytest.raises(DomainError, match="reason must be one of"):
        do_waste(db_session, cat, cat.item, None, "1", reason="lost")


# --- Rebuild --------------------------------------------------------------------


def test_rebuild_finds_and_fixes_drifted_and_missing_balances(
    db_session: Session, cat: Catalogue
) -> None:
    received = do_receive(db_session, cat, milk(cat, "1"), milk(cat, "1", expires_day=25))
    drifted, missing = (m.batch_id for m in received.movements)
    do_use(db_session, cat, cat.item, "400", occurred_at=nov(10))
    db_session.execute(
        update(StockBalance).where(StockBalance.batch_id == drifted).values(qty_on_hand=d("7"))
    )
    # stock_balances is a projection, so deleting a row only loses derived data.
    db_session.execute(delete(StockBalance).where(StockBalance.batch_id == missing))

    differences = rebuild_balances(db_session, cat.business.id, fix=True)

    assert sorted(differences, key=lambda x: x.batch_id) == sorted(
        [
            BalanceDifference(drifted, d("600"), d("7"), nov(10), nov(10)),
            BalanceDifference(missing, d("1000"), None, RECEIVED_AT, None),
        ],
        key=lambda x: x.batch_id,
    )
    assert on_hand(db_session, drifted) == d("600")
    assert on_hand(db_session, missing) == d("1000")
    assert_reconciled(db_session, cat)
