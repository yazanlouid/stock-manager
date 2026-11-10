"""The demo data commands.

reset is only ever run with input and the schema and simulate steps
monkeypatched, so no test touches the dev database's schema or recreates
stock_test. The other commands run on the test's rolled-back transaction
through a session factory bound to db_session's connection.
"""

from argparse import Namespace
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import cli
from app.config import get_settings
from app.models import Business
from tests.builders import Catalogue, add, new_balance, new_batch


@pytest.fixture
def session_factory(db_session: Session):
    """New sessions on db_session's connection, so their commits only release savepoints."""
    connection = db_session.connection()
    return lambda: Session(bind=connection, join_transaction_mode="create_savepoint")


@pytest.fixture
def reset_steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Replace the schema and simulate steps with stubs that record their calls."""
    calls: list[tuple] = []

    def run_simulate(args: Namespace, session_factory) -> int:
        calls.append(("simulate", args.days, args.end_date, args.random_seed))
        return 0

    monkeypatch.setattr(cli, "recreate_schema", lambda: calls.append(("recreate_schema",)))
    monkeypatch.setattr(cli, "run_simulate", run_simulate)
    return calls


@pytest.mark.parametrize("answer", ["", "no", "y", "YES", "yes please"])
def test_reset_does_nothing_unless_the_answer_is_yes(
    answer: str, reset_steps: list[tuple], monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)

    assert cli.main(["reset"]) == 1
    assert reset_steps == []
    assert "nothing was changed" in capsys.readouterr().out


def test_reset_names_the_database_without_its_password(
    reset_steps: list[tuple], monkeypatch: pytest.MonkeyPatch, capsys
):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "no")

    cli.main(["reset"])

    out = capsys.readouterr().out
    url = make_url(get_settings().database_url)
    assert f"{url.database!r} on {url.host}:{url.port}" in out
    if url.password:
        assert url.password not in out + "".join(prompts)


def test_reset_recreates_the_schema_then_simulates(
    reset_steps: list[tuple], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("builtins.input", lambda prompt: " yes ")

    args = ["reset", "--days", "7", "--end-date", "2026-10-31", "--random-seed", "5"]
    assert cli.main(args) == 0

    assert reset_steps == [("recreate_schema",), ("simulate", 7, date(2026, 10, 31), 5)]


def test_simulate_refuses_a_database_that_already_has_a_business(
    session_factory, db_session: Session, cat: Catalogue, capsys
):
    assert cli.main(["simulate", "--days", "1"], session_factory) == 1

    assert "already has a business" in capsys.readouterr().out
    assert db_session.scalar(select(func.count()).select_from(Business)) == 1


def test_simulate_seeds_simulates_and_reconciles(session_factory, capsys):
    args = ["simulate", "--days", "1", "--end-date", "2026-10-31"]
    assert cli.main(args, session_factory) == 0

    out = capsys.readouterr().out
    assert "Days: 1, 2026-10-31 to 2026-10-31 (opening stock on 2026-10-30)" in out
    assert "Movements: receive" in out
    assert "Reconcile Dar Zaatar Café: OK" in out


def test_reconcile_exits_zero_when_every_ledger_reconciles(session_factory, cat, capsys):
    assert cli.main(["reconcile"], session_factory) == 0
    assert "Reconcile Test Café: OK" in capsys.readouterr().out


def test_reconcile_exits_one_and_prints_each_problem(
    session_factory, db_session: Session, cat: Catalogue, capsys
):
    # An item with a batch but no default batch.
    add(db_session, new_balance(add(db_session, new_batch(cat.location, cat.item))))

    assert cli.main(["reconcile"], session_factory) == 1
    out = capsys.readouterr().out
    assert "Reconcile Test Café: 1 problem\n" in out
    assert "default_batch_count:" in out


def test_days_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["simulate", "--days", "0"])
    assert "must be at least 1" in capsys.readouterr().err
