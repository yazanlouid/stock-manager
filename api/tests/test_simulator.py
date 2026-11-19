"""The seed, the simulator and gate 2 (D53, D88): three venues reconcile and
run the rules on their own.

The gate test runs each venue's 14 days once, on db_session: each day's
commit only releases a savepoint, and the test's transaction is rolled back
at the end. Its path checks read the ledger, the alerts and the orders, not
the simulator's counters. Gate 1's 30 café days fold into it (D88).
"""

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, func, or_, select, text
from sqlalchemy.orm import Session

from app.domain.errors import DomainError
from app.domain.reconcile import reconcile
from app.models import (
    Alert,
    Batch,
    Business,
    Item,
    Location,
    ParLevel,
    PurchaseOrder,
    PurchaseOrderLine,
    Receipt,
    ReceiptLine,
    StockBalance,
    StockCount,
    StockCountLine,
    StockMovement,
    Supplier,
    SupplierItem,
    User,
)
from app.sim import simulator
from app.sim.cafe import CAFE
from app.sim.kitchen import KITCHEN
from app.sim.seed import PLACEHOLDER_PASSWORD_HASH, seed
from app.sim.simulator import DEFAULT_RANDOM_SEED, SimulationError, simulate
from app.sim.venue import Venue
from app.sim.venues import VENUES
from tests.test_alerts import recorded_statements

END_DATE = date(2026, 10, 31)
# Asia/Amman has been UTC+3 all year since October 2022 (see test_stock_properties.py).
AMMAN = timezone(timedelta(hours=3))


def local_dates(db: Session, business_id) -> tuple[date, date]:
    """The first and last occurred_at of the business's movements, as dates in
    the business's timezone."""
    return db.execute(
        text(
            "SELECT min((m.occurred_at AT TIME ZONE b.timezone)::date),"
            " max((m.occurred_at AT TIME ZONE b.timezone)::date)"
            " FROM stock_movements AS m JOIN businesses AS b ON b.id = m.business_id"
            " WHERE m.business_id = :business_id"
        ),
        {"business_id": business_id},
    ).one()


# --- Seed -----------------------------------------------------------------------


@pytest.mark.parametrize("venue", VENUES, ids=lambda venue: venue.name)
def test_seed_writes_the_venue_catalogue_and_no_stock(db_session: Session, venue: Venue):
    business_id = seed(db_session, venue)

    business = db_session.get(Business, business_id)
    assert (business.name, business.timezone, business.currency) == (
        venue.name,
        venue.timezone,
        venue.currency,
    )
    locations = db_session.scalars(select(Location).where(Location.business_id == business_id))
    assert [location.kind for location in locations] == ["branch"]

    users = db_session.scalars(select(User).where(User.business_id == business_id)).all()
    assert sorted(user.role for user in users) == ["owner", "staff"]
    for user in users:
        assert user.password_hash == PLACEHOLDER_PASSWORD_HASH
        assert user.password_hash.startswith("!")
        assert user.email == user.email.lower() and user.email.endswith(".example")

    items = db_session.scalars(select(Item).where(Item.business_id == business_id)).all()
    assert 20 <= len(items) <= 45
    kinds = {(item.tracks_batches, item.tracks_expiry) for item in items}
    # Batches with expiry, batches by lot without expiry, and no batch tracking.
    assert kinds == {(True, True), (True, False), (False, False)}
    assert all(item.shelf_life_days for item in items if item.tracks_expiry)

    suppliers = db_session.scalar(
        select(func.count()).select_from(Supplier).where(Supplier.business_id == business_id)
    )
    assert suppliers == len(venue.suppliers)

    supplier_items = db_session.scalars(
        select(SupplierItem).where(SupplierItem.business_id == business_id)
    ).all()
    preferred = [si for si in supplier_items if si.is_preferred]
    assert sorted(si.item_id for si in preferred) == sorted(item.id for item in items)
    for si in preferred:
        assert si.pack_price > 0
        assert si.pack_price == si.pack_price.quantize(Decimal("0.001"))

    for model in (Batch, StockMovement, Receipt):
        count = db_session.scalar(
            select(func.count()).select_from(model).where(model.business_id == business_id)
        )
        assert count == 0

    # One par level per item, its three levels in order (D76, D83).
    par_levels = db_session.scalars(
        select(ParLevel).where(ParLevel.business_id == business_id)
    ).all()
    assert sorted(p.item_id for p in par_levels) == sorted(item.id for item in items)
    for p in par_levels:
        assert 0 <= p.low_stock_level <= p.reorder_point < p.order_up_to
        assert p.low_stock_level < p.reorder_point


