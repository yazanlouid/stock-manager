"""The reconcile check: does a business's ledger hold together?

reconcile() only reads. It never writes or commits, so it can run at any time,
on its own or after the simulator. An empty list means the ledger reconciles.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.domain.stock import rebuild_balances
from app.models import Batch, Item, ReceiptLine, StockMovement


@dataclass(frozen=True, slots=True)
class Problem:
    """One way the ledger fails to reconcile.

    kind is one of balance_mismatch, negative_batch, default_batch_count,
    receipt_line_unposted or receipt_line_mismatch.
    """

    kind: str
    message: str


def reconcile(session: Session, business_id: UUID) -> list[Problem]:
    """Every problem found in the business's ledger, in a fixed order."""
    return [
        *_balance_mismatches(session, business_id),
        *_negative_batches(session, business_id),
        *_default_batch_counts(session, business_id),
        *_receipt_line_problems(session, business_id),
    ]


def _batch_labels(session: Session, business_id: UUID, ids: list[UUID]) -> dict[UUID, str]:
    """A readable name per batch: item SKU, and expiry, lot or "default"."""
    if not ids:
        return {}
    rows = session.execute(
        select(Batch.id, Item.sku, Batch.is_default, Batch.expires_on, Batch.lot_code)
        .join(Item, and_(Item.business_id == Batch.business_id, Item.id == Batch.item_id))
        .where(Batch.business_id == business_id, Batch.id.in_(ids))
    )
    labels = {}
    for batch_id, sku, is_default, expires_on, lot_code in rows:
        if is_default:
            detail = "default batch"
        else:
            parts = [f"expires {expires_on}" if expires_on else None, lot_code]
            detail = "batch " + (", ".join(p for p in parts if p) or str(batch_id))
        labels[batch_id] = f"{sku} {detail} ({batch_id})"
    return labels


def _balance_mismatches(session: Session, business_id: UUID) -> list[Problem]:
    """Rule 1: each batch's stock_balances row equals the sum of its movements."""
    differences = rebuild_balances(session, business_id, fix=False)
    labels = _batch_labels(session, business_id, [d.batch_id for d in differences])
    problems = []
    for d in differences:
        if d.actual_qty is None:
            actual = "no balance row"
        else:
            actual = f"{d.actual_qty} (last movement {d.actual_last_movement_at})"
        problems.append(
            Problem(
                "balance_mismatch",
                f"{labels[d.batch_id]}: balance shows {actual}, the ledger gives"
                f" {d.expected_qty} (last movement {d.expected_last_movement_at})",
            )
        )
    return problems


def _negative_batches(session: Session, business_id: UUID) -> list[Problem]:
    """Only a default batch may go negative (rule 4); any other batch is a bug."""
    total = func.sum(StockMovement.qty_delta)
    rows = session.execute(
        select(Batch.id, total)
        .join(
            StockMovement,
            and_(
                StockMovement.business_id == Batch.business_id,
                StockMovement.batch_id == Batch.id,
            ),
        )
        .where(Batch.business_id == business_id, ~Batch.is_default)
        .group_by(Batch.id)
        .having(total < 0)
        .order_by(Batch.id)
    ).all()
    labels = _batch_labels(session, business_id, [batch_id for batch_id, _ in rows])
    return [
        Problem("negative_batch", f"{labels[batch_id]}: the ledger gives {qty}, below zero")
        for batch_id, qty in rows
    ]


def _default_batch_counts(session: Session, business_id: UUID) -> list[Problem]:
    """Rule 5: an item with batches at a location has exactly one default batch there."""
    defaults = func.count().filter(Batch.is_default)
    rows = session.execute(
        select(Batch.location_id, Item.sku, defaults)
        .join(Item, and_(Item.business_id == Batch.business_id, Item.id == Batch.item_id))
        .where(Batch.business_id == business_id)
        .group_by(Batch.location_id, Item.sku)
        .having(defaults != 1)
        .order_by(Item.sku, Batch.location_id)
    ).all()
    return [
        Problem(
            "default_batch_count",
            f"{sku} at location {location_id} has {count} default batches, not exactly one",
        )
        for location_id, sku, count in rows
    ]


def _receipt_line_problems(session: Session, business_id: UUID) -> list[Problem]:
    """Each receipt line has its receive movement, carrying the line's qty_base and unit_cost."""
    rows = session.execute(
        select(
            ReceiptLine.id,
            Item.sku,
            ReceiptLine.qty_base,
            ReceiptLine.unit_cost,
            StockMovement.id,
            StockMovement.qty_delta,
            StockMovement.unit_cost,
        )
        .join(
            Item, and_(Item.business_id == ReceiptLine.business_id, Item.id == ReceiptLine.item_id)
        )
        .outerjoin(
            StockMovement,
            and_(
                StockMovement.business_id == ReceiptLine.business_id,
                StockMovement.receipt_line_id == ReceiptLine.id,
            ),
        )
        .where(
            ReceiptLine.business_id == business_id,
            or_(
                StockMovement.id.is_(None),
                StockMovement.qty_delta != ReceiptLine.qty_base,
                StockMovement.unit_cost.is_distinct_from(ReceiptLine.unit_cost),
            ),
        )
        .order_by(ReceiptLine.id)
    ).all()
    problems = []
    for line_id, sku, qty_base, unit_cost, movement_id, qty_delta, movement_cost in rows:
        if movement_id is None:
            problems.append(
                Problem(
                    "receipt_line_unposted",
                    f"receipt line {line_id} ({sku}) has no receive movement",
                )
            )
        else:
            problems.append(
                Problem(
                    "receipt_line_mismatch",
                    f"receipt line {line_id} ({sku}) is {qty_base} at {unit_cost},"
                    f" but its receive movement {movement_id} is {qty_delta} at {movement_cost}",
                )
            )
    return problems
