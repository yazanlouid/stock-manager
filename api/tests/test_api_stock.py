"""The stock endpoints: worked examples of receive, use, waste and reverse, and GET /stock.

The cat fixture's item is whole milk, which tracks batches and expiry, bought
by the 1000 ml bottle; the business is in Asia/Amman (UTC+3). Writes default
to 2 Nov (RECEIVED_AT and OCCURRED_AT in builders.py), and the API's clock is
pinned to noon UTC on 10 Nov unless a test pins another time.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.reconcile import reconcile
from app.models import Batch, Item, ItemUnit, StockBalance, StockMovement
from tests.builders import (
    RECEIVED_AT,
    Catalogue,
    add,
    api_headers,
    new_item,
    new_location,
    new_unit,
    receipt_json,
    receipt_line_json,
    reversal_json,
    usage_json,
    waste_json,
)


def nov(day: int, hour: int = 12, minute: int = 0) -> datetime:
    """A time in November 2026, in UTC."""
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


NOW = nov(10)


@pytest.fixture(autouse=True)
def pinned_now(client: TestClient, pin_now: Callable[[datetime], None]) -> None:
    pin_now(NOW)


@pytest.fixture
def flour(db_session: Session, cat: Catalogue) -> tuple[Item, ItemUnit]:
    """An item without batch tracking, bought by the 25 kg sack."""
    item = add(db_session, new_item(cat.business.id, name="Flour", base_unit="g"))
    return item, add(db_session, new_unit(item, name="sack", factor_to_base=Decimal("25000")))


# --- Helpers --------------------------------------------------------------------


def post(client: TestClient, path: str, body: dict[str, Any], cat: Catalogue) -> Any:
    return client.post(path, json=body, headers=api_headers(cat))


def milk_line(
    cat: Catalogue, packs: str, price: str = "0.850", expires_day: int = 12, lot: str | None = None
) -> dict[str, Any]:
    """Bottles of cat's milk expiring that day in November."""
    return receipt_line_json(cat.item, cat.unit, packs, price, date(2026, 11, expires_day), lot)


def receive(client: TestClient, cat: Catalogue, *lines: dict[str, Any]) -> dict[str, Any]:
    """Post a receipt and return its body; one batch per milk line."""
    response = post(client, "/receipts", receipt_json(cat, list(lines)), cat)
    assert response.status_code == 201, response.json()
    return response.json()


def batch_ids(receipt: dict[str, Any]) -> list[str]:
    """The batch each receive movement went into, in line order."""
    return [m["batch_id"] for m in receipt["movements"] if m["type"] == "receive"]


def default_batch_id(session: Session, location_id: uuid.UUID, item_id: uuid.UUID) -> str:
    return str(
        session.scalar(
            select(Batch.id).where(
                Batch.location_id == location_id, Batch.item_id == item_id, Batch.is_default
            )
        )
    )


def on_hand(session: Session, batch_id: str) -> Decimal:
    return session.scalar(
        select(StockBalance.qty_on_hand).where(StockBalance.batch_id == uuid.UUID(batch_id))
    )


def movement_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(StockMovement))


def stock(client: TestClient, cat: Catalogue, **params: Any) -> list[dict[str, Any]]:
    response = client.get("/stock", params=params, headers=api_headers(cat))
    assert response.status_code == 200, response.json()
    return response.json()


def entry_for(entries: list[dict[str, Any]], item: Item) -> dict[str, Any]:
    (entry,) = [e for e in entries if e["item_id"] == str(item.id)]
    return entry


# --- Receive --------------------------------------------------------------------


