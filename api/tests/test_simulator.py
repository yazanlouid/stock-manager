"""The seed, the simulator and gate 1 (30 café days reconcile exactly).

The gate test runs the whole 30 days once, on db_session: each day's commit
only releases a savepoint, and the test's transaction is rolled back at the
end. Its path checks read the ledger, not the simulator's counters.
"""

from datetime import date, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, or_, select, text
from sqlalchemy.orm import Session

from app.domain.errors import DomainError
from app.domain.reconcile import reconcile
from app.models import (
    Batch,
    Business,
    Item,
    Location,
    Receipt,
    ReceiptLine,
    StockBalance,
    StockMovement,
    Supplier,
    SupplierItem,
    User,
)
from app.sim import simulator
from app.sim.cafe import CAFE
from app.sim.seed import PLACEHOLDER_PASSWORD_HASH, seed
from app.sim.simulator import SimulationError, simulate

END_DATE = date(2026, 10, 31)
# Asia/Amman has been UTC+3 all year since October 2022 (see test_stock_properties.py).
AMMAN = timezone(timedelta(hours=3))


def local_dates(db: Session, business_id) -> tuple[date, date]:
    """The first and last occurred_at of the business's movements, as dates in Asia/Amman."""
    return db.execute(
        text(
            "SELECT min((occurred_at AT TIME ZONE 'Asia/Amman')::date),"
            " max((occurred_at AT TIME ZONE 'Asia/Amman')::date)"
            " FROM stock_movements WHERE business_id = :business_id"
        ),
        {"business_id": business_id},
    ).one()


# --- Seed -----------------------------------------------------------------------


def test_seed_writes_the_cafe_catalogue_and_no_stock(db_session: Session):
    business_id = seed(db_session, CAFE)

    business = db_session.get(Business, business_id)
    assert (business.timezone, business.currency) == ("Asia/Amman", "JOD")
    locations = db_session.scalars(select(Location).where(Location.business_id == business_id))
    assert [location.kind for location in locations] == ["branch"]

    users = db_session.scalars(select(User).where(User.business_id == business_id)).all()
    assert sorted(user.role for user in users) == ["owner", "staff"]
    for user in users:
        assert user.password_hash == PLACEHOLDER_PASSWORD_HASH
        assert user.password_hash.startswith("!")
        assert user.email == user.email.lower() and user.email.endswith(".example")

    items = db_session.scalars(select(Item).where(Item.business_id == business_id)).all()
    assert 35 <= len(items) <= 45
    kinds = {(item.tracks_batches, item.tracks_expiry) for item in items}
    # Batches with expiry, batches by lot without expiry, and no batch tracking.
    assert kinds == {(True, True), (True, False), (False, False)}
    assert all(item.shelf_life_days for item in items if item.tracks_expiry)

    suppliers = db_session.scalar(
        select(func.count()).select_from(Supplier).where(Supplier.business_id == business_id)
    )
    assert suppliers == 6

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


# --- Gate 1 ---------------------------------------------------------------------

# Usage movements whose request was not reversed (a reversed mistake doesn't count).
LIVE_USAGE = """
    SELECT m.* FROM stock_movements AS m
    WHERE m.business_id = :business_id AND m.type = 'usage'
      AND NOT EXISTS (SELECT 1 FROM stock_movements AS r WHERE r.reverses_id = m.id)
"""


def ledger_count(db: Session, business_id, sql: str) -> int:
    return db.scalar(text(sql), {"business_id": business_id})


def test_gate_1_thirty_cafe_days_reconcile(db_session: Session):
    business_id = seed(db_session, CAFE)
    summary = simulate(db_session, business_id, venue=CAFE, days=30, end_date=END_DATE)

    assert reconcile(db_session, business_id) == []

    # Every movement is the simulator's, with no user.
    others = db_session.scalar(
        select(func.count())
        .select_from(StockMovement)
        .where(
            StockMovement.business_id == business_id,
            or_(StockMovement.source != "simulator", StockMovement.user_id.is_not(None)),
        )
    )
    assert others == 0

    # Every occurred_at falls between day 0 and the end date, in Asia/Amman.
    first, last = local_dates(db_session, business_id)
    assert summary.opening_day == date(2026, 10, 1)
    assert summary.opening_day <= first and last <= END_DATE

    # The summary's counts agree with the ledger.
    by_type = dict(
        db_session.execute(
            select(StockMovement.type, func.count())
            .where(StockMovement.business_id == business_id)
            .group_by(StockMovement.type)
        ).all()
    )
    assert by_type == dict(summary.movements_by_type)

    # Each path the gate is meant to exercise happened at least once.
    settle_pairs = ledger_count(
        db_session,
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
    split_usages = ledger_count(
        db_session,
        business_id,
        f"""
        SELECT count(*) FROM (
            SELECT client_id FROM ({LIVE_USAGE}) AS u
            GROUP BY client_id HAVING count(DISTINCT batch_id) > 1
        ) AS splits
        """,
    )
    tracked_shortfalls = ledger_count(
        db_session,
        business_id,
        f"""
        SELECT count(*) FROM ({LIVE_USAGE}) AS u
        JOIN batches AS b ON b.id = u.batch_id
        JOIN items AS i ON i.id = u.item_id
        WHERE b.is_default AND i.tracks_batches
        """,
    )
    waste_reasons = set(
        db_session.scalars(
            select(StockMovement.reason)
            .where(StockMovement.business_id == business_id, StockMovement.type == "waste")
            .distinct()
        )
    )
    assert settle_pairs >= 1
    assert split_usages >= 1
    assert tracked_shortfalls >= 1
    assert {"expired", "spoiled"} <= waste_reasons
    assert by_type.get("reversal", 0) >= 1


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
    return {
        "movements": sorted(movement_rows, key=repr),
        "receipt_lines": sorted(lines, key=repr),
        "batches": sorted(batches, key=repr),
        "balances": sorted(balances, key=repr),
    }


def run_in_own_transaction(engine: Engine, **options) -> dict[str, list[tuple]]:
    """Seed and simulate on a fresh connection, read the ledger, then roll it all back.

    A savepoint inside db_session wouldn't do: each day's commit releases it.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            business_id = seed(session, CAFE)
            simulate(session, business_id, venue=CAFE, **options)
            return natural_ledger(session, business_id)
        finally:
            session.close()
            transaction.rollback()


def test_the_same_seed_and_end_date_give_an_identical_ledger(engine: Engine):
    options = {"days": 7, "end_date": END_DATE, "random_seed": 42}
    first = run_in_own_transaction(engine, **options)
    second = run_in_own_transaction(engine, **options)

    assert len(first["movements"]) > 100
    assert first == second


def test_without_an_end_date_the_run_ends_today_in_the_business_timezone(db_session: Session):
    business_id = seed(db_session, CAFE)
    summary = simulate(db_session, business_id, venue=CAFE, days=1)

    today = db_session.scalar(text("SELECT (now() AT TIME ZONE 'Asia/Amman')::date"))
    assert summary.end_date == today
    assert summary.opening_day == today - timedelta(days=1)
    assert local_dates(db_session, business_id) == (summary.opening_day, today)


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
