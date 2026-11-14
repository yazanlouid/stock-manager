"""Alerts: low stock, expiring, expired and negative stock (D59 to D64).

Alerts are a projection, like stock_balances: the open alerts of an item at a
location always equal what its batches, balances and par level say is due.
Whatever changes those re-evaluates the item in the same transaction:
- every stock write (stock.py and counts.py) calls evaluate_alerts for the
  items it touched, after its movements;
- sweep_alerts moves the business's alerts date and re-evaluates every item.
Nothing else writes the alerts table. Like the other domain functions, these
work on the caller's Session and never commit (hard rule 3).

What is due (the _DUE query below), per item and location:
- negative_stock: the item's default batch is below zero (domain rule 4);
- expired: a batch with stock whose expiry date is before the alerts date;
- expiring: a batch with stock that hasn't expired and expires within the
  item's window, min(expiry_warn_days, shelf_life_days // 2) days (D63);
- low_stock: an active item with a par level whose usable on-hand, the sum
  of its batches that haven't expired, is at or below reorder_point (D64).

The alerts date (D61). "Expired" needs a date, and the open alerts can only be
checked against a fresh evaluation if every item was judged by the same one.
So the date is stored, in businesses.alerts_swept_on, and only the sweep moves
it. A stock write judges against the stored date, never its own. Until the
first sweep the date is NULL, and a comparison with NULL is never true in SQL,
so nothing counts as expiring or expired yet.

Locking. The caller of evaluate_alerts holds the default-batch lock of each
item it passes (D36), so two evaluations of one item take turns. The business
row is the lock on the alerts date: the sweep updates it, and evaluate_alerts
locks it FOR SHARE in a statement of its own before it evaluates. So a sweep
waits for the writes that are evaluating, a write waits for a running sweep,
and whichever goes second starts its evaluation after the first has committed
and sees what it did (the same reasoning as Locking in stock.py).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.checks import check_timestamp
from app.domain.errors import DomainError

ALERT_TYPES = frozenset({"low_stock", "expiring", "expired", "negative_stock"})


@dataclass(frozen=True, slots=True)
class AlertKey:
    """What an alert is about. batch_id is None for low_stock, and only for it."""

    type: str
    location_id: UUID
    item_id: UUID
    batch_id: UUID | None


@dataclass(frozen=True, slots=True)
class AlertChange:
    """An alert an evaluation raised or resolved. change is "raised" or "resolved"."""

    change: str
    key: AlertKey


@dataclass(frozen=True, slots=True)
class AlertDifference:
    """An alert that is due but not open ("missing"), or open but not due ("not_due")."""

    kind: str
    key: AlertKey


# --- What is due ----------------------------------------------------------------

# Three CTEs shared by the evaluation and the check. They take the bind
# parameters :business_id, :location_ids and :item_ids; the two arrays are
# the (location, item) pairs to look at, position by position.
#
# stock: every batch of those items that holds stock, with what the rules need.
#   expired and days_left are NULL for a batch with no expiry date and for a
#   business that has never been swept; every use below treats NULL as
#   "not expired, not expiring".
#   LEAST skips a NULL, so an item with no shelf life gets expiry_warn_days;
#   shelf_life_days / 2 is integer division.
# due: one row per alert that should be open.
_DUE = """
    touched AS (
        SELECT location_id, item_id
        FROM unnest(CAST(:location_ids AS uuid[]), CAST(:item_ids AS uuid[]))
            AS pair (location_id, item_id)
    ),
    stock AS (
        SELECT b.location_id,
               b.item_id,
               b.id AS batch_id,
               b.is_default,
               s.qty_on_hand,
               b.expires_on < biz.alerts_swept_on AS expired,
               b.expires_on - biz.alerts_swept_on AS days_left,
               LEAST(biz.expiry_warn_days, i.shelf_life_days / 2) AS warn_days
        FROM touched AS t
        JOIN batches AS b
          ON b.business_id = :business_id
         AND b.location_id = t.location_id
         AND b.item_id = t.item_id
        JOIN stock_balances AS s ON s.business_id = b.business_id AND s.batch_id = b.id
        JOIN items AS i ON i.business_id = b.business_id AND i.id = b.item_id
        JOIN businesses AS biz ON biz.id = b.business_id
        WHERE s.qty_on_hand <> 0
    ),
    due AS (
        SELECT 'negative_stock' AS type, location_id, item_id, batch_id
        FROM stock
        WHERE is_default AND qty_on_hand < 0
        UNION ALL
        SELECT 'expired', location_id, item_id, batch_id
        FROM stock
        WHERE qty_on_hand > 0 AND expired
        UNION ALL
        SELECT 'expiring', location_id, item_id, batch_id
        FROM stock
        WHERE qty_on_hand > 0 AND NOT expired AND days_left <= warn_days
        UNION ALL
        -- Usable on-hand: every batch that hasn't expired, the default batch
        -- included even when negative. An item with no stock at all has 0.
        SELECT 'low_stock', p.location_id, p.item_id, NULL
        FROM touched AS t
        JOIN par_levels AS p
          ON p.business_id = :business_id
         AND p.location_id = t.location_id
         AND p.item_id = t.item_id
        JOIN items AS i ON i.business_id = p.business_id AND i.id = p.item_id
        WHERE i.is_active
          AND p.reorder_point >= COALESCE(
                (SELECT sum(st.qty_on_hand)
                 FROM stock AS st
                 WHERE st.location_id = p.location_id
                   AND st.item_id = p.item_id
                   AND st.expired IS NOT TRUE),
                0)
    )