# --- Gate 2 (D53, D88) ---------------------------------------------------------

GATE_DAYS = 14

# Usage movements whose request was not reversed (a reversed mistake doesn't count).
LIVE_USAGE = """
    SELECT m.* FROM stock_movements AS m
    WHERE m.business_id = :business_id AND m.type = 'usage'
      AND NOT EXISTS (SELECT 1 FROM stock_movements AS r WHERE r.reverses_id = m.id)
"""


def ledger_count(db: Session, business_id, sql: str) -> int:
    return db.scalar(text(sql), {"business_id": business_id})


def test_gate_2_three_venues_reconcile_and_run_the_rules_on_their_own(db_session: Session):
    """Each venue for 14 days, with the CLI's seeds: each reconciles and runs
    the rules on its own, and across the three every path happened (D88)."""
    paths: Counter[str] = Counter()
    for index, venue in enumerate(VENUES):
        business_id = seed(db_session, venue)
        summary = simulate(
            db_session,
            business_id,
            venue=venue,
            days=GATE_DAYS,
            end_date=END_DATE,
            random_seed=DEFAULT_RANDOM_SEED + index,
        )

        assert reconcile(db_session, business_id) == [], venue.name
        assert_the_ledger_is_the_simulators(db_session, business_id, summary)
        assert_the_rules_ran(db_session, business_id, venue, summary.first_day)
        paths.update(paths_taken(db_session, business_id))

    # Across the three venues, every path gates 1 and 2 check (D81, D88).
    required = {
        "settle pair",
        "usage split across batches",
        "tracked shortfall",
        "reversal",
        "waste expired",
        "waste spoiled",
        "alert low_stock",
        "alert expiring",
        "alert expired",
        "alert negative_stock",
        "expired waste after its alert",
    }
    assert {path for path in required if paths[path] == 0} == set(), paths


def test_the_kitchen_receipt_pack_prices_have_at_most_two_decimals(db_session: Session):
    """D101: drifted pack prices are rounded to the currency's minor unit, the
    fils for the kitchen's dirhams."""
    business_id = seed(db_session, KITCHEN)
    simulate(db_session, business_id, venue=KITCHEN, days=7, end_date=END_DATE)

    prices = db_session.scalars(
        select(ReceiptLine.pack_price).where(ReceiptLine.business_id == business_id)
    ).all()
    seeded = set(
        db_session.scalars(
            select(SupplierItem.pack_price).where(SupplierItem.business_id == business_id)
        )
    )
    assert len(prices) > 50
    assert set(prices) - seeded, "no price drifted"
    assert [price for price in prices if price != price.quantize(Decimal("0.01"))] == []


def assert_the_ledger_is_the_simulators(db: Session, business_id, summary) -> None:
    """Every movement is the simulator's, with no user, dated within the run,
    and the summary's counts agree with the ledger."""
    others = db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(
            StockMovement.business_id == business_id,
            or_(StockMovement.source != "simulator", StockMovement.user_id.is_not(None)),
        )
    )
    assert others == 0
    first, last = local_dates(db, business_id)
    assert summary.opening_day == END_DATE - timedelta(days=GATE_DAYS)
    assert summary.opening_day <= first and last <= END_DATE
    by_type = dict(
        db.execute(
            select(StockMovement.type, func.count())
            .where(StockMovement.business_id == business_id)
            .group_by(StockMovement.type)
        ).all()
    )
    assert by_type == dict(summary.movements_by_type)


