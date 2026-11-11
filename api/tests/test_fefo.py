"""FEFO allocation (domain rules 2 to 5). Pure tests: no database.

The worked examples read as documentation of the rules; the Hypothesis
properties check them over generated batches.
"""

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.fefo import Allocation, BatchSnapshot, FefoResult, allocate_usage

AS_OF = date(2026, 11, 10)
MONDAY_8AM = datetime(2026, 11, 2, 8, tzinfo=UTC)
# The usage's occurred_at, after every received_at below unless a test says
# otherwise, so only the tests about D50 depend on it.
USED_AT = datetime(2026, 11, 10, 12, tzinfo=UTC)

DEFAULT_ID = UUID(int=0)
A = UUID(int=1)
B = UUID(int=2)
C = UUID(int=3)


def d(value: str) -> Decimal:
    return Decimal(value)


def days(n: int) -> date:
    """as_of shifted by n days."""
    return AS_OF + timedelta(days=n)


def batch(
    id: UUID,
    on_hand: str,
    expires_on: date | None = None,
    received_at: datetime = MONDAY_8AM,
) -> BatchSnapshot:
    return BatchSnapshot(id, expires_on, received_at, d(on_hand), is_default=False)


def default_batch(on_hand: str = "0", received_at: datetime = MONDAY_8AM) -> BatchSnapshot:
    return BatchSnapshot(DEFAULT_ID, None, received_at, d(on_hand), is_default=True)


def allocations(*pairs: tuple[UUID, str]) -> tuple[Allocation, ...]:
    return tuple(Allocation(batch_id, d(quantity)) for batch_id, quantity in pairs)


# Worked examples


def test_earliest_expiry_is_drawn_first() -> None:
    batches = [batch(A, "10", days(5)), batch(B, "10", days(2)), default_batch()]

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result == FefoResult(allocations((B, "4")), shortfall=d("0"))


def test_a_batch_with_no_expiry_comes_after_dated_batches() -> None:
    batches = [batch(A, "10", None), batch(B, "10", days(30)), default_batch()]

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result.allocations == allocations((B, "4"))


def test_same_expiry_is_broken_by_received_at() -> None:
    batches = [
        batch(A, "10", days(3), received_at=MONDAY_8AM + timedelta(days=1)),
        batch(B, "10", days(3), received_at=MONDAY_8AM),
        default_batch(),
    ]

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result.allocations == allocations((B, "4"))


def test_same_expiry_and_received_at_is_broken_by_id() -> None:
    batches = [batch(B, "10", days(3)), batch(A, "10", days(3)), default_batch()]

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result.allocations == allocations((A, "4"))


def test_expired_batch_is_skipped_but_one_expiring_today_is_used() -> None:
    batches = [
        batch(A, "10", days(-1)),  # expired yesterday: skipped
        batch(B, "10", days(0)),  # expires today: still usable
        default_batch(),
    ]

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result.allocations == allocations((B, "4"))


def test_empty_and_negative_batches_are_skipped() -> None:
    batches = [batch(A, "0", days(1)), batch(B, "-2", days(2)), batch(C, "10", days(3))]
    batches.append(default_batch())

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result.allocations == allocations((C, "4"))


def test_usage_splits_across_two_batches_in_fefo_order() -> None:
    batches = [batch(A, "3.250", days(1)), batch(B, "10", days(2)), default_batch()]

    result = allocate_usage(batches, d("5"), AS_OF, USED_AT)

    assert result == FefoResult(allocations((A, "3.250"), (B, "1.750")), shortfall=d("0"))


def test_shortfall_goes_to_default_batch_that_starts_positive() -> None:
    """The default batch holds 1 of the 3 missing, so 2 is a shortfall."""
    batches = [batch(A, "2", days(1)), default_batch("1")]

    result = allocate_usage(batches, d("5"), AS_OF, USED_AT)

    assert result == FefoResult(allocations((A, "2"), (DEFAULT_ID, "3")), shortfall=d("2"))


def test_shortfall_goes_to_default_batch_that_is_already_negative() -> None:
    """A default batch at -4 holds nothing, so all 3 missing are a shortfall."""
    batches = [batch(A, "2", days(1)), default_batch("-4")]

    result = allocate_usage(batches, d("5"), AS_OF, USED_AT)

    assert result == FefoResult(allocations((A, "2"), (DEFAULT_ID, "3")), shortfall=d("3"))


def test_default_batch_comes_last_even_if_received_first() -> None:
    batches = [
        batch(A, "10", None, received_at=MONDAY_8AM + timedelta(days=3)),
        default_batch("10", received_at=MONDAY_8AM),
    ]

    result = allocate_usage(batches, d("4"), AS_OF, USED_AT)

    assert result.allocations == allocations((A, "4"))


def test_item_without_batch_tracking_uses_only_its_default_batch() -> None:
    result = allocate_usage([default_batch("500")], d("30"), AS_OF, USED_AT)

    assert result == FefoResult(allocations((DEFAULT_ID, "30")), shortfall=d("0"))


