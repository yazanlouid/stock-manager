"""Domain rules 1 to 7 over random sequences of stock requests (Hypothesis).

A RuleBasedStateMachine posts random receives, usages, wastes, reversals and
retries for three items: milk (batches and expiry), eggs (batches, no expiry)
and flour (no batch tracking), with expiry dates a few days either side of the
usage dates and times near midnight in Amman.

Rules 2, 3 and 7 are about the moment of a write, so the rules check them
against a snapshot taken just before it. Rules 1, 4, 5 and 6 are about the
state, so the invariants check them after every step.

Each example opens its own connection and transaction and rolls it back in
teardown, because function-scoped fixtures don't reset between examples
(CLAUDE.md). Nothing is ever deleted from the ledger.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from uuid import UUID

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.domain.errors import DomainError
from app.domain.fefo import BatchSnapshot, allocate_usage
from app.domain.stock import ReceiptLineInput, rebuild_balances, receive, reverse, use, waste
from app.models import Batch, ReceiptLine, StockBalance, StockMovement
from tests.builders import add, build_catalogue, new_item, new_unit

# Asia/Amman (the test business's timezone) has been UTC+3 all year since
# October 2022. A fixed offset keeps Python's zoneinfo, which needs the tzdata
# package on Windows, out of the test; use() itself converts in Postgres.
AMMAN_OFFSET = timedelta(hours=3)
FIRST_DAY = date(2026, 11, 1)
# Stands in for a default batch that doesn't exist yet when a snapshot is taken.
NEW_DEFAULT = UUID(int=0)

ITEMS = st.integers(0, 2)  # milk, eggs, flour
HOURS = st.integers(0, 23)
MINUTES = st.sampled_from([0, 30])
WASTE_REASONS = st.sampled_from(["expired", "spoiled", "damaged", "other"])


def thousandths(low: int, high: int) -> st.SearchStrategy[Decimal]:
    """Decimals from low/1000 to high/1000, with three decimal places."""
    return st.integers(low, high).map(lambda n: Decimal(n).scaleb(-3))


@dataclass
class Request:
    """A request the machine posted, kept for retries and reversals."""

    kind: str
    client_id: UUID
    # Posts the same request again, with the same arguments and client_id.
    call: Callable
    movements: tuple[StockMovement, ...]


@dataclass(frozen=True)
class Snapshot:
    """A batch as it was just before a write."""

    batch: BatchSnapshot
    unit_cost: Decimal | None


class StockLedgerMachine(RuleBasedStateMachine):
    def __init__(self, engine: Engine) -> None:
        super().__init__()
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection, join_transaction_mode="create_savepoint")

        self.cat = build_catalogue(self.db)  # its item is milk, by the 1000 ml bottle
        business_id = self.cat.business.id
        eggs = add(
            self.db, new_item(business_id, name="Eggs", base_unit="piece", tracks_batches=True)
        )
        flour = add(self.db, new_item(business_id, name="Flour", base_unit="g"))
        self.items = [
            (self.cat.item, self.cat.unit),
            (eggs, add(self.db, new_unit(eggs, name="tray", factor_to_base=Decimal("30")))),
            (flour, add(self.db, new_unit(flour, name="bag", factor_to_base=Decimal("1000")))),
        ]

        self.day = FIRST_DAY
        self.requests: list[Request] = []
        self.reversed: set[UUID] = set()

    def teardown(self) -> None:
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    # --- Helpers ----------------------------------------------------------------

    @property
    def business_id(self) -> UUID:
        return self.cat.business.id

    def at(self, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(self.day, time(hour, minute), UTC)

    def snapshots(self, item_id: UUID) -> dict[UUID, Snapshot]:
        """The item's batches at the location, as stored right now."""
        rows = self.db.execute(
            select(
                Batch.id,
                Batch.expires_on,
                Batch.received_at,
                StockBalance.qty_on_hand,
                Batch.is_default,
                Batch.unit_cost,
            )
            .join(StockBalance, StockBalance.batch_id == Batch.id)
            .where(
                Batch.business_id == self.business_id,
                Batch.location_id == self.cat.location.id,
                Batch.item_id == item_id,
            )
        )
        return {row.id: Snapshot(BatchSnapshot(*row[:5]), row.unit_cost) for row in rows}

    def default_of(self, snapshots: dict[UUID, Snapshot]) -> Snapshot | None:
        return next((s for s in snapshots.values() if s.batch.is_default), None)

    def movement_count(self) -> int:
        return self.db.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.business_id == self.business_id)
        )

    def remember(self, kind: str, client_id: UUID, call: Callable, movements) -> None:
        self.requests.append(Request(kind, client_id, call, tuple(movements)))

    # --- Rules ------------------------------------------------------------------

    @rule(days=st.integers(1, 2))
    def next_day(self, days: int) -> None:
        self.day += timedelta(days=days)

    @rule(
        lines=st.lists(
            st.tuples(ITEMS, st.integers(1, 12), thousandths(0, 5000), st.integers(-2, 4)),
            min_size=1,
            max_size=3,
        ),
        hour=HOURS,
    )
    def receive_goods(self, lines: list[tuple[int, int, Decimal, int]], hour: int) -> None:
        inputs = []
        received_qty: dict[UUID, Decimal] = defaultdict(Decimal)
        for index, quarters, pack_price, expiry_offset in lines:
            item, unit = self.items[index]
            qty_packs = Decimal(quarters) / 4
            expires_on = self.day + timedelta(days=expiry_offset) if item.tracks_expiry else None
            inputs.append(ReceiptLineInput(item.id, unit.id, qty_packs, pack_price, expires_on))
            received_qty[item.id] += qty_packs * unit.factor_to_base
        tracked = {item.id for item, _ in self.items if item.tracks_batches} & received_qty.keys()
        default_before = {}
        for item_id in tracked:
            default = self.default_of(self.snapshots(item_id))
            default_before[item_id] = default.batch.on_hand if default else Decimal(0)

        client_id = uuid.uuid4()
        call = partial(
            receive,
            self.db,
            self.business_id,
            location_id=self.cat.location.id,
            supplier_id=self.cat.supplier.id,
            received_at=self.at(hour),
            invoice_ref=None,
            lines=inputs,
            client_id=client_id,
            source="simulator",
        )
        result = call()
        self.remember("receive", client_id, call, result.movements)

        # Rule 7: a receive carries its line's cost, and both halves of a settle
        # pair carry the cost of the receive they follow.
        line_costs = dict(
            self.db.execute(
                select(ReceiptLine.id, ReceiptLine.unit_cost).where(
                    ReceiptLine.receipt_id == result.receipt.id
                )
            ).all()
        )
        receive_cost = None
        for movement in result.movements:
            if movement.type == "receive":
                receive_cost = line_costs[movement.receipt_line_id]
            assert movement.unit_cost == receive_cost

        # Rule 4: the receipt settles a batch-tracked item's negative default
        # batch first, as far as the quantity received covers it.
        for item_id, before in default_before.items():
            expected = before if before >= 0 else min(Decimal(0), before + received_qty[item_id])
            assert self.default_of(self.snapshots(item_id)).batch.on_hand == expected

    @rule(index=ITEMS, quantity=thousandths(1, 2_500_000), hour=HOURS, minute=MINUTES)
    def use_stock(self, index: int, quantity: Decimal, hour: int, minute: int) -> None:
        item, _ = self.items[index]
        occurred_at = self.at(hour, minute)
        before = self.snapshots(item.id)
        if self.default_of(before) is None:
            before[NEW_DEFAULT] = Snapshot(
                BatchSnapshot(NEW_DEFAULT, None, occurred_at, Decimal(0), True), None
            )
        # Rules 2 and 3: the split is FEFO over the balances just before the
        # usage, as of its date in Amman, from batches received by then (D50).
        expected = allocate_usage(
            [s.batch for s in before.values()],
            quantity,
            (occurred_at + AMMAN_OFFSET).date(),
            occurred_at,
        )

        client_id = uuid.uuid4()
        call = partial(
            use,
            self.db,
            self.business_id,
            location_id=self.cat.location.id,
            item_id=item.id,
            quantity=quantity,
            occurred_at=occurred_at,
            client_id=client_id,
            source="simulator",
        )
        result = call()
        self.remember("use", client_id, call, result.movements)

        def key(batch_id: UUID) -> UUID:
            return batch_id if batch_id in before else NEW_DEFAULT

        assert [(key(m.batch_id), -m.qty_delta) for m in result.movements] == [
            (a.batch_id, a.quantity) for a in expected.allocations
        ]
        assert result.shortfall == expected.shortfall
        # Rule 7: each usage carries its batch's cost at the time.
        for movement in result.movements:
            assert movement.unit_cost == before[key(movement.batch_id)].unit_cost

    @rule(index=ITEMS, reason=WASTE_REASONS, hour=HOURS, data=st.data())
    def waste_stock(self, index: int, reason: str, hour: int, data: st.DataObject) -> None:
        item, _ = self.items[index]
        candidates = sorted(
            (s for s in self.snapshots(item.id).values() if s.batch.on_hand > 0),
            key=lambda s: s.batch.id,
        )
        if not candidates:
            return
        target = data.draw(st.sampled_from(candidates))
        # Sometimes more than the batch holds, which must be refused.
        quantity = data.draw(thousandths(1, int(target.batch.on_hand * 1000) + 500))
        batch_id = target.batch.id if item.tracks_batches else None
        occurred_at = self.at(hour)

        client_id = uuid.uuid4()
        call = partial(
            waste,
            self.db,
            self.business_id,
            location_id=self.cat.location.id,
            item_id=item.id,
            batch_id=batch_id,
            quantity=quantity,
            reason=reason,
            occurred_at=occurred_at,
            client_id=client_id,
            source="simulator",
        )
        # D50: waste can't name a batch received after it. A default batch's
        # received_at is when it was created, so the rule never applies to it.
        if not target.batch.is_default and target.batch.received_at > occurred_at:
            count = self.movement_count()
            with pytest.raises(DomainError, match="received at"):
                call()
            assert self.movement_count() == count
            return
        if quantity > target.batch.on_hand:
            count = self.movement_count()
            with pytest.raises(DomainError, match="on hand"):
                call()
            assert self.movement_count() == count
            return

        result = call()
        self.remember("waste", client_id, call, result.movements)
        (movement,) = result.movements
        assert (movement.batch_id, movement.qty_delta) == (target.batch.id, -quantity)
        # Rule 7: waste carries its batch's cost at the time.
        assert movement.unit_cost == target.unit_cost

    @precondition(lambda self: any(r.kind in ("use", "waste") for r in self.requests))
    @rule(data=st.data())
    def reverse_request(self, data: st.DataObject) -> None:
        original = data.draw(
            st.sampled_from([r for r in self.requests if r.kind in ("use", "waste")])
        )
        client_id = uuid.uuid4()
        call = partial(
            reverse,
            self.db,
            self.business_id,
            original_client_id=original.client_id,
            client_id=client_id,
            source="manual",
        )
        if original.client_id in self.reversed:
            # Rule 6: each movement can be reversed once.
            with pytest.raises(DomainError, match="already reversed"):
                call()
            return

        result = call()
        self.reversed.add(original.client_id)
        self.remember("reverse", client_id, call, result.movements)
        # Rules 6 and 7: the negated quantity at the original's batch, cost and time.
        assert [
            (m.reverses_id, m.batch_id, m.qty_delta, m.unit_cost, m.occurred_at)
            for m in result.movements
        ] == [
            (m.id, m.batch_id, -m.qty_delta, m.unit_cost, m.occurred_at) for m in original.movements
        ]

    @precondition(lambda self: any(r.kind in ("receive", "reverse") for r in self.requests))
    @rule(data=st.data())
    def reverse_what_cant_be_reversed(self, data: st.DataObject) -> None:
        original = data.draw(
            st.sampled_from([r for r in self.requests if r.kind in ("receive", "reverse")])
        )
        with pytest.raises(DomainError, match="only usage and waste can be reversed"):
            reverse(
                self.db,
                self.business_id,
                original_client_id=original.client_id,
                client_id=uuid.uuid4(),
                source="manual",
            )

    @precondition(lambda self: self.requests)
    @rule(data=st.data())
    def retry_request(self, data: st.DataObject) -> None:
        request = data.draw(st.sampled_from(self.requests))
        count = self.movement_count()

        result = request.call()

        assert result.replayed
        assert [m.id for m in result.movements] == [m.id for m in request.movements]
        assert self.movement_count() == count

    # --- Invariants -------------------------------------------------------------

    @invariant()
    def balances_equal_the_sum_of_their_movements(self) -> None:
        """Rule 1, checked directly and through rebuild_balances."""
        sums = dict(
            self.db.execute(
                select(StockMovement.batch_id, func.sum(StockMovement.qty_delta))
                .where(StockMovement.business_id == self.business_id)
                .group_by(StockMovement.batch_id)
            ).all()
        )
        balances = dict(
            self.db.execute(
                select(StockBalance.batch_id, StockBalance.qty_on_hand).where(
                    StockBalance.business_id == self.business_id
                )
            ).all()
        )
        batch_ids = set(
            self.db.scalars(select(Batch.id).where(Batch.business_id == self.business_id))
        )
        assert balances.keys() == batch_ids
        assert balances == {batch_id: sums.get(batch_id, Decimal(0)) for batch_id in batch_ids}
        assert rebuild_balances(self.db, self.business_id) == []

    @invariant()
    def only_default_batches_go_negative(self) -> None:
        """Rule 4."""
        negative = self.db.scalar(
            select(func.count())
            .select_from(StockBalance)
            .join(Batch, Batch.id == StockBalance.batch_id)
            .where(
                StockBalance.business_id == self.business_id,
                ~Batch.is_default,
                StockBalance.qty_on_hand < 0,
            )
        )
        assert negative == 0

    @invariant()
    def one_default_batch_per_item_and_location(self) -> None:
        """Rule 5: every item with batches at a location has exactly one default."""
        defaults = self.db.scalars(
            select(func.count().filter(Batch.is_default))
            .where(Batch.business_id == self.business_id)
            .group_by(Batch.location_id, Batch.item_id)
        ).all()
        assert all(count == 1 for count in defaults)

    @invariant()
    def reversed_requests_net_to_zero(self) -> None:
        """Rule 6: no movement is reversed twice, and a reversed request nets to
        zero in quantity and value on every batch."""
        reverses_ids = self.db.scalars(
            select(StockMovement.reverses_id).where(
                StockMovement.business_id == self.business_id,
                StockMovement.reverses_id.is_not(None),
            )
        ).all()
        assert len(reverses_ids) == len(set(reverses_ids))

        for request in self.requests:
            if request.client_id not in self.reversed:
                continue
            ids = [m.id for m in request.movements]
            rows = self.db.execute(
                select(
                    StockMovement.batch_id, StockMovement.qty_delta, StockMovement.unit_cost
                ).where(
                    StockMovement.business_id == self.business_id,
                    StockMovement.id.in_(ids) | StockMovement.reverses_id.in_(ids),
                )
            ).all()
            qty: dict[UUID, Decimal] = defaultdict(Decimal)
            value: dict[UUID, Decimal] = defaultdict(Decimal)
            for batch_id, qty_delta, unit_cost in rows:
                qty[batch_id] += qty_delta
                value[batch_id] += qty_delta * (unit_cost or 0)
            assert all(q == 0 for q in qty.values())
            assert all(v == 0 for v in value.values())


def test_ledger_rules_hold_for_random_requests(engine: Engine) -> None:
    run_state_machine_as_test(
        lambda: StockLedgerMachine(engine),
        settings=settings(max_examples=40, stateful_step_count=30, deadline=None),
    )