"""

# One statement brings the touched items' alerts into line: it resolves the
# open alerts that are no longer due and raises the due ones that aren't open,
# and returns both lists. Both halves read the alerts table as it was when the
# statement started, and they never touch the same alert: one handles open
# alerts that aren't due, the other due alerts that aren't open.
#
# resolved_at is :at, but never earlier than raised_at: a write dated before
# the alert was raised (a backdated request) resolves it at raised_at (D60).
# IS NOT DISTINCT FROM compares batch ids so that two NULLs (low_stock) match.
_EVALUATE = text(
    f"""
    WITH {_DUE},
    resolved AS (
        UPDATE alerts AS a
        SET status = 'resolved',
            resolved_at = GREATEST(a.raised_at, CAST(:at AS timestamptz))
        FROM touched AS t
        WHERE a.business_id = :business_id
          AND a.status = 'open'
          AND a.location_id = t.location_id
          AND a.item_id = t.item_id
          AND NOT EXISTS (
                SELECT 1 FROM due AS d
                WHERE d.location_id = a.location_id
                  AND d.item_id = a.item_id
                  AND d.type = a.type
                  AND d.batch_id IS NOT DISTINCT FROM a.batch_id)
        RETURNING a.type, a.location_id, a.item_id, a.batch_id
    ),
    raised AS (
        INSERT INTO alerts (business_id, location_id, item_id, batch_id, type, raised_at)
        SELECT :business_id, d.location_id, d.item_id, d.batch_id, d.type,
               CAST(:at AS timestamptz)
        FROM due AS d
        WHERE NOT EXISTS (
                SELECT 1 FROM alerts AS a
                WHERE a.business_id = :business_id
                  AND a.status = 'open'
                  AND a.location_id = d.location_id
                  AND a.item_id = d.item_id
                  AND a.type = d.type
                  AND a.batch_id IS NOT DISTINCT FROM d.batch_id)
        RETURNING type, location_id, item_id, batch_id
    )
    SELECT 'raised' AS change, type, location_id, item_id, batch_id FROM raised
    UNION ALL
    SELECT 'resolved', type, location_id, item_id, batch_id FROM resolved
    """
)

# The same comparison, read-only: what a fresh evaluation would change.
_CHECK = text(
    f"""
    WITH {_DUE}
    SELECT 'missing' AS kind, d.type, d.location_id, d.item_id, d.batch_id
    FROM due AS d
    WHERE NOT EXISTS (
            SELECT 1 FROM alerts AS a
            WHERE a.business_id = :business_id
              AND a.status = 'open'
              AND a.location_id = d.location_id
              AND a.item_id = d.item_id
              AND a.type = d.type
              AND a.batch_id IS NOT DISTINCT FROM d.batch_id)
    UNION ALL
    SELECT 'not_due', a.type, a.location_id, a.item_id, a.batch_id
    FROM alerts AS a
    WHERE a.business_id = :business_id
      AND a.status = 'open'
      AND NOT EXISTS (
            SELECT 1 FROM due AS d
            WHERE d.location_id = a.location_id
              AND d.item_id = a.item_id
              AND d.type = a.type
              AND d.batch_id IS NOT DISTINCT FROM a.batch_id)
    """
)

type Pair = tuple[UUID, UUID]  # (location_id, item_id)


def _pair_params(business_id: UUID, pairs: list[Pair]) -> dict:
    return {
        "business_id": business_id,
        "location_ids": [location_id for location_id, _ in pairs],
        "item_ids": [item_id for _, item_id in pairs],
    }


def _all_pairs(session: Session, business_id: UUID) -> list[Pair]:
    """Every (location, item) of the business that can have an alert.

    An alert needs a batch or a par level. Pairs with an open alert are
    included as well, so an alert left behind by a deleted par level is
    still looked at.
    """
    rows = session.execute(
        text(
            """
            SELECT location_id, item_id FROM batches WHERE business_id = :business_id
            UNION
            SELECT location_id, item_id FROM par_levels WHERE business_id = :business_id
            UNION
            SELECT location_id, item_id FROM alerts
            WHERE business_id = :business_id AND status = 'open'
            ORDER BY location_id, item_id
            """
        ),
        {"business_id": business_id},
    )
    return [(location_id, item_id) for location_id, item_id in rows]


# --- Evaluate -------------------------------------------------------------------


def evaluate_alerts(
    session: Session, business_id: UUID, pairs: Iterable[Pair], at: datetime
) -> list[AlertChange]:
    """Bring the alerts of these (location_id, item_id) pairs into line with their stock.

    Call it after writing the movements, holding each pair's default-batch
    lock. at is business time, the write's occurred_at: it becomes raised_at
    of an alert this raises and resolved_at of one it resolves.
    """
    pairs = sorted(set(pairs))
    if not pairs:
        return []
    # Wait for a sweep that is running, and keep the alerts date still until
    # this transaction ends (see Locking above). A statement of its own, so
    # the evaluation below starts after any sweep it waited for has committed.
    session.execute(
        text("SELECT id FROM businesses WHERE id = :business_id FOR SHARE"),
        {"business_id": business_id},
    )
    return _evaluate(session, business_id, pairs, at)


def _evaluate(
    session: Session, business_id: UUID, pairs: list[Pair], at: datetime
) -> list[AlertChange]:
    rows = session.execute(_EVALUATE, _pair_params(business_id, pairs) | {"at": at})
    return [
        AlertChange(change, AlertKey(type, location_id, item_id, batch_id))
        for change, type, location_id, item_id, batch_id in rows
    ]


# --- Sweep ----------------------------------------------------------------------


def sweep_alerts(session: Session, business_id: UUID, as_of: datetime) -> list[AlertChange]:
    """Move the alerts date to as_of's local date and re-evaluate every item.

    This is what turns a batch from fine to expiring to expired as days pass,
    and makes an item low when the stock it relied on expires. as_of is
    business time and stamps the alerts it raises and resolves. The date only
    moves forward: a sweep as of an earlier date than the last one is refused,
    and one as of the same date re-evaluates, which changes nothing unless
    something was out of line.
    """
    check_timestamp(as_of, "as_of")
    # as_of's date in the business's timezone, computed by Postgres (D32).
    # The UPDATE locks the business row, so it waits for writes that are
    # evaluating (they hold it FOR SHARE), and they wait for this sweep.
    swept_on = session.scalar(
        text(
            """
            UPDATE businesses
            SET alerts_swept_on = (CAST(:as_of AS timestamptz) AT TIME ZONE timezone)::date
            WHERE id = :business_id
              AND (alerts_swept_on IS NULL
                   OR alerts_swept_on <= (CAST(:as_of AS timestamptz) AT TIME ZONE timezone)::date)
            RETURNING alerts_swept_on
            """
        ),
        {"business_id": business_id, "as_of": as_of},
    )
    if swept_on is None:
        raise DomainError(_sweep_refusal(session, business_id))
    pairs = _all_pairs(session, business_id)
    if not pairs:
        return []
    return _evaluate(session, business_id, pairs, as_of)


def _sweep_refusal(session: Session, business_id: UUID) -> str:
    """Why the sweep's UPDATE matched no row: no such business, or a later date."""
    row = session.execute(
        text("SELECT alerts_swept_on FROM businesses WHERE id = :business_id"),
        {"business_id": business_id},
    ).one_or_none()
    if row is None:
        return f"business {business_id} not found"
    return f"alerts were already swept as of {row.alerts_swept_on}, and the date can't move back"


