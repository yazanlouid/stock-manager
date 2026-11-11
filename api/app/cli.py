"""Demo data commands, run from api/:

    uv run python -m app.cli reset [--days N] [--end-date YYYY-MM-DD] [--random-seed N]
    uv run python -m app.cli simulate [--days N] [--end-date YYYY-MM-DD] [--random-seed N]
    uv run python -m app.cli reconcile

reset recreates the schema (Alembic downgrade to base, then upgrade to head)
after you type yes, then does what simulate does (D37). simulate seeds the café
and simulates it into a migrated, empty database. reconcile checks every
business's ledger and exits with 1 if it finds a problem.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.domain.reconcile import reconcile
from app.models import Business
from app.sim.cafe import CAFE
from app.sim.seed import seed
from app.sim.simulator import (
    DEFAULT_RANDOM_SEED,
    SimulationError,
    SimulationSummary,
    simulate,
)

API_DIR = Path(__file__).resolve().parents[1]

type SessionFactory = Callable[[], Session]


def main(argv: Sequence[str] | None = None, session_factory: SessionFactory = SessionLocal) -> int:
    args = _parser().parse_args(argv)
    match args.command:
        case "reset":
            return reset(args, session_factory)
        case "simulate":
            return run_simulate(args, session_factory)
        case _:
            return run_reconcile(session_factory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Demo data commands.")
    commands = parser.add_subparsers(dest="command", required=True)

    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--days", type=_positive_int, default=30, help="days to simulate (default 30)"
    )
    options.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        help="last simulated day, YYYY-MM-DD (default: today in the business's timezone)",
    )
    options.add_argument(
        "--random-seed",
        type=int,
        default=DEFAULT_RANDOM_SEED,
        help=f"random seed (default {DEFAULT_RANDOM_SEED})",
    )
    commands.add_parser(
        "reset", parents=[options], help="recreate the schema, then seed and simulate"
    )
    commands.add_parser(
        "simulate", parents=[options], help="seed and simulate into an empty database"
    )
    commands.add_parser("reconcile", help="check every business's ledger")
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


# --- reset ----------------------------------------------------------------------


def reset(args: argparse.Namespace, session_factory: SessionFactory) -> int:
    url = make_url(get_settings().database_url)
    print(f"This erases every table in database {url.database!r} on {url.host}:{url.port}")
    print("and fills it with fresh demo data.")
    if input("Type yes to continue: ").strip() != "yes":
        print("Cancelled; nothing was changed.")
        return 1
    recreate_schema()
    return run_simulate(args, session_factory)


def recreate_schema() -> None:
    """Drop every table with Alembic's downgrade to base, then migrate to head.

    Dropping the tables removes the ledger without deleting a row, and the
    append-only trigger is recreated with the stock tables.
    """
    config = Config(str(API_DIR / "alembic.ini"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")


# --- simulate -------------------------------------------------------------------


def run_simulate(args: argparse.Namespace, session_factory: SessionFactory) -> int:
    with session_factory() as session:
        if session.scalar(select(Business.id).limit(1)) is not None:
            print("The database already has a business, so simulate won't add another.")
            print("Run reset to start again from an empty database.")
            return 1
        business_id = seed(session, CAFE)
        try:
            summary = simulate(
                session,
                business_id,
                venue=CAFE,
                days=args.days,
                end_date=args.end_date,
                random_seed=args.random_seed,
            )
        except SimulationError as exc:
            print(f"Simulation stopped: {exc}", file=sys.stderr)
            print("Days before the failed one are committed.", file=sys.stderr)
            return 1
    print(f"Simulated {CAFE.name}.")
    for line in format_summary(summary):
        print(line)
    print()
    return run_reconcile(session_factory)


def format_summary(summary: SimulationSummary) -> list[str]:
    def counts(counter) -> str:
        return ", ".join(f"{key} {count}" for key, count in sorted(counter.items())) or "none"

    shortfalls = ", ".join(f"{sku} {n}" for sku, n in sorted(summary.shortfalls_by_sku.items()))
    return [
        f"Days: {summary.days}, {summary.first_day} to {summary.end_date}"
        f" (opening stock on {summary.opening_day}), random seed {summary.random_seed}",
        f"Receipts: {summary.receipts} ({summary.late_deliveries} late)",
        f"Movements: {counts(summary.movements_by_type)}",
        f"Waste by reason: {counts(summary.waste_by_reason)}",
        f"Shortfalls: {summary.shortfalls}" + (f" ({shortfalls})" if shortfalls else ""),
        f"Settle pairs: {summary.settle_pairs}",
        f"Reversals: {summary.reversals}",
    ]


# --- reconcile ------------------------------------------------------------------


def run_reconcile(session_factory: SessionFactory) -> int:
    found = False
    with session_factory() as session:
        businesses = session.execute(
            select(Business.id, Business.name).order_by(Business.name, Business.id)
        ).all()
        if not businesses:
            print("Reconcile: no businesses to check.")
        for business_id, name in businesses:
            problems = reconcile(session, business_id)
            if not problems:
                print(f"Reconcile {name}: OK")
                continue
            found = True
            plural = "" if len(problems) == 1 else "s"
            print(f"Reconcile {name}: {len(problems)} problem{plural}")
            for problem in problems:
                print(f"  {problem.kind}: {problem.message}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