def test_a_batch_received_after_the_usage_is_skipped() -> None:
    # A usage logged for 07:59 can't draw from a delivery received at 08:00,
    # so it falls to the default batch as a shortfall although A holds stock.
    batches = [batch(A, "10", days(2), received_at=MONDAY_8AM), default_batch("0")]

    result = allocate_usage(batches, d("4"), AS_OF, MONDAY_8AM - timedelta(minutes=1))

    assert result == FefoResult(allocations((DEFAULT_ID, "4")), shortfall=d("4"))


def test_a_batch_received_at_the_usage_time_is_drawn() -> None:
    batches = [batch(A, "10", days(2), received_at=MONDAY_8AM), default_batch("0")]

    result = allocate_usage(batches, d("4"), AS_OF, MONDAY_8AM)

    assert result == FefoResult(allocations((A, "4")), shortfall=d("0"))


def test_the_default_batch_is_drawn_even_if_created_after_the_usage() -> None:
    # A default batch's received_at is when it was created (the real clock),
    # so it is always available.
    batches = [default_batch("5", received_at=MONDAY_8AM + timedelta(days=30))]

    result = allocate_usage(batches, d("4"), AS_OF, MONDAY_8AM)

    assert result == FefoResult(allocations((DEFAULT_ID, "4")), shortfall=d("0"))


# Invalid input


def test_no_default_batch_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly one default batch"):
        allocate_usage([batch(A, "10")], d("1"), AS_OF, USED_AT)


def test_two_default_batches_are_rejected() -> None:
    second = replace(default_batch(), id=A)
    with pytest.raises(ValueError, match="exactly one default batch"):
        allocate_usage([default_batch(), second], d("1"), AS_OF, USED_AT)


