"""Draft order lines: what each supplier's open draft should hold (domain rule 8).

Draft lines are a projection, like alerts (D67): a supplier's draft at a
location holds exactly the lines that are due. A line is due for an item
there when (D68)
- the item is active, has a par level and has a preferred supplier;
- its usable on-hand is at or below reorder_point (the low_stock condition,
  with the same usable on-hand, D64);
- order_up_to minus usable on-hand minus what is on order is above zero.
The line goes on the preferred supplier's draft, in that supplier's pack
unit, for that amount rounded up to whole packs. On order is the item's
quantity on sent orders at the location; a sent order stops counting when it
is received or cancelled.

evaluate_draft_lines brings the given items' lines into line: it adds a line
(creating the supplier's draft if there is none), changes a suggestion that
no longer matches, and removes a line that is no longer due. It is called by
every stock write, by the sweep and by the order actions, through rules.py
and orders.py, never on its own, because the caller holds the locks:
- a stock write holds the business row FOR SHARE and its items' default
  batches, so two evaluations of one item take turns;
- the sweep and the order actions hold the business row exclusively, so
  nothing else is writing stock or orders for the business.
Two stock writes for different items can fill the same draft at once: the
draft is created with INSERT … ON CONFLICT, and they add different lines. A
draft is never deleted, even when its last line goes, so no write can remove
a draft another is adding to.

Like the other domain functions, these work on the caller's Session and never
commit (hard rule 3).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.orm import Session

from app.domain.alerts import STOCK_CTES, Pair, pair_params
from app.models import PurchaseOrder, PurchaseOrderLine


@dataclass(frozen=True, slots=True)
class DraftLineDifference:
    """How one item's draft line at a location differs from what is due.

    wanted_packs is None when no line is due, and line_id is None when there
    is no line; otherwise the line exists with another suggestion or unit.
    """

    location_id: UUID
    item_id: UUID
    supplier_id: UUID
    wanted_unit_id: UUID | None
    wanted_packs: int | None
    line_id: UUID | None
    line_packs: int | None

    @property
    def kind(self) -> str:
        if self.line_id is None:
            return "missing"
        if self.wanted_packs is None:
            return "not_due"
        return "mismatch"


# wanted: the lines that are due for the touched pairs (see the module docstring).
#   on_order sums the item's lines on sent orders at the location, each in
#   base units: packs times its unit's factor_to_base. Sent lines always have
#   qty_packs (D70).
# existing: the touched items' lines on drafts at their location.
# The FULL JOIN pairs them up on location, item and supplier, and the WHERE
# keeps only the pairs that differ: a wanted line with no line, a line that
# isn't wanted, or a line whose suggestion or unit is out of date. The
# supplier is part of the match, so if an item's preferred supplier changes,
# its old line shows as not wanted and the new supplier's as missing.
_DIFFERENCES = text(
    f"""
    WITH {STOCK_CTES},
    wanted AS (
        SELECT p.location_id,
               p.item_id,
               si.supplier_id,
               si.item_unit_id,
               CEIL((p.order_up_to - u.qty - on_order.qty) / unit.factor_to_base)::integer
                   AS packs
        FROM usable AS u
        JOIN par_levels AS p
          ON p.business_id = :business_id
         AND p.location_id = u.location_id
         AND p.item_id = u.item_id
        JOIN items AS i ON i.business_id = p.business_id AND i.id = p.item_id
        JOIN supplier_items AS si
          ON si.business_id = p.business_id AND si.item_id = p.item_id AND si.is_preferred
        JOIN item_units AS unit
          ON unit.business_id = si.business_id AND unit.id = si.item_unit_id
        CROSS JOIN LATERAL (
            SELECT COALESCE(sum(l.qty_packs * lu.factor_to_base), 0) AS qty
            FROM purchase_orders AS po
            JOIN purchase_order_lines AS l
              ON l.business_id = po.business_id AND l.po_id = po.id
            JOIN item_units AS lu ON lu.business_id = l.business_id AND lu.id = l.item_unit_id
            WHERE po.business_id = :business_id
              AND po.status = 'sent'
              AND po.location_id = p.location_id
              AND l.item_id = p.item_id
        ) AS on_order
        WHERE i.is_active
          AND u.qty <= p.reorder_point
          AND p.order_up_to - u.qty - on_order.qty > 0
    ),
    existing AS (
        SELECT l.id AS line_id,
               po.location_id,
               l.item_id,
               po.supplier_id,
               l.item_unit_id,
               l.suggested_packs
        FROM touched AS t
        JOIN purchase_orders AS po
          ON po.business_id = :business_id
         AND po.location_id = t.location_id
         AND po.status = 'draft'
        JOIN purchase_order_lines AS l
          ON l.business_id = po.business_id AND l.po_id = po.id AND l.item_id = t.item_id
    )
    SELECT COALESCE(w.location_id, e.location_id) AS location_id,
           COALESCE(w.item_id, e.item_id) AS item_id,
           COALESCE(w.supplier_id, e.supplier_id) AS supplier_id,
           w.item_unit_id AS wanted_unit_id,
           w.packs AS wanted_packs,
           e.line_id,
           e.suggested_packs AS line_packs
    FROM wanted AS w
    FULL JOIN existing AS e
      ON e.location_id = w.location_id
     AND e.item_id = w.item_id
     AND e.supplier_id = w.supplier_id
    WHERE w.item_id IS NULL
       OR e.line_id IS NULL
       OR e.suggested_packs <> w.packs
       OR e.item_unit_id <> w.item_unit_id
    ORDER BY 1, 2, 3
    """
)


def _differences(
    session: Session, business_id: UUID, pairs: list[Pair]
) -> list[DraftLineDifference]:
    rows = session.execute(_DIFFERENCES, pair_params(business_id, pairs))
    return [DraftLineDifference(*row) for row in rows]


def all_pairs(session: Session, business_id: UUID) -> list[Pair]:
    """Every (location, item) of the business that can have a draft line.

    A line needs a par level. Pairs that have a draft line are included as
    well, so a line left behind by a deleted par level is still looked at.
    """
    rows = session.execute(
        text(
            """
            SELECT location_id, item_id FROM par_levels WHERE business_id = :business_id
            UNION
            SELECT po.location_id, l.item_id
            FROM purchase_orders AS po
            JOIN purchase_order_lines AS l ON l.business_id = po.business_id AND l.po_id = po.id
            WHERE po.business_id = :business_id AND po.status = 'draft'
            ORDER BY location_id, item_id
            """
        ),
        {"business_id": business_id},
    )
    return [(location_id, item_id) for location_id, item_id in rows]


# --- Evaluate -------------------------------------------------------------------


def evaluate_draft_lines(
    session: Session, business_id: UUID, pairs: Iterable[Pair]
) -> list[DraftLineDifference]:
    """Bring the draft lines of these (location_id, item_id) pairs into line.

    Returns the differences it found and fixed. Most calls find none: an
    item's line changes only when it crosses its reorder point or its need
    crosses a whole pack. The caller holds the locks (see the module docstring).
    """
    pairs = sorted(set(pairs))
    if not pairs:
        return []
    differences = _differences(session, business_id, pairs)
    for difference in differences:
        if difference.wanted_packs is None:
            session.execute(
                delete(PurchaseOrderLine).where(
                    PurchaseOrderLine.business_id == business_id,
                    PurchaseOrderLine.id == difference.line_id,
                )
            )
        elif difference.line_id is None:
            session.execute(
                insert(PurchaseOrderLine).values(
                    business_id=business_id,
                    po_id=_draft_order(session, business_id, difference),
                    item_id=difference.item_id,
                    item_unit_id=difference.wanted_unit_id,
                    suggested_packs=difference.wanted_packs,
                )
            )
        else:
            _update_line(session, business_id, difference)
    return differences


def _draft_order(session: Session, business_id: UUID, difference: DraftLineDifference) -> UUID:
    """The supplier's draft at the location, created if there is none.

    ON CONFLICT targets the partial unique index (one draft per location and
    supplier), so a concurrent write creating the same draft makes this wait
    for it and then do nothing; the SELECT then finds whichever exists.
    """
    key = {
        "business_id": business_id,
        "location_id": difference.location_id,
        "supplier_id": difference.supplier_id,
    }
    session.execute(
        text(
            """
            INSERT INTO purchase_orders (business_id, location_id, supplier_id)
            VALUES (:business_id, :location_id, :supplier_id)
            ON CONFLICT (location_id, supplier_id) WHERE status = 'draft' DO NOTHING
            """
        ),
        key,
    )
    return session.scalars(
        select(PurchaseOrder.id).where(
            PurchaseOrder.business_id == business_id,
            PurchaseOrder.location_id == difference.location_id,
            PurchaseOrder.supplier_id == difference.supplier_id,
            PurchaseOrder.status == "draft",
        )
    ).one()


def _update_line(session: Session, business_id: UUID, difference: DraftLineDifference) -> None:
    """Set the line's suggestion, and its unit if the supplier's pack unit changed.

    The owner's qty_packs stays, unless the unit changed: a number of the old
    packs means nothing in the new ones, so it goes back to "as suggested".
    """
    line = (PurchaseOrderLine.business_id == business_id) & (
        PurchaseOrderLine.id == difference.line_id
    )
    session.execute(
        update(PurchaseOrderLine)
        .where(line, PurchaseOrderLine.item_unit_id != difference.wanted_unit_id)
        .values(item_unit_id=difference.wanted_unit_id, qty_packs=None)
    )
    session.execute(
        update(PurchaseOrderLine).where(line).values(suggested_packs=difference.wanted_packs)
    )


# --- Check ----------------------------------------------------------------------


def check_draft_lines(session: Session, business_id: UUID) -> list[DraftLineDifference]:
    """How the draft lines differ from a fresh evaluation of every item; reads only.

    An empty list means every draft holds exactly the lines that are due.
    reconcile reports each difference as a problem (D67).
    """
    pairs = all_pairs(session, business_id)
    if not pairs:
        return []  # unnest of nothing would find nothing; skip the query
    return _differences(session, business_id, pairs)