def assert_the_rules_ran(db: Session, business_id, venue: Venue, first_day: date) -> None:
    """D81, per venue: alerts raised, the alerts date at the end date, orders
    sent on order days and received against them, and a count variance."""
    local = ZoneInfo(venue.timezone)
    raised = set(db.scalars(select(Alert.type).where(Alert.business_id == business_id)))
    assert {"low_stock", "expiring"} <= raised, (venue.name, raised)
    assert db.get(Business, business_id).alerts_swept_on == END_DATE

    # Orders were sent on their supplier's order days, and each receipt after
    # day 0 names an order and came its lead time after the order was sent, or
    # a day later when it was late (D77).
    specs = {spec.name: spec for spec in venue.suppliers}
    orders = db.execute(
        select(PurchaseOrder, Supplier.name)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .where(PurchaseOrder.business_id == business_id, PurchaseOrder.status != "draft")
    ).all()
    assert len(orders) >= 10, venue.name
    by_id = {}
    for order, supplier in orders:
        spec = specs[supplier]
        sent_on = order.sent_at.astimezone(local).date()
        assert sent_on.weekday() in spec.order_weekdays, (supplier, sent_on)
        by_id[order.id] = (order, spec, sent_on)
    receipts = db.scalars(
        select(Receipt).where(
            Receipt.business_id == business_id,
            Receipt.received_at >= datetime.combine(first_day, time(0), local),
        )
    ).all()
    assert receipts and all(receipt.po_id is not None for receipt in receipts)
    for receipt in receipts:
        order, spec, sent_on = by_id[receipt.po_id]
        assert order.status == "received"
        days = (receipt.received_at.astimezone(local).date() - sent_on).days
        assert days in (spec.lead_time_days, spec.lead_time_days + 1)

    # A count found a variance (D78).
    variances = db.scalar(
        select(func.count())
        .select_from(StockCountLine)
        .where(
            StockCountLine.business_id == business_id,
            StockCountLine.counted_qty != StockCountLine.expected_qty,
        )
    )
    assert variances >= 1, venue.name


def paths_taken(db: Session, business_id) -> Counter[str]:
    """How often each path the gate looks for happened in one business."""
    paths: Counter[str] = Counter()
    paths["settle pair"] = ledger_count(
        db,
        business_id,
        """
        SELECT count(*) FROM (
            SELECT client_id FROM stock_movements
            WHERE business_id = :business_id AND type = 'settle'
            GROUP BY client_id
            HAVING bool_or(qty_delta > 0) AND bool_or(qty_delta < 0)
        ) AS receipts_that_settled
        """,
    )
    paths["usage split across batches"] = ledger_count(
        db,
        business_id,
        f"""
        SELECT count(*) FROM (
            SELECT client_id FROM ({LIVE_USAGE}) AS u
            GROUP BY client_id HAVING count(DISTINCT batch_id) > 1
        ) AS splits
        """,
    )
    paths["tracked shortfall"] = ledger_count(
        db,
        business_id,
        f"""
        SELECT count(*) FROM ({LIVE_USAGE}) AS u
        JOIN batches AS b ON b.id = u.batch_id
        JOIN items AS i ON i.id = u.item_id
        WHERE b.is_default AND i.tracks_batches
        """,
    )
    for movement_type, reason, count in db.execute(
        select(StockMovement.type, StockMovement.reason, func.count())
        .where(
            StockMovement.business_id == business_id,
            StockMovement.type.in_(["reversal", "waste"]),
        )
        .group_by(StockMovement.type, StockMovement.reason)
    ):
        paths["reversal" if movement_type == "reversal" else f"waste {reason}"] += count
    for alert_type, count in db.execute(
        select(Alert.type, func.count())
        .where(Alert.business_id == business_id)
        .group_by(Alert.type)
    ):
        paths[f"alert {alert_type}"] += count
    paths["expired waste after its alert"] = db.scalar(
        select(func.count())
        .select_from(StockMovement)
        .join(
            Alert,
            (Alert.business_id == StockMovement.business_id)
            & (Alert.batch_id == StockMovement.batch_id)
            & (Alert.type == "expired")
            & (Alert.raised_at <= StockMovement.occurred_at),
        )
        .where(
            StockMovement.business_id == business_id,
            StockMovement.type == "waste",
            StockMovement.reason == "expired",
        )
    )
    return paths


# --- Determinism, default end date, failures ------------------------------------