@pytest.mark.parametrize("quantity", ["0", "0.000", "-1"])
def test_zero_or_negative_quantity_is_rejected(quantity: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        allocate_usage([default_batch()], d(quantity), AS_OF, USED_AT)


@pytest.mark.parametrize("quantity", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_quantity_is_rejected(quantity: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        allocate_usage([default_batch()], d(quantity), AS_OF, USED_AT)


@pytest.mark.parametrize("quantity", ["0.0005", "2.0015", "1E+30"])
def test_quantity_with_more_than_three_decimals_is_rejected(quantity: str) -> None:
    """1E+30 can't be quantized to 0.001 within Decimal's 28 digits."""
    with pytest.raises(ValueError, match="three decimal places"):
        allocate_usage([default_batch()], d(quantity), AS_OF, USED_AT)


def test_trailing_zeros_beyond_three_decimals_are_accepted() -> None:
    result = allocate_usage([default_batch("5")], d("1.5000"), AS_OF, USED_AT)

    assert result.allocations == allocations((DEFAULT_ID, "1.5"))


def test_float_quantity_is_rejected() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        allocate_usage([default_batch()], 1.5, AS_OF, USED_AT)  # type: ignore[arg-type]


# Properties


def three_dp(low: str, high: str) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=low, max_value=high, places=3)


# Dates around as_of, so some batches are expired, some expire on as_of and
# many share a date. Few distinct received_at values, so ties are common.
expiry_dates = st.one_of(st.none(), st.integers(-3, 3).map(days))
received_times = st.sampled_from([MONDAY_8AM + timedelta(hours=h) for h in range(3)])
on_hands = st.one_of(st.just(d("0.000")), three_dp("-50", "100"))
quantities = three_dp("0.001", "300")


@st.composite
def item_batches(draw: st.DrawFn) -> list[BatchSnapshot]:
    """One item's batches at one location: up to 7 batches plus the default
    batch, which sits anywhere in the list and may be negative."""
    default_id, *ids = draw(st.lists(st.uuids(), min_size=1, max_size=8, unique=True))
    batches = [
        BatchSnapshot(i, draw(expiry_dates), draw(received_times), draw(on_hands), False)
        for i in ids
    ]
    default = BatchSnapshot(default_id, None, draw(received_times), draw(on_hands), True)
    batches.insert(draw(st.integers(0, len(batches))), default)
    return batches


# A reference model of the rules, written from the spec rather than the code.


def is_usable(b: BatchSnapshot) -> bool:
    return not b.is_default and b.on_hand > 0 and (b.expires_on is None or b.expires_on >= AS_OF)


def fefo_order(batches: list[BatchSnapshot]) -> list[BatchSnapshot]:
    return sorted(
        filter(is_usable, batches),
        key=lambda b: (b.expires_on is None, b.expires_on or date.max, b.received_at, b.id),
    )


def the_default(batches: list[BatchSnapshot]) -> BatchSnapshot:
    return next(b for b in batches if b.is_default)


def taken(result: FefoResult) -> dict[UUID, Decimal]:
    return {a.batch_id: a.quantity for a in result.allocations}


@given(item_batches(), quantities)
def test_allocations_sum_exactly_to_quantity(batches: list[BatchSnapshot], qty: Decimal) -> None:
    result = allocate_usage(batches, qty, AS_OF, USED_AT)

    assert sum(a.quantity for a in result.allocations) == qty


@given(item_batches(), quantities)
def test_allocations_are_positive_known_and_unique(
    batches: list[BatchSnapshot], qty: Decimal
) -> None:
    result = allocate_usage(batches, qty, AS_OF, USED_AT)
    ids = [a.batch_id for a in result.allocations]

    assert all(a.quantity > 0 for a in result.allocations)
    assert set(ids) <= {b.id for b in batches}
    assert len(ids) == len(set(ids))


@given(item_batches(), quantities)
def test_batches_give_at_most_their_stock_and_unusable_ones_give_nothing(
    batches: list[BatchSnapshot], qty: Decimal
) -> None:
    took = taken(allocate_usage(batches, qty, AS_OF, USED_AT))

    for b in batches:
        if b.is_default:
            continue
        if is_usable(b):
            assert took.get(b.id, 0) <= b.on_hand
        else:
            assert b.id not in took


@given(item_batches(), quantities)
def test_fefo_every_batch_before_the_last_one_drawn_is_used_up(
    batches: list[BatchSnapshot], qty: Decimal
) -> None:
    result = allocate_usage(batches, qty, AS_OF, USED_AT)
    order = fefo_order(batches)
    drawn = [a.batch_id for a in result.allocations if a.batch_id != the_default(batches).id]

    # The non-default allocations are a prefix of the FEFO order, in that order...
    assert drawn == [b.id for b in order[: len(drawn)]]
    # ...and every batch before the last one drawn gave all its stock.
    took = taken(result)
    for b in order[: max(len(drawn) - 1, 0)]:
        assert took[b.id] == b.on_hand


@given(item_batches(), quantities)
def test_default_batch_is_drawn_last_and_only_for_a_shortfall(
    batches: list[BatchSnapshot], qty: Decimal
) -> None:
    result = allocate_usage(batches, qty, AS_OF, USED_AT)
    default = the_default(batches)
    usable_stock = sum((b.on_hand for b in fefo_order(batches)), Decimal(0))
    took = taken(result)

    if qty <= usable_stock:
        assert default.id not in took
        assert result.shortfall == 0
    else:
        remainder = qty - usable_stock
        assert result.allocations[-1] == Allocation(default.id, remainder)
        assert result.shortfall == max(Decimal(0), remainder - max(Decimal(0), default.on_hand))


@given(item_batches(), quantities, st.randoms(use_true_random=False))
def test_input_order_does_not_matter_and_input_is_not_modified(
    batches: list[BatchSnapshot], qty: Decimal, rng: random.Random
) -> None:
    before = list(batches)
    result = allocate_usage(batches, qty, AS_OF, USED_AT)
    assert batches == before

    shuffled = list(batches)
    rng.shuffle(shuffled)
    assert allocate_usage(shuffled, qty, AS_OF, USED_AT) == result


def apply(batches: list[BatchSnapshot], result: FefoResult) -> list[BatchSnapshot]:
    """The snapshots after the usage's movements are posted."""
    took = taken(result)
    return [replace(b, on_hand=b.on_hand - took.get(b.id, 0)) for b in batches]


@given(item_batches(), quantities, quantities)
def test_two_draws_take_the_same_from_each_batch_as_one(
    batches: list[BatchSnapshot], qty_a: Decimal, qty_b: Decimal
) -> None:
    first = allocate_usage(batches, qty_a, AS_OF, USED_AT)
    second = allocate_usage(apply(batches, first), qty_b, AS_OF, USED_AT)
    combined = allocate_usage(batches, qty_a + qty_b, AS_OF, USED_AT)

    split_total: dict[UUID, Decimal] = {}
    for result in (first, second):
        for batch_id, quantity in taken(result).items():
            split_total[batch_id] = split_total.get(batch_id, Decimal(0)) + quantity

    assert split_total == taken(combined)


# Usage times from half an hour before the first received_at to an hour after
# the last, so some batches arrive before the usage, some at it, some after.
usage_times = st.sampled_from([MONDAY_8AM + timedelta(minutes=30 * k) for k in range(-1, 7)])


@given(item_batches(), quantities, usage_times)
def test_batches_received_after_the_usage_are_skipped(
    batches: list[BatchSnapshot], qty: Decimal, used_at: datetime
) -> None:
    result = allocate_usage(batches, qty, AS_OF, used_at)

    later = {b.id for b in batches if not b.is_default and b.received_at > used_at}
    assert later.isdisjoint(taken(result))
    # The same as if the later batches weren't there; the default batch stays
    # whenever it was created.
    earlier = [b for b in batches if b.is_default or b.received_at <= used_at]
    assert result == allocate_usage(earlier, qty, AS_OF, used_at)
