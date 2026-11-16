"""Order actions: what the owner does with a purchase order (D69, D70, D72).

The par-level rule fills each supplier's draft (draft_lines.py). The owner can
set a quantity on a draft's line, send the draft, and cancel a sent order.
Receiving against a sent order is a delivery, so it is in receive() in
stock.py. Nothing is sent to the supplier: "sent" records that the owner
placed the order, and from then its lines count as on order.

Like the other domain functions, these work on the caller's Session, never
commit (hard rule 3), scope every lookup by business_id, and raise DomainError
for a request they refuse. They take no client_id: each is a state change
that can't happen twice, so a retry is refused with a message that says why.

Locking (D72). Each action first locks the business row exclusively, as the
alerts sweep does. Every stock write holds that row FOR SHARE from before its
first batch lock until it ends, so an action waits for the writes in progress
and writes wait for the action. With stock and orders standing still, an
action can change an order and re-evaluate the draft lines of the order's
items without locking each item.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.domain.checks import check_timestamp
from app.domain.draft_lines import evaluate_draft_lines
from app.domain.errors import DomainError
from app.models import PurchaseOrder, PurchaseOrderLine

# The most packs the owner can put on a line. Far beyond any real order,
# and it keeps a typo from reaching the database as a number its column
# can't hold.
MAX_ORDER_PACKS = 1_000_000_000


def order_packs(line: PurchaseOrderLine) -> int:
    """What a line orders: the owner's number, or the suggestion if there is none (D70)."""
    return line.suggested_packs if line.qty_packs is None else line.qty_packs


def set_line_quantity(
    session: Session, business_id: UUID, *, order_id: UUID, line_id: UUID, qty_packs: int | None
) -> PurchaseOrder:
    """Set the owner's number of packs on a draft's line; None goes back to "as suggested".

    0 means "don't order this": the line stays on the draft, and sending
    drops it. The number survives changes to the suggestion, but not the
    line: if the item recovers before the draft is sent, the rule removes
    the line (D67).
    """
    if qty_packs is not None:
        # bool is an int in Python; True packs is a mistake, not one pack.
        if isinstance(qty_packs, bool) or not isinstance(qty_packs, int):
            raise TypeError(f"qty_packs must be an int or None, not {type(qty_packs).__name__}")
        if qty_packs < 0:
            raise DomainError(f"qty_packs must not be negative, got {qty_packs}")
        if qty_packs > MAX_ORDER_PACKS:
            raise DomainError(f"qty_packs must be at most {MAX_ORDER_PACKS}, got {qty_packs}")

    _lock_business(session, business_id)
    order = _get_order(session, business_id, order_id)
    _check_status(order, "draft", "its lines can't be changed")
    line = session.scalar(
        select(PurchaseOrderLine).where(
            PurchaseOrderLine.business_id == business_id,
            PurchaseOrderLine.po_id == order.id,
            PurchaseOrderLine.id == line_id,
        )
    )
    if line is None:
        raise DomainError(f"line {line_id} not found on order {order_id}")
    line.qty_packs = qty_packs
    session.flush()
    return order


def send_order(
    session: Session, business_id: UUID, *, order_id: UUID, sent_at: datetime
) -> PurchaseOrder:
    """Mark a draft as sent: its lines are final and count as on order (D69, D70).

    Each line's qty_packs becomes what it orders, and lines that order
    nothing (set to 0 by the owner) are dropped. A draft with nothing to
    order can't be sent. The items are then re-evaluated: one whose order
    was cut short of its need, or dropped, goes straight onto a new draft.
    """
    check_timestamp(sent_at, "sent_at")
    _lock_business(session, business_id)
    order = _get_order(session, business_id, order_id)
    _check_status(order, "draft", "it can't be sent")
    lines = _lines(session, business_id, order.id)
    if not any(order_packs(line) > 0 for line in lines):
        raise DomainError(f"order {order_id} has nothing to order, so it can't be sent")

    for line in lines:
        if order_packs(line) > 0:
            line.qty_packs = order_packs(line)
        else:
            session.delete(line)
    order.status = "sent"
    order.sent_at = sent_at
    session.flush()
    evaluate_draft_lines(
        session, business_id, [(order.location_id, line.item_id) for line in lines]
    )
    return order


def cancel_order(
    session: Session, business_id: UUID, *, order_id: UUID, cancelled_at: datetime
) -> PurchaseOrder:
    """Give up on a sent order: it stops counting as on order (D69).

    Its items are re-evaluated, so whatever is still low goes onto the
    supplier's draft again. A draft can't be cancelled, because the rule
    would raise it again: set a line to 0 packs to leave an item out.
    """
    check_timestamp(cancelled_at, "cancelled_at")
    _lock_business(session, business_id)
    order = _get_order(session, business_id, order_id)
    _check_status(order, "sent", "it can't be cancelled")
    order.status = "cancelled"
    order.cancelled_at = cancelled_at
    session.flush()
    lines = _lines(session, business_id, order.id)
    evaluate_draft_lines(
        session, business_id, [(order.location_id, line.item_id) for line in lines]
    )
    return order


# --- Checks and lookups ---------------------------------------------------------


def _lock_business(session: Session, business_id: UUID) -> None:
    """Keep stock writes and other order actions out until this transaction ends.

    FOR NO KEY UPDATE is the lock an UPDATE of the row takes: it conflicts
    with the stock writes' FOR SHARE and with itself, but not with the
    FOR KEY SHARE that inserting any row of the business takes on it.
    """
    session.execute(
        text("SELECT id FROM businesses WHERE id = :business_id FOR NO KEY UPDATE"),
        {"business_id": business_id},
    )


def _get_order(session: Session, business_id: UUID, order_id: UUID) -> PurchaseOrder:
    """The business's order, read after the lock, so its status can't change under us."""
    order = session.scalar(
        select(PurchaseOrder)
        .where(PurchaseOrder.business_id == business_id, PurchaseOrder.id == order_id)
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise DomainError(f"order {order_id} not found")
    return order


def _check_status(order: PurchaseOrder, status: str, consequence: str) -> None:
    if order.status != status:
        article = "a " if status == "draft" else ""
        raise DomainError(
            f"order {order.id} is {order.status}, not {article}{status}, so {consequence}"
        )


def _lines(session: Session, business_id: UUID, order_id: UUID) -> list[PurchaseOrderLine]:
    return list(
        session.scalars(
            select(PurchaseOrderLine)
            .where(
                PurchaseOrderLine.business_id == business_id, PurchaseOrderLine.po_id == order_id
            )
            .order_by(PurchaseOrderLine.id)
            .execution_options(populate_existing=True)
        )
    )
