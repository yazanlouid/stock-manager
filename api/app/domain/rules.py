"""The rules that follow from stock: alerts and draft order lines, kept together.

Both are projections of the same things (balances, par levels and the alerts
date), so whatever re-evaluates one must re-evaluate the other. The stock
writes, the sweep and the order actions go through here, so neither can be
forgotten:
- evaluate_rules: after a stock write or an order action, for the items it touched;
- sweep and sweep_if_due: when the day changes, for every item.
"""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.domain import draft_lines
from app.domain.alerts import (
    AlertChange,
    Pair,
    evaluate_alerts,
    sweep_alerts,
    sweep_alerts_if_due,
)


def evaluate_rules(
    session: Session, business_id: UUID, pairs: Iterable[Pair], at: datetime
) -> None:
    """Bring the alerts and draft lines of these (location_id, item_id) pairs into line.

    Alerts first, then draft lines; neither reads the other. The caller holds
    the locks: see Locking in alerts.py and the docstring of draft_lines.py.
    at is business time, for the alerts raised and resolved.
    """
    pairs = sorted(set(pairs))
    evaluate_alerts(session, business_id, pairs, at)
    draft_lines.evaluate_draft_lines(session, business_id, pairs)


def sweep(session: Session, business_id: UUID, as_of: datetime) -> list[AlertChange]:
    """Move the alerts date to as_of's local date and re-evaluate every item (D61).

    A batch that expires leaves usable on-hand, which can make an item low
    and put it on a draft order, so the draft lines are re-evaluated with the
    alerts. sweep_alerts takes the business row exclusively first, which
    covers the draft lines too. Returns the alerts raised and resolved.
    """
    changes = sweep_alerts(session, business_id, as_of)
    _evaluate_all_draft_lines(session, business_id)
    return changes


def sweep_if_due(session: Session, business_id: UUID, now: datetime) -> None:
    """Sweep as of now if its local date is past the alerts date (D62).

    GET /alerts and GET /orders call this before they list, so what they
    show is current whenever someone reads it.
    """
    if sweep_alerts_if_due(session, business_id, now):
        _evaluate_all_draft_lines(session, business_id)


def _evaluate_all_draft_lines(session: Session, business_id: UUID) -> None:
    pairs = draft_lines.all_pairs(session, business_id)
    draft_lines.evaluate_draft_lines(session, business_id, pairs)