def sweep_alerts_if_due(session: Session, business_id: UUID, now: datetime) -> date | None:
    """Sweep if now's local date is past the alerts date; return the alerts date (D62).

    GET /alerts calls this, so the list is current whenever someone reads it.
    The check is a plain read: on most reads the date hasn't moved and nothing
    is locked. Two reads that both find a sweep due both sweep, one after the
    other, and the second changes nothing.
    """
    row = session.execute(
        text(
            """
            SELECT alerts_swept_on,
                   (CAST(:now AS timestamptz) AT TIME ZONE timezone)::date AS today
            FROM businesses WHERE id = :business_id
            """
        ),
        {"business_id": business_id, "now": now},
    ).one_or_none()
    if row is None:
        raise DomainError(f"business {business_id} not found")
    if row.alerts_swept_on is not None and row.alerts_swept_on >= row.today:
        return row.alerts_swept_on
    sweep_alerts(session, business_id, now)
    return row.today


# --- Check ----------------------------------------------------------------------


def check_alerts(session: Session, business_id: UUID) -> list[AlertDifference]:
    """How the open alerts differ from a fresh evaluation of every item; reads only.

    An empty list means the alerts are right as of the business's alerts date.
    reconcile reports each difference as a problem (D59).
    """
    pairs = _all_pairs(session, business_id)
    if not pairs:
        return []
    rows = session.execute(_CHECK, _pair_params(business_id, pairs))
    return [
        AlertDifference(kind, AlertKey(type, location_id, item_id, batch_id))
        for kind, type, location_id, item_id, batch_id in rows
    ]