def natural_ledger(db: Session, business_id) -> dict[str, list[tuple]]:
    """Movements, receipt lines, batches and balances, with ids and server times
    replaced by natural keys, so two runs can be compared."""
    skus = dict(db.execute(select(Item.id, Item.sku).where(Item.business_id == business_id)).all())
    batch_keys = {
        batch.id: (skus[batch.item_id], "default")
        if batch.is_default
        else (skus[batch.item_id], batch.expires_on, batch.received_at, batch.lot_code)
        for batch in db.scalars(select(Batch).where(Batch.business_id == business_id))
    }
    batches = [
        (batch_keys[b.id], b.unit_cost)
        for b in db.scalars(select(Batch).where(Batch.business_id == business_id))
    ]
    line_keys = {}
    lines = []
    rows = db.execute(
        select(ReceiptLine, Receipt, Supplier.name)
        .join(Receipt, Receipt.id == ReceiptLine.receipt_id)
        .join(Supplier, Supplier.id == Receipt.supplier_id)
        .where(ReceiptLine.business_id == business_id)
    ).all()
    for line, receipt, supplier in rows:
        key = (supplier, receipt.invoice_ref, skus[line.item_id])
        line_keys[line.id] = key
        lines.append(
            (
                key,
                receipt.received_at,
                line.qty_packs,
                line.qty_base,
                line.pack_price,
                line.unit_cost,
                line.expires_on,
                line.lot_code,
                batch_keys[line.batch_id],
            )
        )
    movements = db.scalars(
        select(StockMovement).where(StockMovement.business_id == business_id)
    ).all()
    movement_keys = {m.id: (m.client_id, m.client_seq) for m in movements}
    movement_rows = [
        (
            m.client_id,
            m.client_seq,
            skus[m.item_id],
            batch_keys[m.batch_id],
            m.qty_delta,
            m.type,
            m.source,
            m.reason,
            m.unit_cost,
            line_keys.get(m.receipt_line_id),
            movement_keys.get(m.reverses_id),
            m.occurred_at,
            m.user_id,
        )
        for m in movements
    ]
    balances = [
        (batch_keys[b.batch_id], b.qty_on_hand, b.last_movement_at)
        for b in db.scalars(select(StockBalance).where(StockBalance.business_id == business_id))
    ]
    alerts = [
        (a.type, a.status, skus[a.item_id], batch_keys.get(a.batch_id), a.raised_at, a.resolved_at)
        for a in db.scalars(select(Alert).where(Alert.business_id == business_id))
    ]
    suppliers = dict(
        db.execute(
            select(Supplier.id, Supplier.name).where(Supplier.business_id == business_id)
        ).all()
    )
    order_keys = {
        o.id: (suppliers[o.supplier_id], o.status, o.sent_at, o.received_at, o.cancelled_at)
        for o in db.scalars(select(PurchaseOrder).where(PurchaseOrder.business_id == business_id))
    }
    order_lines = [
        (order_keys[line.po_id], skus[line.item_id], line.suggested_packs, line.qty_packs)
        for line in db.scalars(
            select(PurchaseOrderLine).where(PurchaseOrderLine.business_id == business_id)
        )
    ]
    counts = {
        c.id: (c.counted_at, c.client_id)
        for c in db.scalars(select(StockCount).where(StockCount.business_id == business_id))
    }
    count_lines = [
        (counts[line.count_id], batch_keys[line.batch_id], line.expected_qty, line.counted_qty)
        for line in db.scalars(
            select(StockCountLine).where(StockCountLine.business_id == business_id)
        )
    ]
    return {
        "movements": sorted(movement_rows, key=repr),
        "receipt_lines": sorted(lines, key=repr),
        "batches": sorted(batches, key=repr),
        "balances": sorted(balances, key=repr),
        "alerts": sorted(alerts, key=repr),
        "orders": sorted(order_keys.values(), key=repr),
        "order_lines": sorted(order_lines, key=repr),
        "count_lines": sorted(count_lines, key=repr),
    }


