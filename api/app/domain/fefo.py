"""FEFO allocation: which batches a usage draws from (domain rules 2 to 5).

This is a pure function over snapshots. It never touches the database or the
clock: the caller loads one item's batches at one location (locked with
SELECT ... FOR UPDATE), passes the usage's local date as as_of, and writes one
usage movement per allocation, in the order returned.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from app.domain.checks import ZERO, check_quantity


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
    """One batch as the allocation sees it. on_hand comes from stock_balances."""

    id: UUID
    expires_on: date | None
    received_at: datetime
    on_hand: Decimal
    is_default: bool


@dataclass(frozen=True, slots=True)
class Allocation:
    """Draw quantity (> 0, in base units) from one batch."""

    batch_id: UUID
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class FefoResult:
    """The allocations in FEFO order, and the part the default batch didn't hold.

    The allocations always sum to the requested quantity. shortfall > 0 means the
    default batch went (further) negative and a negative-stock alert is due.
    """

    allocations: tuple[Allocation, ...]
    shortfall: Decimal


def allocate_usage(batches: Sequence[BatchSnapshot], quantity: Decimal, as_of: date) -> FefoResult:
    """Split a usage of `quantity` across one item's batches, earliest expiry first.

    Usable batches are the non-default ones with stock on hand that haven't
    expired: a batch is usable on its expiry date and skipped from the next day.
    They are drawn in order of expires_on (no date last), then received_at, then
    id. Whatever they can't cover goes to the default batch, last, even if that
    takes it negative.
    """
    check_quantity(quantity)
    default = _default_batch(batches)

    usable = sorted(
        (b for b in batches if not b.is_default and b.on_hand > 0 and _not_expired(b, as_of)),
        key=_fefo_key,
    )

    allocations: list[Allocation] = []
    remaining = quantity
    for batch in usable:
        if remaining == 0:
            break
        take = min(remaining, batch.on_hand)
        allocations.append(Allocation(batch.id, take))
        remaining -= take

    shortfall = ZERO
    if remaining > 0:
        allocations.append(Allocation(default.id, remaining))
        # Only the default batch's positive stock covers part of the remainder.
        shortfall = max(ZERO, remaining - max(ZERO, default.on_hand))

    return FefoResult(tuple(allocations), shortfall)


def _fefo_key(batch: BatchSnapshot) -> tuple[bool, date, datetime, UUID]:
    """Sort key for usable batches: expires_on ascending with no date last,
    then received_at, then id, so the order is total."""
    no_expiry = batch.expires_on is None
    return (no_expiry, batch.expires_on or date.min, batch.received_at, batch.id)


def _not_expired(batch: BatchSnapshot, as_of: date) -> bool:
    return batch.expires_on is None or batch.expires_on >= as_of


def _default_batch(batches: Sequence[BatchSnapshot]) -> BatchSnapshot:
    defaults = [b for b in batches if b.is_default]
    if len(defaults) != 1:
        raise ValueError(f"expected exactly one default batch, got {len(defaults)}")
    return defaults[0]