def test_a_receipt_with_a_tracked_and_an_untracked_line(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    flour_item, sack = flour
    body = receipt_json(
        cat,
        [
            milk_line(cat, "2", "0.850", expires_day=20, lot="L-20"),
            receipt_line_json(flour_item, sack, "1", "12.500"),
        ],
        invoice_ref="INV-1",
    )

    response = post(client, "/receipts", body, cat)

    assert response.status_code == 201
    data = response.json()
    assert data["replayed"] is False
    receipt = data["receipt"]
    assert receipt["location_id"] == str(cat.location.id)
    assert receipt["supplier_id"] == str(cat.supplier.id)
    assert receipt["invoice_ref"] == "INV-1"
    assert datetime.fromisoformat(receipt["received_at"]) == RECEIVED_AT

    milk_move, flour_move = data["movements"]
    assert [milk_move["client_seq"], flour_move["client_seq"]] == [0, 1]
    for move in (milk_move, flour_move):
        assert move["type"] == "receive"
        assert move["source"] == "manual"
        assert move["client_id"] == body["client_id"]
        assert move["receipt_line_id"] is not None
        assert "business_id" not in move and "user_id" not in move
    # Decimals are strings at the column's scale.
    assert (milk_move["qty_delta"], milk_move["unit_cost"]) == ("2000.000", "0.000850")
    assert (flour_move["qty_delta"], flour_move["unit_cost"]) == ("25000.000", "0.000500")

    # Milk gets a new batch with its expiry and lot; flour goes into its default batch.
    milk_batch = db_session.get(Batch, uuid.UUID(milk_move["batch_id"]))
    assert (milk_batch.is_default, milk_batch.expires_on, milk_batch.lot_code) == (
        False,
        date(2026, 11, 20),
        "L-20",
    )
    flour_batch = db_session.get(Batch, uuid.UUID(flour_move["batch_id"]))
    assert flour_batch.is_default
    assert flour_batch.unit_cost == Decimal("0.000500")


def test_json_numbers_are_read_exactly_and_written_back_as_strings(
    client: TestClient, cat: Catalogue
):
    # JSON numbers, not strings. They pass through a float on the way in, which
    # is exact for anything that fits numeric(14,3).
    line = milk_line(cat, "0", "0") | {"qty_packs": 1.5, "pack_price": 0.85}

    data = receive(client, cat, line)

    (move,) = data["movements"]
    assert (move["qty_delta"], move["unit_cost"]) == ("1500.000", "0.000850")


# --- Use ------------------------------------------------------------------------


def test_usage_splits_across_two_batches_earliest_expiry_first(
    client: TestClient, db_session: Session, cat: Catalogue
):
    later, sooner = batch_ids(
        receive(client, cat, milk_line(cat, "1", "0.900", 15), milk_line(cat, "2", "0.850", 12))
    )

    response = post(client, "/usage", usage_json(cat, "2500"), cat)

    assert response.status_code == 201
    data = response.json()
    assert [(m["batch_id"], m["qty_delta"], m["unit_cost"]) for m in data["movements"]] == [
        (sooner, "-2000.000", "0.000850"),
        (later, "-500.000", "0.000900"),
    ]
    assert [m["client_seq"] for m in data["movements"]] == [0, 1]
    assert data["shortfall"] == "0.000"
    assert (on_hand(db_session, sooner), on_hand(db_session, later)) == (0, 500)


def test_a_shortfall_posts_to_the_default_batch(
    client: TestClient, db_session: Session, cat: Catalogue
):
    (batch,) = batch_ids(receive(client, cat, milk_line(cat, "1")))

    response = post(client, "/usage", usage_json(cat, "1500"), cat)

    assert response.status_code == 201
    data = response.json()
    default = default_batch_id(db_session, cat.location.id, cat.item.id)
    assert [(m["batch_id"], m["qty_delta"]) for m in data["movements"]] == [
        (batch, "-1000.000"),
        (default, "-500.000"),
    ]
    assert data["shortfall"] == "500.000"
    assert on_hand(db_session, default) == -500


# --- Waste ----------------------------------------------------------------------


def test_waste_from_a_named_batch(client: TestClient, db_session: Session, cat: Catalogue):
    (batch,) = batch_ids(receive(client, cat, milk_line(cat, "2")))

    response = post(client, "/waste", waste_json(cat, "300", batch), cat)

    assert response.status_code == 201
    (move,) = response.json()["movements"]
    assert (move["type"], move["reason"], move["batch_id"]) == ("waste", "spoiled", batch)
    assert (move["qty_delta"], move["unit_cost"]) == ("-300.000", "0.000850")
    assert on_hand(db_session, batch) == 1700


def test_waste_of_an_untracked_item_comes_from_its_default_batch(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    flour_item, sack = flour
    (default,) = batch_ids(receive(client, cat, receipt_line_json(flour_item, sack, "1", "12.500")))

    body = waste_json(cat, "1000", None, item_id=flour_item.id, reason="damaged")
    response = post(client, "/waste", body, cat)

    assert response.status_code == 201
    (move,) = response.json()["movements"]
    assert (move["batch_id"], move["qty_delta"], move["reason"]) == (
        default,
        "-1000.000",
        "damaged",
    )
    assert on_hand(db_session, default) == 24000


def test_more_waste_than_on_hand_is_a_400(client: TestClient, cat: Catalogue):
    (batch,) = batch_ids(receive(client, cat, milk_line(cat, "2")))

    response = post(client, "/waste", waste_json(cat, "2500", batch), cat)

    assert response.status_code == 400
    assert response.json() == {"detail": "can't waste 2500: the batch has 2000.000 on hand"}


# --- Reverse --------------------------------------------------------------------


def test_reversing_a_usage_restores_both_batches(
    client: TestClient, db_session: Session, cat: Catalogue
):
    receipt = receive(
        client, cat, milk_line(cat, "1", "0.900", 15), milk_line(cat, "2", "0.850", 12)
    )
    later, sooner = batch_ids(receipt)
    usage = post(client, "/usage", usage_json(cat, "2500"), cat).json()

    response = post(client, "/reversals", reversal_json(usage["movements"][0]["client_id"]), cat)

    assert response.status_code == 201
    reversals = response.json()["movements"]
    assert [(m["type"], m["batch_id"], m["qty_delta"], m["unit_cost"]) for m in reversals] == [
        ("reversal", sooner, "2000.000", "0.000850"),
        ("reversal", later, "500.000", "0.000900"),
    ]
    assert [m["reverses_id"] for m in reversals] == [m["id"] for m in usage["movements"]]
    # Each reversal keeps its original's occurred_at.
    assert [m["occurred_at"] for m in reversals] == [m["occurred_at"] for m in usage["movements"]]
    assert (on_hand(db_session, sooner), on_hand(db_session, later)) == (2000, 1000)


def test_a_request_can_be_reversed_only_once(client: TestClient, cat: Catalogue):
    receive(client, cat, milk_line(cat, "1"))
    usage_body = usage_json(cat, "200")
    post(client, "/usage", usage_body, cat)
    assert (
        post(client, "/reversals", reversal_json(usage_body["client_id"]), cat).status_code == 201
    )

    response = post(client, "/reversals", reversal_json(usage_body["client_id"]), cat)

    assert response.status_code == 400
    assert response.json() == {"detail": f"request {usage_body['client_id']} was already reversed"}


def test_a_receipt_cant_be_reversed(client: TestClient, cat: Catalogue):
    receipt = receive(client, cat, milk_line(cat, "1"))

    body = reversal_json(receipt["movements"][0]["client_id"])
    response = post(client, "/reversals", body, cat)

    assert response.status_code == 400
    assert response.json() == {"detail": "only usage and waste can be reversed, not receive"}


# --- Replays --------------------------------------------------------------------


def replay_cases(client: TestClient, cat: Catalogue) -> dict[str, tuple[str, dict[str, Any]]]:
    """A valid request per write endpoint, after receiving the stock they need."""
    (batch,) = batch_ids(receive(client, cat, milk_line(cat, "2")))
    usage_body = usage_json(cat, "100")
    post(client, "/usage", usage_body, cat)
    return {
        "receipt": ("/receipts", receipt_json(cat, [milk_line(cat, "1")], invoice_ref="INV-9")),
        "usage": ("/usage", usage_json(cat, "100")),
        "waste": ("/waste", waste_json(cat, "100", batch)),
        "reversal": ("/reversals", reversal_json(usage_body["client_id"])),
    }


@pytest.mark.parametrize("kind", ["receipt", "usage", "waste", "reversal"])
def test_a_write_sent_twice_is_replayed(
    client: TestClient, db_session: Session, cat: Catalogue, kind: str
):
    path, body = replay_cases(client, cat)[kind]
    first = post(client, path, body, cat)
    assert first.status_code == 201
    count = movement_count(db_session)

    again = post(client, path, body, cat)

    assert again.status_code == 200
    replay = again.json()
    assert replay["replayed"] is True
    assert replay["movements"] == first.json()["movements"]
    if kind == "receipt":
        assert replay["receipt"] == first.json()["receipt"]
    if kind == "usage":
        # use() doesn't recompute the shortfall for a replay.
        assert replay["shortfall"] is None
    assert movement_count(db_session) == count


# --- Timestamps (D46) -----------------------------------------------------------


def timestamp_case(
    client: TestClient, cat: Catalogue, kind: str, moment: datetime | str
) -> tuple[str, dict[str, Any]]:
    """A request of that kind with its timestamp set to moment."""
    if kind == "receipt":
        return "/receipts", receipt_json(cat, [milk_line(cat, "1")], received_at=moment)
    if kind == "usage":
        return "/usage", usage_json(cat, "100", occurred_at=moment)
    (batch,) = batch_ids(receive(client, cat, milk_line(cat, "1")))
    return "/waste", waste_json(cat, "100", batch, occurred_at=moment)


FIELDS = {"receipt": "received_at", "usage": "occurred_at", "waste": "occurred_at"}


@pytest.mark.parametrize("kind", FIELDS)
def test_a_timestamp_10_minutes_ahead_is_a_400(client: TestClient, cat: Catalogue, kind: str):
    path, body = timestamp_case(client, cat, kind, NOW + timedelta(minutes=10))

    response = post(client, path, body, cat)

    assert response.status_code == 400
    assert response.json() == {
        "detail": f"{FIELDS[kind]} is more than 5 minutes ahead of the server clock"
    }


@pytest.mark.parametrize("kind", FIELDS)
def test_a_timestamp_4_minutes_ahead_is_accepted(client: TestClient, cat: Catalogue, kind: str):
    path, body = timestamp_case(client, cat, kind, NOW + timedelta(minutes=4))

    assert post(client, path, body, cat).status_code == 201


@pytest.mark.parametrize("kind", FIELDS)
def test_a_timestamp_without_a_timezone_is_a_422(client: TestClient, cat: Catalogue, kind: str):
    path, body = timestamp_case(client, cat, kind, "2026-11-02T12:00:00")

    response = post(client, path, body, cat)

    assert response.status_code == 422
    assert [e["loc"] for e in response.json()["detail"]] == [["body", FIELDS[kind]]]


def test_a_body_with_an_unknown_field_is_a_422(client: TestClient, cat: Catalogue):
    response = post(client, "/usage", usage_json(cat, "100", source="simulator"), cat)

    assert response.status_code == 422
    assert [e["type"] for e in response.json()["detail"]] == ["extra_forbidden"]


# --- Another business's rows ----------------------------------------------------


def cross_tenant_cases(
    client: TestClient, cat: Catalogue, other: Catalogue
) -> list[tuple[str, dict[str, Any], str]]:
    """Writes for cat that name one of other's rows, and the error each gives."""
    (other_batch,) = batch_ids(receive(client, other, milk_line(other, "1")))
    other_usage = usage_json(other, "100")
    post(client, "/usage", other_usage, other)
    return [
        (
            "/receipts",
            receipt_json(cat, [milk_line(cat, "1")], location_id=other.location.id),
            f"location {other.location.id} not found",
        ),
        (
            "/receipts",
            receipt_json(cat, [milk_line(cat, "1")], supplier_id=other.supplier.id),
            f"supplier {other.supplier.id} not found",
        ),
        (
            "/receipts",
            receipt_json(cat, [milk_line(other, "1")]),
            f"item {other.item.id} not found",
        ),
        (
            "/usage",
            usage_json(cat, "100", location_id=other.location.id),
            f"location {other.location.id} not found",
        ),
        (
            "/usage",
            usage_json(cat, "100", item_id=other.item.id),
            f"item {other.item.id} not found",
        ),
        (
            "/waste",
            waste_json(cat, "100", other_batch),
            f"batch {other_batch} not found for item {cat.item.sku} at this location",
        ),
        (
            "/waste",
            waste_json(cat, "100", other_batch, item_id=other.item.id),
            f"item {other.item.id} not found",
        ),
        (
            "/reversals",
            reversal_json(other_usage["client_id"]),
            f"no request with client_id {other_usage['client_id']}",
        ),
    ]


CROSS_TENANT_IDS = [
    "receipt-location",
    "receipt-supplier",
    "receipt-item",
    "usage-location",
    "usage-item",
    "waste-batch",
    "waste-item",
    "reversal-request",
]


@pytest.mark.parametrize("case", range(len(CROSS_TENANT_IDS)), ids=CROSS_TENANT_IDS)
def test_another_business_s_rows_are_not_found(
    client: TestClient, db_session: Session, cat: Catalogue, other: Catalogue, case: int
):
    path, body, detail = cross_tenant_cases(client, cat, other)[case]
    count = movement_count(db_session)

    response = post(client, path, body, cat)

    assert response.status_code == 400
    assert response.json() == {"detail": detail}
    assert movement_count(db_session) == count


# --- GET /stock -----------------------------------------------------------------


def test_stock_shows_totals_and_batches_in_fefo_order(
    client: TestClient, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    later, sooner = batch_ids(
        receive(
            client,
            cat,
            milk_line(cat, "1", "0.900", 15, lot="L-15"),
            milk_line(cat, "2", "0.850", 12),
        )
    )
    post(client, "/usage", usage_json(cat, "500"), cat)

    entries = stock(client, cat)

    milk = entry_for(entries, cat.item)
    assert milk | {"batches": None} == {
        "item_id": str(cat.item.id),
        "sku": cat.item.sku,
        "item_name": "Whole milk",
        "category": None,
        "base_unit": "ml",
        "location_id": str(cat.location.id),
        "location_name": "Main branch",
        "on_hand": "2500.000",
        "expired": "0.000",
        "batches": None,
    }
    # The default batch holds nothing, so it isn't listed.
    assert milk["batches"] == [
        {
            "id": sooner,
            "is_default": False,
            "expires_on": "2026-11-12",
            "lot_code": None,
            "received_at": milk["batches"][0]["received_at"],
            "on_hand": "1500.000",
            "unit_cost": "0.000850",
            "expired": False,
        },
        {
            "id": later,
            "is_default": False,
            "expires_on": "2026-11-15",
            "lot_code": "L-15",
            "received_at": milk["batches"][1]["received_at"],
            "on_hand": "1000.000",
            "unit_cost": "0.000900",
            "expired": False,
        },
    ]
    assert all(datetime.fromisoformat(b["received_at"]) == RECEIVED_AT for b in milk["batches"])
    # Flour was never received: it is listed with nothing on hand.
    never_received = entry_for(entries, flour[0])
    assert (never_received["on_hand"], never_received["expired"]) == ("0.000", "0.000")
    assert never_received["batches"] == []


def test_stock_leaves_out_empty_batches(client: TestClient, cat: Catalogue):
    later, sooner = batch_ids(
        receive(client, cat, milk_line(cat, "1", "0.900", 15), milk_line(cat, "2", "0.850", 12))
    )
    post(client, "/usage", usage_json(cat, "2000"), cat)

    milk = entry_for(stock(client, cat), cat.item)

    assert [b["id"] for b in milk["batches"]] == [later]
    assert milk["on_hand"] == "1000.000"


def test_stock_shows_a_negative_default_batch(
    client: TestClient, db_session: Session, cat: Catalogue
):
    post(client, "/usage", usage_json(cat, "500"), cat)

    milk = entry_for(stock(client, cat), cat.item)

    assert milk["on_hand"] == "-500.000"
    (default,) = milk["batches"]
    assert default["id"] == default_batch_id(db_session, cat.location.id, cat.item.id)
    assert (default["is_default"], default["expires_on"], default["on_hand"]) == (
        True,
        None,
        "-500.000",
    )
    # Nothing has been received into it, so it has no cost yet.
    assert default["unit_cost"] is None
    assert default["expired"] is False


@pytest.mark.parametrize(
    ("now", "expired"),
    [
        # 23:59 on 9 Nov in Amman: a batch is usable on its expiry date.
        (nov(9, 20, 59), False),
        # 02:30 on 10 Nov in Amman, though still 9 Nov in UTC.
        (nov(9, 23, 30), True),
    ],
    ids=["9-nov-in-amman", "10-nov-in-amman"],
)
def test_stock_counts_a_batch_expired_by_the_business_s_date(
    client: TestClient,
    cat: Catalogue,
    pin_now: Callable[[datetime], None],
    now: datetime,
    expired: bool,
):
    nine, ten = batch_ids(
        receive(
            client, cat, milk_line(cat, "1", expires_day=9), milk_line(cat, "2", expires_day=10)
        )
    )
    pin_now(now)

    milk = entry_for(stock(client, cat), cat.item)

    assert [(b["id"], b["expired"]) for b in milk["batches"]] == [(nine, expired), (ten, False)]
    assert milk["expired"] == ("1000.000" if expired else "0.000")
    assert milk["on_hand"] == "3000.000"


def test_stock_filters_by_location_and_item(
    client: TestClient,
    db_session: Session,
    cat: Catalogue,
    other: Catalogue,
    flour: tuple[Item, ItemUnit],
):
    annex = add(db_session, new_location(cat.business.id, name="Annex", kind="storeroom"))
    receive(client, cat, milk_line(cat, "1"))
    body = receipt_json(cat, [milk_line(cat, "2")], location_id=annex.id)
    assert post(client, "/receipts", body, cat).status_code == 201

    # Sorted by location name, then SKU.
    items = sorted([cat.item, flour[0]], key=lambda item: item.sku)
    expected = [(str(loc.id), str(item.id)) for loc in (annex, cat.location) for item in items]
    assert [(e["location_id"], e["item_id"]) for e in stock(client, cat)] == expected

    at_annex = stock(client, cat, location_id=annex.id)
    assert [(e["location_name"], e["item_id"]) for e in at_annex] == [
        ("Annex", str(item.id)) for item in items
    ]
    assert entry_for(at_annex, cat.item)["on_hand"] == "2000.000"

    milk_everywhere = stock(client, cat, item_id=cat.item.id)
    assert [(e["location_name"], e["on_hand"]) for e in milk_everywhere] == [
        ("Annex", "2000.000"),
        ("Main branch", "1000.000"),
    ]

    both = stock(client, cat, location_id=cat.location.id, item_id=cat.item.id)
    assert [(e["location_name"], e["on_hand"]) for e in both] == [("Main branch", "1000.000")]

    # Another business's location matches nothing.
    assert stock(client, cat, location_id=other.location.id) == []


def test_stock_leaves_out_inactive_items(client: TestClient, db_session: Session, cat: Catalogue):
    add(db_session, new_item(cat.business.id, name="Old syrup", is_active=False))

    assert [e["item_id"] for e in stock(client, cat)] == [str(cat.item.id)]


def test_stock_shows_only_the_business_s_rows(
    client: TestClient, db_session: Session, cat: Catalogue, other: Catalogue
):
    receive(client, cat, milk_line(cat, "1"))
    receive(client, other, milk_line(other, "2"))

    (entry,) = stock(client, cat)

    assert (entry["item_id"], entry["location_id"]) == (str(cat.item.id), str(cat.location.id))
    assert entry["on_hand"] == "1000.000"
    other_batches = db_session.scalars(
        select(Batch.id).where(Batch.business_id == other.business.id)
    ).all()
    assert not {b["id"] for b in entry["batches"]} & {str(b) for b in other_batches}


# --- End to end -----------------------------------------------------------------


def test_receive_use_waste_and_reverse_reconcile(
    client: TestClient, db_session: Session, cat: Catalogue, flour: tuple[Item, ItemUnit]
):
    flour_item, sack = flour
    receive(
        client,
        cat,
        milk_line(cat, "1", "0.900", 15),
        milk_line(cat, "2", "0.850", 12),
        receipt_line_json(flour_item, sack, "2", "12.500"),
    )

    usage = usage_json(cat, "2500")
    assert post(client, "/usage", usage, cat).status_code == 201
    shortfall = usage_json(cat, "1000", occurred_at=nov(3))
    assert post(client, "/usage", shortfall, cat).json()["shortfall"] == "500.000"
    flour_waste = waste_json(cat, "3000", None, item_id=flour_item.id, reason="spoiled")
    assert post(client, "/waste", flour_waste, cat).status_code == 201
    assert post(client, "/reversals", reversal_json(usage["client_id"]), cat).status_code == 201

    assert reconcile(db_session, cat.business.id) == []
    entries = stock(client, cat)
    # Milk: 2000 back in the sooner batch after the reversal, 500 in the later
    # one, and the default batch 500 short. Flour: two sacks less 3000 g.
    assert entry_for(entries, cat.item)["on_hand"] == "2000.000"
    assert entry_for(entries, flour_item)["on_hand"] == "47000.000"