def run_in_own_transaction(
    engine: Engine, venue: Venue = CAFE, **options
) -> dict[str, list[tuple]]:
    """Seed and simulate on a fresh connection, read the ledger, then roll it all back.

    A savepoint inside db_session wouldn't do: each day's commit releases it.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            business_id = seed(session, venue)
            simulate(session, business_id, venue=venue, **options)
            return natural_ledger(session, business_id)
        finally:
            session.close()
            transaction.rollback()


# The café, and the kitchen, whose local times are in another timezone (D86).
@pytest.mark.parametrize("venue", [CAFE, KITCHEN], ids=lambda venue: venue.name)
def test_the_same_seed_and_end_date_give_an_identical_ledger(engine: Engine, venue: Venue):
    options = {"days": 5, "end_date": END_DATE, "random_seed": 42}
    first = run_in_own_transaction(engine, venue, **options)
    second = run_in_own_transaction(engine, venue, **options)

    assert len(first["movements"]) > 100
    assert first["alerts"] and first["orders"] and first["count_lines"]
    assert first == second


def test_without_an_end_date_the_run_ends_today_and_stops_at_the_current_time(
    db_session: Session,
):
    business_id = seed(db_session, CAFE)
    summary = simulate(db_session, business_id, venue=CAFE, days=1)

    today = db_session.scalar(text("SELECT (now() AT TIME ZONE 'Asia/Amman')::date"))
    assert summary.end_date == today
    assert summary.opening_day == today - timedelta(days=1)
    first, last = local_dates(db_session, business_id)
    assert first == summary.opening_day and last <= today

    # D82: nothing is dated after the time the last day stopped at.
    stopped_at = summary.stopped_at
    assert stopped_at is not None
    assert stopped_at <= db_session.scalar(text("SELECT clock_timestamp()"))
    latest = [
        select(func.max(StockMovement.occurred_at)).where(StockMovement.business_id == business_id),
        select(func.max(Alert.raised_at)).where(Alert.business_id == business_id),
        select(func.max(PurchaseOrder.sent_at)).where(PurchaseOrder.business_id == business_id),
        select(func.max(StockCount.counted_at)).where(StockCount.business_id == business_id),
    ]
    for query in latest:
        when = db_session.scalar(query)
        assert when is None or when <= stopped_at


def test_an_end_date_in_the_future_is_refused(db_session: Session):
    business_id = seed(db_session, CAFE)
    today = db_session.scalar(text("SELECT (now() AT TIME ZONE 'Asia/Amman')::date"))

    with pytest.raises(ValueError, match="in the future"):
        simulate(db_session, business_id, venue=CAFE, days=1, end_date=today + timedelta(days=1))

    assert local_dates(db_session, business_id) == (None, None)


def test_a_last_day_stopped_at_the_current_time_matches_the_full_run_up_to_it(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
):
    """D82, with the clock pinned: END_DATE stands in for today, at 15:00.

    Everything up to the cutoff is what the full run wrote, because the
    events after it were still planned and drew their random numbers; nothing
    after it exists, the evening's orders and the Saturday count included.
    """
    options = {"days": 2, "end_date": END_DATE, "random_seed": 7}
    full = run_in_own_transaction(engine, **options)
    cutoff = datetime.combine(END_DATE, time(15), AMMAN)
    monkeypatch.setattr(
        simulator._Run, "_cutoff", lambda self, day: cutoff if day == END_DATE else None
    )
    stopped = run_in_own_transaction(engine, **options)

    # Movements rows: (client_id, client_seq, sku, batch, qty, type, ..., occurred_at
    # at 11, ...). A reversal is dated at its original (D36), so one made just
    # after the cutoff can carry an earlier occurred_at: compare the others.
    def before_cutoff(rows):
        return [row for row in rows if row[11] <= cutoff and row[5] != "reversal"]

    assert set(stopped["movements"]) <= set(full["movements"])
    assert before_cutoff(stopped["movements"]) == before_cutoff(full["movements"])
    assert len(stopped["movements"]) < len(full["movements"])
    assert all(row[4] <= cutoff for row in stopped["alerts"])  # raised_at
    assert all(row[2] is None or row[2] <= cutoff for row in stopped["orders"])  # sent_at
    assert {row[0][0] for row in full["count_lines"]} - {
        row[0][0] for row in stopped["count_lines"]
    } == {datetime.combine(END_DATE, CAFE.count_time, AMMAN)}


def test_the_simulator_analyzes_after_the_opening_day_and_then_every_few_days(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    """D75: the planner needs statistics for the tables the run is filling.

    Every 2 days here instead of every 7, so four simulated days show it.
    """
    monkeypatch.setattr(simulator, "ANALYZE_EVERY_DAYS", 2)
    business_id = seed(db_session, CAFE)

    with recorded_statements(db_session) as statements:
        simulate(db_session, business_id, venue=CAFE, days=4, end_date=END_DATE)

    # After day 0 (the opening stock) and day 2, of days 0 to 4. Not after
    # day 4: it is the last, so nothing is left running after the final commit.
    assert statements.count("ANALYZE") == 2
    assert statements[-1] != "ANALYZE"


def test_a_failed_day_is_rolled_back_and_earlier_days_are_kept(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
):
    day_2 = END_DATE

    def use_failing_on_day_2(*args, occurred_at, **kwargs):
        if occurred_at.astimezone(AMMAN).date() == day_2:
            raise DomainError("the test refuses this usage")
        return real_use(*args, occurred_at=occurred_at, **kwargs)

    real_use = simulator.use
    monkeypatch.setattr(simulator, "use", use_failing_on_day_2)
    business_id = seed(db_session, CAFE)

    with pytest.raises(SimulationError, match=r"^day 2 \(2026-10-31\): use .* at .*refuses"):
        simulate(db_session, business_id, venue=CAFE, days=2, end_date=END_DATE)

    # Days 0 and 1 are committed, day 2 left nothing, and the ledger reconciles.
    assert local_dates(db_session, business_id) == (date(2026, 10, 29), date(2026, 10, 30))
    assert reconcile(db_session, business_id) == []
